"""
R9.4/R9.5 核心持仓研究预取器（Python 侧：取数 + 算估值，定性判断交给 skill）。

用法：python core_research.py
产物：output/{date}/core_inputs.json —— 每只核心股的 filings 元数据 / 财务序列摘要 /
      估值带 / 缠论与技术状态 / 财报日历 / 台账派生（成本、进度、底仓下限）。
台账：output/core_ledger.json 不存在时生成空模板（真金成交须用户手动维护）。

诚实边界：advisory 预取，无回测无信号发射；数据缺失如实进 degraded，不臆造。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
from loguru import logger

from config.stocks import (BASE_FLOOR_FRAC, CORE_HOLDINGS, CORE_LEDGER_PATH,
                           CORE_TARGET_FRAC)
from data.cache import SQLiteCache
from data.edgar_source import EdgarSource
from data.yfinance_source import YFinanceSource
from signals.chan.chan_signal import HIGH_VOL_PCT, compute_chan_signal
from signals.valuation import (ONEOFF_FLAG_THRESHOLD, _last_close, qqq_valuation,
                               valuation_band)

_PRICE_DAYS = 800   # 与实盘管线同窗口（缠论需 ≥200 根，推荐 550+ TD）

_LEDGER_TEMPLATE = {
    "_readme": "真金核心台账：每笔成交按时间顺序追加进对应票 fills（layer: base=底仓|enh=增强层）；"
               "买入 shares 填正数、卖出填负数；price 为 null 或 ≤0 = 成本待补（只计股数，"
               "所有成本类结论会被跳过）；total_capital 填总资金美元数（核心目标 = 70% × 此值）；"
               "enhancement_rounds 记高抛低吸配对（status: open|paired|abandoned）。"
               "底仓下限锚定历史最高已建股数，不随高抛回落。",
    "total_capital": None,
    "positions": {t: {"fills": []} for t in CORE_HOLDINGS},
    "enhancement_rounds": [],
}


def _load_ledger() -> dict:
    path = Path(CORE_LEDGER_PATH)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_LEDGER_TEMPLATE, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        logger.warning(f"[Core] 台账不存在，已生成空模板: {path}（请填入真实成交）")
        return json.loads(json.dumps(_LEDGER_TEMPLATE))
    return json.loads(path.read_text(encoding="utf-8"))


def _baseline_plan(mode: str, budget: float, per_name: dict, records: dict) -> dict:
    """把当月基线余额摊到各名（纯算术，不含估值/结构/财报窗口判断）。

    `proportional`：按**当前**缺口占比分摊——缺口每月重算，某名补满后自动退出分摊，自我校正。
    `largest_gap`：全额给缺口最大的一只（贪心；路径上会让单名连续独占数月）。
    两者 12 个月终点相同，差别只在路径。财报窗口造成的推迟/改投由 skill 决定——
    本层不知道 `next_earnings` 的语义，不该在这里做判断。

    ⚠️ 分摊会产生**不足一股**的碎额（月度基线摊到七只、而单价数百美元时，高价名必然如此），
    列入 `sub_one_share` 交给 skill 提示，不在此处四舍五入掩盖。
    """
    gaps = {t: r["gap_usd"] for t, r in per_name.items() if (r.get("gap_usd") or 0) > 0}
    plan: dict = {"mode": mode, "budget_usd": round(budget, 2), "per_name": {},
                  "sub_one_share": []}
    if budget <= 0 or not gaps:
        return plan
    if mode == "proportional":
        total = sum(gaps.values())
        alloc = {t: budget * g / total for t, g in gaps.items()}
    else:  # largest_gap：贪心，全额给缺口最大的一只
        alloc = {max(gaps, key=lambda t: gaps[t]): budget}
    for t, usd in sorted(alloc.items(), key=lambda kv: -kv[1]):
        px = records.get(t, {}).get("price")
        row = {"alloc_usd": round(usd, 2), "price": px}
        if px:
            row["shares_frac"] = round(usd / float(px), 3)
            row["shares_whole"] = int(usd // float(px))
            if row["shares_whole"] < 1:
                plan["sub_one_share"].append(t)
        plan["per_name"][t] = row
    return plan


def _policy_view(ledger: dict, records: dict, asof: str) -> dict:
    """把台账 `policy` 的配置意图落成可执行口径：逐名缺口 + 当月基线执行情况。

    `policy` 是**用户的配置意图，不是回测结论**——本函数只做算术，不做任何判断。
    缺 `policy` 键时返回 {}，skill 退回「估值门为入场闸门」的旧口径。

    ⚠️ 当月已投（mtd）只认**带 date 的买入 fill**：存量汇总 fill 的 date 为 null，
    无法归月，计入 `undated_priced_buys` 并打 `MTD_UNVERIFIABLE`——宁可报不可核算，
    也不把无日期的历史成本当成本月投入（那会让基线永远显示"已完成"）。
    """
    pol = ledger.get("policy") or {}
    if not pol:
        return {}
    out: dict = {"degraded": []}
    sch = pol.get("build_schedule") or {}
    tgt = pol.get("per_name_target_usd") or {}

    per_name, undated = {}, 0
    for t in CORE_HOLDINGS:
        inv = float((records.get(t, {}).get("ledger") or {}).get("invested") or 0.0)
        tv = tgt.get(t)
        row = {"invested_usd": round(inv, 2), "target_usd": tv}
        if tv:
            row["gap_usd"] = round(float(tv) - inv, 2)
            row["built_frac"] = round(inv / float(tv), 4)
        per_name[t] = row
    out["per_name"] = per_name
    if tgt:
        out["target_sum_usd"] = round(sum(float(v) for v in tgt.values()), 2)
        out["gap_sum_usd"] = round(sum(r.get("gap_usd") or 0 for r in per_name.values()), 2)
        # 基线该投向谁：缺口最大的先补（纯算术排序，不含估值/结构判断）
        out["largest_gaps"] = [
            {"ticker": t, "gap_usd": r["gap_usd"]}
            for t, r in sorted(per_name.items(), key=lambda kv: -(kv[1].get("gap_usd") or 0))
            if r.get("gap_usd", 0) > 0][:3]

    if sch:
        month = str(asof)[:7]
        mtd = 0.0
        for t in CORE_HOLDINGS:
            for f in ledger.get("positions", {}).get(t, {}).get("fills", []):
                n, p = float(f.get("shares") or 0), f.get("price")
                if n <= 0 or p is None or float(p) <= 0:
                    continue
                d = pd.to_datetime(f.get("date"), errors="coerce")
                if pd.isna(d):
                    undated += 1
                elif str(d.date())[:7] == month:
                    mtd += n * float(p)
        base = sch.get("monthly_baseline_usd")
        out.update({
            "mode": sch.get("mode"),
            "monthly_baseline_usd": base,
            "trigger_extra_tranche_usd": sch.get("trigger_extra_tranche_usd"),
            "horizon_months": sch.get("horizon_months"),
            "started": sch.get("started"),
            "current_month": month,
            "mtd_invested_usd": round(mtd, 2),
            "undated_priced_buys": undated,
            "fractional_shares": bool(sch.get("fractional_shares")),
        })
        if base:
            out["baseline_remaining_usd"] = round(max(0.0, float(base) - mtd), 2)
            out["baseline_met"] = mtd >= float(base)
            out["baseline_allocation"] = sch.get("baseline_allocation") or "largest_gap"
            out["baseline_plan"] = _baseline_plan(
                out["baseline_allocation"], out["baseline_remaining_usd"], per_name, records)
        if undated:
            out["degraded"].append(
                f"MTD_UNVERIFIABLE({undated} 笔买入 fill 无 date，无法归月；"
                f"本月已投仅统计带日期的成交)")
    return out


def _ledger_stats(ledger: dict, ticker: str, price: float | None) -> dict:
    """派生台账口径。fills 按列表顺序视作时间序（date 可空）；shares>0=买入、<0=卖出。

    三条纪律：
    ① **移动加权成本法**：卖出按当时均价等比扣减 invested，`avg_cost` 不因高抛而漂移
       （否则「摊低平均成本」这个核心目标的度量本身就被卖出污染）。
    ② **底仓下限锚定历史最高已建股数** built_peak，不随高抛回落——若按当前股数重算，
       每轮高抛后下限都会再降一档（0.7ⁿ），双层结构会被合法地蚕食至零。
    ③ price 为 None 或 ≤0 = **成本待补**：只计股数不计成本，cost_pending_shares 显式暴露。
    """
    fills = ledger.get("positions", {}).get(ticker, {}).get("fills", [])
    shares = priced_shares = invested = realized = built_peak = 0.0
    for f in fills:
        n = float(f.get("shares") or 0)
        p = f.get("price")
        p = float(p) if p is not None and float(p) > 0 else None   # ≤0 视同待补
        if n >= 0:
            shares += n
            if p is not None:
                invested += n * p
                priced_shares += n
            built_peak = max(built_peak, shares)
        else:
            q = min(-n, shares)          # 不允许卖成负持仓
            avg = invested / priced_shares if priced_shares else None
            cut = q * (priced_shares / shares) if shares else 0.0   # 按已知成本占比等比出库
            shares -= q
            priced_shares -= cut
            if avg is not None:
                invested -= cut * avg
                if p is not None:
                    realized += cut * (p - avg)

    floor = int(built_peak * BASE_FLOOR_FRAC)
    stats = {
        "shares": round(shares, 4),
        "built_peak_shares": round(built_peak, 4),
        "cost_pending_shares": round(shares - priced_shares, 4),
        "avg_cost": round(invested / priced_shares, 3) if priced_shares > 0 else None,
        "invested": round(invested, 2),
        "realized_pnl": round(realized, 2),
        # 口径二：已实现盈亏冲抵后的实际持仓成本——「高抛低吸降低成本」的度量本身。
        # avg_cost 按会计口径不受卖出影响，只看它会看不出增强层到底有没有摊低成本。
        "effective_avg_cost": (round((invested - realized) / shares, 3)
                               if shares > 0 and priced_shares > 0 else None),
        # 底仓下限（R9.0 双层结构）：高抛卖出不得使持仓跌破此股数
        "base_floor_shares": floor,
        "enhancement_shares": max(0, int(round(shares)) - floor),
    }
    if shares and price:
        stats["market_value"] = round(shares * price, 2)
        if stats["avg_cost"]:
            stats["unrealized_pct"] = round(price / stats["avg_cost"] - 1, 4)
    open_rounds = [r for r in ledger.get("enhancement_rounds", [])
                   if r.get("ticker") == ticker and r.get("status") == "open"]
    stats["open_enhancement_rounds"] = open_rounds
    return stats


def _fin_summary(fin: dict[str, pd.DataFrame]) -> dict:
    """年/季关键科目 + YoY（skill 判 thesis 用的结构化趋势，非估值口径）。"""
    out: dict = {}
    for freq in ("annual", "quarterly"):
        df = fin.get(freq, pd.DataFrame())
        if df.empty:
            continue
        blk: dict = {}
        for row in df.index:
            vals = df.loc[row].dropna()
            blk[row] = {str(k)[:10]: (round(float(v), 4) if abs(float(v)) < 1e3
                                      else round(float(v)))
                        for k, v in vals.items()}
        # YoY：年度=相邻财年；季度=同比（隔 4 期）
        rev = df.loc["Total Revenue"].dropna() if "Total Revenue" in df.index else pd.Series(dtype=float)
        if len(rev) >= 2:
            lag = 1 if freq == "annual" else min(4, len(rev) - 1)
            if len(rev) > lag and float(rev.iloc[lag]) != 0:
                blk["revenue_yoy_latest"] = round(float(rev.iloc[0]) / float(rev.iloc[lag]) - 1, 4)
        out[freq] = blk
    return out


CONSENSUS_THIN_N = 10   # 季度一致预期覆盖分析师少于此数即打薄标
_GATE_ROLL_DAYS = 45    # 下次财报比表上日期晚过此天数 ⇒ 季度已滚过（而非日期漂移）
GATES_PATH = "output/earnings_gates.json"


def _load_gates() -> dict:
    """财报前裁决表。文件不存在 = 整块缺席（skill 会提示补写），不生成模板。"""
    p = Path(GATES_PATH)
    if not p.exists():
        return {}
    try:
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("gates") or {}
    except Exception as e:
        logger.warning(f"[Core] earnings_gates 解析失败: {e}")
        return {}


def _gate_view(gate: dict | None, next_earnings: str | None) -> tuple[dict, list[str]]:
    """把一名的 gate 附上完整性校验。**只校验，不评判命中**——判定在 skill。

    裁决表的价值来源**只有一条**：`written_at` 早于 `earnings_date`。
    晚于 ⇒ 事后写的 ⇒ 无预测价值 ⇒ 打 `GATES_POST_HOC` 作废。
    这条检查必须由代码做而非由我口头承诺——否则「事前写死」就成了一句无法核验的话。
    """
    if not gate:
        return {}, (["GATES_MISSING(下次财报已知但无财报前裁决表)"] if next_earnings else [])
    flags: list[str] = []
    ed, wa = gate.get("earnings_date"), gate.get("written_at")
    if ed and wa and str(wa) >= str(ed):
        flags.append(f"GATES_POST_HOC(written_at={wa} 不早于 earnings_date={ed}，"
                     f"事后写的表无预测价值，不得当作事前判据)")
    if not gate.get("locked"):
        flags.append("GATES_UNLOCKED(locked≠true，表未锁定，可能被事后修改)")

    d2e = None
    if ed:
        try:
            d2e = (pd.Timestamp(ed) - pd.Timestamp.today().normalize()).days
        except Exception:
            pass

    # 「过期」的定义必须是**那一季已经报过**，不能是「日期对不上」（2026-08-28 修）。
    # yfinance 的 calendar 给的是**估计日**，公司正式确认前routinely 漂移几天；
    # 原先按 `!=` 判 stale，而 skill 收到 GATES_STALE 就会「为新一季写表、written_at 填当日」
    # —— 一次几天的日期漂移就足以把 written_at 推到今天，**表唯一的价值来源被自己毁掉**，
    # 且新阈值是在多看了两个月行情之后定的。这正是本功能要防的事，故判据收紧为二选一：
    #   ① d2e < 0：财报日已过（**不依赖 calendar**，calendar 取不到时仍然有效——
    #      原先两条 stale/missing 都挂在 next_earnings 上，calendar 一失败就整体失明）；
    #   ② 下次财报比表上的日期晚 45 天以上：季度真的滚过去了（季度间隔 ~91 天，漂移只有几天）。
    nxt = str(next_earnings)[:10] if next_earnings else None
    rolled = False
    if nxt and ed:
        try:
            rolled = (pd.Timestamp(nxt) - pd.Timestamp(ed)).days > _GATE_ROLL_DAYS
        except Exception:
            rolled = False
    if ed and ((d2e is not None and d2e < 0) or rolled):
        flags.append(f"GATES_STALE(表针对 {ed}"
                     + (f"，下次财报 {nxt}" if nxt else "，该日已过")
                     + " → 该季已过，须为新一季写表)")
    elif nxt and ed and nxt != ed:
        # 日期漂移但该季未过 ⇒ **只提示，绝不触发重写**；不计入 integrity。
        flags.append(f"GATES_DATE_DRIFT(表按 {ed} 写，calendar 现估 {nxt}，"
                     f"相差 {abs((pd.Timestamp(nxt) - pd.Timestamp(ed)).days)} 天 → "
                     f"财报日估计值漂移，属正常；**表照旧有效，不得重写**)")

    # ── 指引采集（2026-08-28 制度化）──────────────────────
    # 公司自己给的下季指引是财报前**信息量最高**的一个数，而且写表时它就已经可得
    # （上一季新闻稿里）。审计发现 6 只里只有 NVDA 录了 —— 这是**流程缺口不是数据缺口**：
    # 另五只 7 月底就报过，指引躺在它们的新闻稿里，只是没人去取。
    # 故此处显式点名，逼报告每次面对它；不计入 integrity（缺指引不影响表的事前性）。
    b = gate.get("basis") or {}
    has_g = any(k.startswith("guidance") for k in b)
    view_guidance = has_g
    if not has_g:
        flags.append("GATES_NO_GUIDANCE(表里没有公司自己的下季指引 → 判据只能挂在"
                     "一致预期上，少了最有信息量的那一条；下次财报后须读官方新闻稿补录)")

    view = dict(gate)
    view["guidance_captured"] = view_guidance
    view["days_to_earnings"] = d2e
    # 日期漂移不是完整性问题：它不影响 written_at 早于 earnings_date 这一唯一价值来源。
    _SOFT = ("GATES_DATE_DRIFT", "GATES_NO_GUIDANCE")
    hard = [f for f in flags if not f.startswith(_SOFT)]
    view["integrity"] = "ok" if not hard else "degraded"
    return view, flags


def _consensus(yft, fin: dict[str, pd.DataFrame]) -> dict:
    """卖方一致预期与历史 surprise（纯暴露，不进任何决策）。

    动机（2026-08-27）：`financials` 走 EDGAR/yfinance 报表，财报后要滞后数周才更新，
    而 `earnings_history` 在财报当晚就有 actual —— 报告因此会说「财报数据未到」，
    对利润表成立、对 actual-vs-estimate 不成立。此函数把后者补上，让财报后的裁决
    不必等报表。**只做事后加速，不做事前预测**：一致预期与修正方向都是公开信息，
    早已在价格里，不构成 edge（讨论见 2026-08-27）。

    ⚠️⚠️⚠️ **`surprise_pct` 是混口径量，且混法逐票不同**（2026-08-27 实测）：
    `epsEstimate` 恒为街面 **non-GAAP**；而 `epsActual` 有的票给 GAAP、有的票给 non-GAAP——
    GOOGL/AMZN/META/AAPL 的 actual **逐字等于**同季 GAAP 稀释 EPS，NVDA 的 actual
    却是 non-GAAP（2.22，其 GAAP 为 2.46）。后果：GOOGL 报 **+214%**、AMZN **+215%**
    「超预期」，纯粹是 GAAP 里那半数一次性损益撞上 non-GAAP 预期造出来的假象。
    故此处**探测**而非假设：把 actual 与同季 GAAP 稀释 EPS 比对，写出 `actual_basis`；
    `estimate_basis` 恒为 `non_gaap_street`。actual 走 GAAP 且该票 `oneoff_share` 超标时，
    由 main 打 `SURPRISE_MIXED_BASIS` —— 该票的 surprise **不得解读为超预期幅度**。

    下季**指引**不在此处：指引是新闻稿里的散文（"$108.0 billion, plus or minus 2%"），
    机器端点拿不到 —— 由 skill 读官方新闻稿取，与本块的 `next_quarter` 一致预期相减。
    """
    out: dict = {"estimate_basis": "non_gaap_street"}
    if yft is None:
        return out

    try:
        hist = yft.earnings_history
    except Exception:
        hist = None
    if hist is not None and not hist.empty:
        rows = []
        # 先排序再截取：yfinance 直接由 Yahoo 的 list 建帧、不保证升序，
        # 若某日返回新→旧，tail(8) 取到的会是**最老**的八季，last_report 静默指向陈旧季度。
        for q, r in hist.sort_index().tail(8).iloc[::-1].iterrows():      # 新→旧
            sp = r.get("surprisePercent")
            rows.append({"quarter": str(q)[:10],
                         "eps_actual": _f(r.get("epsActual")),
                         "eps_estimate": _f(r.get("epsEstimate")),
                         "surprise_pct": _f(sp, 4)})
        out["surprise_history"] = rows
        if rows:
            out["last_report"] = rows[0]
            out.update(_actual_basis(rows[0], fin))

    try:
        rev, eps = yft.revenue_estimate, yft.earnings_estimate
    except Exception:
        rev = eps = None
    nxt: dict = {}
    for src, pfx in ((rev, "revenue"), (eps, "eps")):
        if src is None or getattr(src, "empty", True) or "0q" not in src.index:
            continue
        row = src.loc["0q"]
        nxt[f"{pfx}_avg"] = _f(row.get("avg"), 4)
        nxt[f"{pfx}_low"], nxt[f"{pfx}_high"] = _f(row.get("low"), 4), _f(row.get("high"), 4)
        nxt[f"{pfx}_n"] = _f(row.get("numberOfAnalysts"), 0)
    if nxt:
        out["next_quarter"] = nxt

    try:
        trend, rev30 = yft.eps_trend, yft.eps_revisions
    except Exception:
        trend = rev30 = None
    if trend is not None and not getattr(trend, "empty", True) and "0q" in trend.index:
        r = trend.loc["0q"]
        out["eps_estimate_trend"] = {"current": _f(r.get("current")),
                                     "d30": _f(r.get("30daysAgo")),
                                     "d90": _f(r.get("90daysAgo"))}
    if rev30 is not None and not getattr(rev30, "empty", True) and "0q" in rev30.index:
        r = rev30.loc["0q"]
        out["revisions_30d"] = {"up": _f(r.get("upLast30days"), 0),
                                "down": _f(r.get("downLast30days"), 0)}
    return out


def _actual_basis(last: dict, fin: dict[str, pd.DataFrame]) -> dict:
    """探测 `epsActual` 走的是 GAAP 还是 non-GAAP —— 与同季 GAAP 稀释 EPS 比对。

    相等 ⇒ actual 是 GAAP，而 estimate 是街面 non-GAAP → surprise 为混口径；
    不等 ⇒ actual 是 non-GAAP，与 estimate 同口径，surprise 可用。
    同季 GAAP 拿不到（报表尚未更新到该季，财报刚发时的常态）⇒ 无法判定，如实说不知道。
    """
    act = last.get("eps_actual")
    q = fin.get("quarterly", pd.DataFrame())
    if act is None or q.empty or "Diluted EPS" not in q.index:
        return {"actual_basis": "unknown"}
    col = [c for c in q.columns if str(c)[:10] == last.get("quarter")]
    if not col:
        return {"actual_basis": "unknown_gaap_quarter_missing"}
    gaap = _f(q.loc["Diluted EPS", col[0]])
    if gaap is None:
        return {"actual_basis": "unknown"}
    same = abs(act - gaap) < 0.005
    return {"actual_basis": "gaap" if same else "non_gaap", "gaap_eps_same_quarter": gaap}


DRIFT_MIN_POINTS = 3    # 少于此观测数不下漂移结论


def _revision_drift(ticker: str, asof: str) -> dict:
    """整季的一致预期修正漂移 —— **数据早就在硬盘上，此前没有任何代码读它**。

    动机（2026-08-28 审计）：`output/{date}/core_inputs.json` 每天都写了 `consensus`
    快照，但只有 `eps_estimate_trend` 的 30d/90d 两个点被看见，**营收的修正漂移
    完全不可见**。而修正方向是财报前信息量最高的公开信号之一（不是 edge——它公开、
    已在价格里——但它是判断「市场预期在往哪走」的分母）。

    口径：按日期升序取每天的 `next_quarter.revenue_avg` / `eps_avg` 与
    `revisions_30d`，报首末值、变动幅度、观测天数。
    ⚠️ **只描述预期怎么变，不预测财报结果**；观测不足 `DRIFT_MIN_POINTS` 天时
    只给原始点、明确说不下结论（打 `DRIFT_THIN`）。
    """
    pts = []
    for p in sorted(Path("output").glob("*/core_inputs.json")):
        day = p.parent.name
        if day > asof:
            continue
        try:
            h = json.loads(p.read_text(encoding="utf-8")).get("holdings", {}).get(ticker) or {}
        except Exception:
            continue
        c = h.get("consensus") or {}
        nq, rv = c.get("next_quarter") or {}, c.get("revisions_30d") or {}
        if not nq:
            continue
        pts.append({"date": day, "revenue_avg": nq.get("revenue_avg"),
                    "eps_avg": nq.get("eps_avg"),
                    "up": rv.get("up"), "down": rv.get("down")})
    if not pts:
        return {}
    out: dict = {"points": pts, "n_days": len(pts),
                 "window": [pts[0]["date"], pts[-1]["date"]]}
    for k in ("revenue_avg", "eps_avg"):
        a, b = pts[0].get(k), pts[-1].get(k)
        if a and b:
            out[f"{k}_first"], out[f"{k}_last"] = a, b
            out[f"{k}_chg_pct"] = round(b / a - 1, 4)
    if len(pts) < DRIFT_MIN_POINTS:
        out["degraded"] = (f"DRIFT_THIN(仅 {len(pts)} 天观测 < {DRIFT_MIN_POINTS}，"
                           f"只列原始点，**不得据此判断修正方向**)")
    return out


def _vix_view(cache) -> tuple[dict, list[str]]:
    """VIX 档位 —— 核心 sleeve 的**唯一**宏观输入。

    为什么是 VIX 而不是完整 macro_score（2026-08-28）：台账 policy 把
    「VIX≥25 恐慌回撤」写成了加速器的第三个扳机，而在此之前核心 sleeve 的输入里
    **一个宏观字段都没有** —— 那条扳机根本无从判定，写了等于没写。
    这里只补它真正需要的：VIX 水平 + 四档制度 + 仓位上限。

    ⚠️ **刻意不算 35% 的 macro_score**：那是战术 sleeve 的量，需要全池价格 + 桶强度，
    只喂 7 只核心名会算出一个被稀释得没有意义的数，再拿去做决策就是自欺。
    核心 sleeve 用 VIX 门控，不用 macro_score —— 报告须照此口径说话。
    """
    try:
        from data.fred_source import FREDSource
        from signals.macro.regime import chan_buy_threshold, classify_vix
        df = FREDSource(cache).get_macro("VIXCLS")
        if df is None or df.empty:
            return {}, ["VIX_UNAVAILABLE(FRED VIXCLS 空 → 加速器的 VIX≥25 扳机本次无法判定)"]
        vix = float(df["value"].dropna().iloc[-1])
        asof = str(df.index[-1])[:10] if hasattr(df.index[-1], "year") else None
        reg = classify_vix(vix)
        return ({"vix": round(vix, 2), "vix_asof": asof, "regime": reg.regime,
                 "position_limit": reg.position_limit,
                 "chan_buy_allowed": chan_buy_threshold(reg),
                 "panic_accelerator": vix >= 25.0}, [])
    except Exception as e:
        return {}, [f"VIX_UNAVAILABLE({type(e).__name__} → 加速器的 VIX≥25 扳机本次无法判定)"]


def _entry_window(chan, piv: dict, price: float | None) -> dict:
    """b3 的理想回踩区（CLAUDE.md 明文）：ZG×0.99 ~ ZG×1.03。

    价已高于上沿 ⇒ 回踩窗口已过 ⇒ 改报 price×0.995~1.005 并打 `b3_window_passed`。
    **「等它跌回 ZG 再买」是错误逻辑**——届时结构已变，b3 大概率消失；
    要么按现价接受，要么放弃本轮。此处只做算术，是否入场由 skill 综合裁决。
    """
    bp, zg = chan.buy_point_type, piv.get("ZG")
    if bp not in ("b3", "lb2") or not zg or not price:
        return {}
    # ⚠️ 必须用**已发布的那个 ZG**（同样 round 到 2 位）来算，不能用原始值：
    # `technical.pivot.ZG` 发出去的是 465.44，若这里拿原始 465.4467 算，
    # 读者按文件里的 ZG×1.03 复现会得到 479.40 而文件写着 479.41——
    # 价位就不可复现了，而「不得手抄日志、须可从文件复现」正是暴露这些字段的理由。
    zg = round(float(zg), 2)
    lo, hi = round(zg * 0.99, 2), round(zg * 1.03, 2)
    passed = price > hi
    out = {"b3_ideal_entry": [lo, hi], "b3_window_passed": passed}
    if passed:
        out["entry_band"] = [round(price * 0.995, 2), round(price * 1.005, 2)]
        out["above_ideal_pct"] = round(price / hi - 1, 4)
    else:
        out["entry_band"] = [lo, hi]
    return out


TACTICAL_MAX_AGE_DAYS = 7      # 战术快照比 asof 旧过此天数即弃用（宁可缺席，不可拿旧裁决拼今天）
TACTICAL_MAX_FUTURE_DAYS = 4   # 目录日=墙钟日，正常会比 asof 晚（周末/NaN 尾行），故须留出前瞻窗口


def _find_tactical(asof: str) -> tuple[dict | None, str | None]:
    """找最近一份战术快照 output/{date}/tactical_snapshot.json。

    ⚠️ 目录名是**墙钟日**（main.py 的 `today_str()`），核心侧的 `asof` 是**最后一根
    有效 K 线日** —— 战术目录**正常地会比 core 的 asof 晚 1~3 天**（周末、或核心名
    命中 NaN 尾行）。所以不能按「目录日 ≤ asof」筛，那会把当天刚跑出来的快照当成
    未来数据丢掉。判定新鲜度看快照里的 `bar_asof`，不看目录名。

    优先取 `bar_asof == asof` 的那一份（真正同一根 K 线）；没有则取目录日最新的一份，
    由调用方打 `TACTICAL_ASOF_MISMATCH`。都没有则返回 (None, None) ——
    **缺席比拼凑好**：核心报告会退回纯核心口径并注明战术侧未运行。
    """
    root = Path("output")
    if not root.is_dir():
        return None, None
    cands: list[tuple[str, Path, dict]] = []
    for d in sorted(root.iterdir(), reverse=True):
        f = d / "tactical_snapshot.json"
        if not f.is_file():
            continue
        try:
            age = (pd.Timestamp(asof) - pd.Timestamp(d.name)).days
        except Exception:
            continue
        if age > TACTICAL_MAX_AGE_DAYS or age < -TACTICAL_MAX_FUTURE_DAYS:
            continue
        try:
            cands.append((d.name, f, json.loads(f.read_text(encoding="utf-8"))))
        except Exception as e:
            logger.warning(f"[Core] 战术快照解析失败 {f}: {e}")
    if not cands:
        return None, None
    for _, f, snap in cands:
        if snap.get("bar_asof") == asof:
            return snap, str(f)
    _, f, snap = cands[0]
    return snap, str(f)


def _tactical_link(asof: str, records: dict) -> tuple[dict, list[str]]:
    """把同一次交易日的战术 sleeve 裁决挂进核心输入，并做四项交叉检查。

    为什么要这一块（2026-08-29 用户提出）：两条管线本来各写各的报告——`今日操作.md`
    给短线、`核心持仓研究.md` 给长线，读者要自己在脑子里合并两份结论。但两者其实
    **共用同一份价格缓存**（`get_price` 同 key、同 800 天窗口），六只核心名在战术侧
    也**已经全量算过一遍缠论**。不合并的代价不是重复劳动，是**两份结论可以静默不一致**
    而没人发现（2026-08-28 的 NaN 尾行事故就同时打中了两侧，MSFT 的 b3 在两边一起消失）。

    ⚠️ **只合并输入与呈现，绝不合并裁决**：两个 sleeve 对 VIX 的方向相反、对
    「结构 vs 基本面」冲突的优先级相反、持有期差两个数量级。这里做的是**对账**
    （as-of 是否同日 / 缠论是否一致 / 两个宏观口径差多少 / 两本账是否重叠），
    不是投票，更不产生任何合成评级。
    """
    snap, src = _find_tactical(asof)
    if snap is None:
        return {}, [f"TACTICAL_SNAPSHOT_MISSING(近 {TACTICAL_MAX_AGE_DAYS} 天内无 "
                    f"tactical_snapshot.json → 本次报告只有长线一侧；"
                    f"先跑 `python main.py` 再跑本预取即可合并)"]

    flags: list[str] = []
    bar = snap.get("bar_asof")
    if bar and bar != asof:
        flags.append(f"TACTICAL_ASOF_MISMATCH(战术侧最后 K 线 {bar} ≠ 核心侧 {asof}"
                     f" → 两侧看的不是同一天，价位与信号不可直接并列)")
    run_date = snap.get("run_date")
    lag = {t: d for t, d in (snap.get("bar_lagging") or {}).items() if t in CORE_HOLDINGS}
    if lag:
        flags.append("TACTICAL_BAR_LAGGING(" + ", ".join(f"{t}@{d}" for t, d in lag.items())
                     + " 在战术侧的最后有效收盘也落后于全池 —— 两条管线共用同一份价格缓存，"
                     "所以这是**同一个数据缺陷同时打中两侧**，不是两侧不一致；"
                     "两边一起错时对账是看不出来的，只能靠这一条)")

    # ── 六只核心名：战术侧已算过一遍缠论，逐名对账 ──
    dec = snap.get("decisions", {}) or {}
    core_rows: dict[str, dict] = {}
    for t in CORE_HOLDINGS:
        row = dec.get(t)
        if not row:
            continue
        tc = row.get("chan") or {}
        cc = (records.get(t, {}) or {}).get("technical") or {}
        diffs = []
        if cc:
            for tk, ck in (("buy_point", "buy_point"), ("sell_point", "sell_point"),
                           ("stroke_confirmed", "stroke_confirmed")):
                if tc.get(tk) != cc.get(ck):
                    diffs.append(f"{tk}: 战术={tc.get(tk)} 核心={cc.get(ck)}")
            ts, cs = tc.get("score"), cc.get("chan_score")
            if ts is not None and cs is not None and abs(float(ts) - float(cs)) > 0.005:
                diffs.append(f"score: 战术={ts} 核心={cs}")
        core_rows[t] = {
            "rating": row.get("rating"),
            "final_score": row.get("final_score"),
            "price": row.get("price"),
            "suggested_position": row.get("suggested_position"),
            "risk_flags": row.get("risk_flags"),
            "score_reasoning": row.get("score_reasoning"),
            "quant_score": row.get("quant_score"),
            "chan": tc or None,
            # 核心名被 main.py 排除出战术买入候选 → 这个评级是**分析结论**，
            # 不是一条可下单的战术指令。核心侧的动作只能由三轴裁决给出。
            "tactical_tradable": bool(row.get("tactical_tradable")),
            "chan_agrees_with_core": (not diffs) if cc else None,
            "chan_diffs": diffs or None,
        }
        if diffs:
            flags.append(f"TACTICAL_CHAN_DISAGREE({t}: " + "; ".join(diffs) +
                         " → 两侧共用同一份价格缓存，出现分歧即为数据或时点漂移，"
                         "须先查清再引用任一侧结构位)")

    # ── 战术侧今日可执行行（非核心名）：短线动作原样透传，不作任何改写 ──
    _BUY_ACT  = {"Buy", "Overweight"}
    _SELL_ACT = {"Sell", "Underweight"}

    def _actionable(r: dict) -> bool:
        """买卖两侧分开判：**卖出侧对所有在战术账本里的票都生效**。

        强制入池的持仓票 `tactical_buyable=false` 但 `tactical_tradable=true`——
        它账本里有真仓位，离场指令必须出现在 §C，那正是它被强制入池的唯一目的。
        早先只判一个合并标，导致这类票转 Sell 时整行在此处被丢弃、指引里看不到离场。
        旧快照无 `tactical_buyable` → 回退到 `tactical_tradable`（旧口径），不炸。
        """
        rt = r.get("rating")
        if not r.get("tactical_tradable"):
            return False
        if rt in _SELL_ACT:
            return True
        if rt in _BUY_ACT:
            return bool(r.get("tactical_buyable", r.get("tactical_tradable")))
        return False

    actionable = [
        {"ticker": t, "rating": r.get("rating"), "final_score": r.get("final_score"),
         "price": r.get("price"), "suggested_position": r.get("suggested_position"),
         "entry_price_range": r.get("entry_price_range"), "stop_loss": r.get("stop_loss"),
         "take_profit": r.get("take_profit"), "risk_flags": r.get("risk_flags"),
         "chan_sell_confirmed": r.get("chan_sell_confirmed"),
         # 只分析不加仓的持仓票也会出现在这里（仅卖出侧）——写明原因，
         # 免得读者把一条离场行误读成"这票可交易、也可以买"。
         "no_buy_reason": r.get("no_buy_reason")}
        for t, r in sorted(dec.items(), key=lambda kv: -(kv[1].get("final_score") or 0))
        if _actionable(r)
    ]

    book = snap.get("book", {}) or {}
    positions = book.get("positions", {}) or {}
    overlap = [t for t in positions if t in CORE_HOLDINGS]
    if overlap:
        flags.append(f"TACTICAL_CORE_OVERLAP({','.join(overlap)} 同时在 paper 持仓与核心池 "
                     f"→ 同名双重敞口，main.py 的核心名排除本应防住这个，须查历史遗留仓)")

    tmacro = snap.get("macro", {}) or {}
    link = {
        "source": src,
        "run_date": run_date,
        "bar_asof": bar,
        "asof_aligned": (bar == asof),
        "generated_at": snap.get("generated_at"),
        # ⚠️ 两本账彼此独立，各自 $100k，**合起来不是一本 70/30 的账**。
        # 战术 30% 是 paper 自身权益的 30%，核心 70% 是真金 total_capital 的 70%。
        "book": {**{k: v for k, v in book.items() if k != "positions"},
                 "positions": positions,
                 "independent_from_core": True},
        # 战术的 35% macro_score ≠ 核心的 VIX 档位口径，两者只可并列不可互换。
        "macro": tmacro,
        "core_names": core_rows,
        "actionable": actionable,
    }
    return link, flags


def _pct(v) -> str:
    """百分比格式化；None/NaN（Yahoo 缺 surprisePercent）返回「未提供」而不是抛 TypeError
    或印出 `nan%`。缺失说「未提供」，不伪装成一个数。"""
    try:
        return "未提供" if v is None or pd.isna(v) else f"{float(v):+.1%}"
    except (TypeError, ValueError):
        return "未提供"


def _f(v, nd: int = 3):
    """None/NaN 安全的取数；nd=0 取整。缺失一律 None，不填 0（0 会被读成真值）。"""
    try:
        if v is None or pd.isna(v):
            return None
        return int(v) if nd == 0 else round(float(v), nd)
    except (TypeError, ValueError):
        return None


def main() -> int:
    cache = SQLiteCache()
    yfs = YFinanceSource(cache)
    edgar = EdgarSource(cache)
    ledger = _load_ledger()
    gates = _load_gates()

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=_PRICE_DAYS)).strftime("%Y-%m-%d")

    prices = {t: yfs.get_price(t, start, end) for t in CORE_HOLDINGS}
    # as-of = 最后一根**有效**收盘（取全核心池最新），不是原始最后一行。
    # ⚠️ Yahoo 会返回只有 Volume、OHLC 全 NaN 的未完成尾行（memory:
    # insight_yf_nan_tail_bar）。用原始 index[-1] 会让 `asof` 比价格晚一天——
    # 报告日期与 `price` 字段来自不同的两天，且与战术侧 `bar_asof` 无谓打架。
    _last_valid = {}
    for t, df in prices.items():
        if df is None or df.empty:
            continue
        cl = df["Close"].dropna()
        if not cl.empty:
            _last_valid[t] = pd.Timestamp(cl.index[-1]).strftime("%Y-%m-%d")
    # 用**众数**而非 max：Yahoo 的填充是逐票到的，个别票先拿到新 OHLC 时取 max
    # 会把整池的 as-of 定成那一两只票的日期、再把其余全体报成「落后」。
    _counts: dict[str, int] = {}
    for d in _last_valid.values():
        _counts[d] = _counts.get(d, 0) + 1
    date_str = max(_counts, key=lambda d: (_counts[d], d)) if _counts else None
    # 有效收盘与 as-of 不同的核心名：其 price/缠论末笔不是 as-of 当日的，须逐名可见
    price_offbar = {t: d for t, d in sorted(_last_valid.items()) if d != date_str} \
        if date_str else {}
    if date_str is None:
        logger.error("[Core] 全部核心票无价格数据，退出")
        return 1

    records: dict[str, dict] = {}
    core_bands: dict[str, dict] = {}
    core_caps: dict[str, float] = {}
    degraded: list[str] = []

    for t in CORE_HOLDINGS:
        df = prices.get(t)
        price = _last_close(df)
        rec: dict = {"ticker": t, "price": price}

        # ── 技术/缠论状态（只读复用，不改 55% 本体）──────
        close = df["Close"].dropna() if df is not None and not df.empty else pd.Series(dtype=float)
        if len(close) >= 200:                     # 按**有效**收盘数计门，非原始行数
            chan = compute_chan_signal(t, prices)
            sma200 = float(close.rolling(200).mean().iloc[-1])
            # ⚠️ **只读暴露，不改缠论 55% 本体的任何判定逻辑**（沿用 R6.2 边界）。
            # 动机（2026-08-28）：ZG/ZD、结构止损、R、定笔状态本来就已算出，却只进
            # DEBUG 日志——skill 因此**无法从自己的输入文件算出 b3 理想回踩区**，
            # 之前那份报告里的入场带是我从日志行手抄的，不可复现也不可核验。
            piv = chan.current_pivot or {}
            rec["technical"] = {
                "chan_score": round(float(chan.score), 3) if hasattr(chan, "score") else None,
                "buy_point": chan.buy_point_type,
                "sell_point": chan.sell_point_type,
                "divergence": bool(chan.divergence),
                "weekly_trend": chan.weekly_trend,
                "trend_type": chan.trend_type,
                "level_resonance": int(chan.level_resonance),
                "confidence": round(float(chan.confidence), 3),
                "sma200": round(sma200, 2),
                "dev_200dma": round(price / sma200 - 1, 4) if price else None,
                "atr_pct": round(float(chan.atr_pct), 4),
                # 右端稳定性三层防护的**当前状态**（未定笔 = 结构上禁止交易，如财报反应日）
                "stroke_confirmed": bool(chan.stroke_confirmed),
                "fractal_stop": bool(chan.fractal_stop),
                "high_vol": bool(float(chan.atr_pct) >= HIGH_VOL_PCT),
                # 中枢与结构止损
                "pivot": {k: round(float(v), 2) for k, v in piv.items()
                          if k in ("ZD", "ZG", "mid") and v is not None} or None,
                "stop_loss": round(float(chan.stop_loss), 2) if chan.stop_loss else None,
                "r_ratio": round(float(chan.r_ratio), 4) if chan.r_ratio else None,
            }
            rec["technical"].update(_entry_window(chan, piv, price))

        if t == "QQQ":
            records[t] = rec   # 估值代理在个股跑完后补
            continue

        # ── Filings + 财务（EDGAR 优先 → yfinance 回退）──
        filings = edgar.get_filings(t)
        rec["filings"] = filings.to_dict("records") if not filings.empty else []
        latest = filings["date"].iloc[0] if not filings.empty else None
        stale = EdgarSource.staleness(t, latest)
        if stale:
            degraded.append(stale)
        fin = edgar.get_financials(t)
        rec["financials"] = _fin_summary(fin)

        # ── 估值带 + 财报日历（info 直取：需要分析师目标价，仅 7 名，无缓存压力）──
        try:
            yft = yf.Ticker(t)
            info = yft.info or {}
        except Exception as e:
            logger.warning(f"[Core] info 失败 {t}: {e}")
            info, yft = {}, None
        band = valuation_band(t, df if df is not None else pd.DataFrame(), fin, info,
                              filings=filings)
        rec["valuation"] = band
        core_bands[t] = band
        # 个股估值降级须并入顶层清单，否则报告的「数据降级」看着干净、实则无公允价带
        degraded += [f"{t}:{d}" for d in band.get("degraded", [])]
        if info.get("marketCap"):
            core_caps[t] = float(info["marketCap"])
        try:
            cal = yft.calendar if yft is not None else {}
            ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if ed:
                rec["next_earnings"] = str(ed[0])
        except Exception:
            pass

        # ── 一致预期 / surprise（纯暴露，不进决策）──────────
        cons = _consensus(yft, fin)
        drift = _revision_drift(t, date_str)
        if drift:
            cons["revision_drift"] = drift
            if drift.get("degraded"):
                degraded.append(f"{t}:{drift['degraded']}")
        rec["consensus"] = cons
        nq = cons.get("next_quarter") or {}
        eps_n = nq.get("eps_n")
        if eps_n is not None and eps_n < CONSENSUS_THIN_N:
            degraded.append(
                f"{t}:CONSENSUS_THIN(下季 EPS 一致预期仅 {eps_n} 位分析师覆盖，"
                f"非真街面共识；营收口径 n={nq.get('revenue_n')} 更可用)")
        if not cons.get("last_report"):
            degraded.append(f"{t}:CONSENSUS_UNAVAILABLE(无 earnings_history，无法算 surprise)")
        # ── 财报前裁决表（只校验完整性，命中判定在 skill）──
        gview, gflags = _gate_view(gates.get(t), rec.get("next_earnings"))
        if gview:
            rec["earnings_gate"] = gview
        degraded += [f"{t}:{f}" for f in gflags]

        # actual 走 GAAP + 该票一次性损益超标 ⇒ surprise 是 GAAP 实际撞 non-GAAP 预期的假象
        oneoff = band.get("oneoff_share")
        # 报表滞后数周，财报刚发时同季 GAAP 拿不到 ⇒ 口径判不出 ⇒ SURPRISE_MIXED_BASIS 无法触发。
        # 而这**正是**该护栏要防的窗口（GOOGL/AMZN 的 +200% 假象就诞生在这几周里）。
        # 判不出时必须显式说「判不出」，否则 degraded 是干净的，报告会读成「口径没问题」。
        if cons.get("actual_basis", "").startswith("unknown") and cons.get("last_report"):
            degraded.append(
                f"{t}:SURPRISE_BASIS_UNKNOWN(同季 GAAP EPS 尚不可得，无法判定 epsActual 走 "
                f"GAAP 还是 non-GAAP → **surprise 不可解读**，不得当作超预期幅度；"
                f"报表更新到该季后本标自动消失)")
        if cons.get("actual_basis") == "gaap" and abs(oneoff or 0) > ONEOFF_FLAG_THRESHOLD:
            lr = cons["last_report"]
            degraded.append(
                f"{t}:SURPRISE_MIXED_BASIS(epsActual {lr['eps_actual']} 为 GAAP、"
                f"epsEstimate {lr['eps_estimate']} 为街面 non-GAAP，而该票一次性损益占 "
                f"{oneoff:.1%} → surprise {_pct(lr['surprise_pct'])} 是口径假象，"
                f"**不得解读为超预期幅度**)")

        rec["ledger"] = _ledger_stats(ledger, t, price)
        records[t] = rec

    # ── QQQ 指数级估值代理（依赖个股 premium）──────────────
    qdf = prices.get("QQQ")
    if qdf is not None and not qdf.empty:
        qband = qqq_valuation(qdf, core_bands, core_caps)
        records["QQQ"]["valuation"] = qband
        records["QQQ"]["ledger"] = _ledger_stats(ledger, "QQQ", _last_close(qdf))
        degraded += [f"QQQ:{d}" for d in qband.get("degraded", [])]

    degraded += edgar.degraded
    total_invested = sum(r.get("ledger", {}).get("invested", 0) or 0 for r in records.values())
    total_mv = sum(r.get("ledger", {}).get("market_value", 0) or 0 for r in records.values())
    pending = sum(r.get("ledger", {}).get("cost_pending_shares", 0) or 0 for r in records.values())
    total_capital = ledger.get("total_capital")
    core_target_usd = total_capital * CORE_TARGET_FRAC if total_capital else None
    portfolio = {
        "core_target_frac": CORE_TARGET_FRAC,
        "base_floor_frac": BASE_FLOOR_FRAC,
        "total_capital": total_capital,
        "core_target_usd": core_target_usd,
        "core_invested": round(total_invested, 2),
        "core_market_value": round(total_mv, 2),
        # 两个口径不可混用：built=已投入**资金**占目标的比例（决定还要投多少钱）；
        # weight=当前**市值**占目标的比例（配置占位，持仓上涨会推高它但并未多投一分钱）。
        "core_built_frac": (round(total_invested / core_target_usd, 4)
                            if core_target_usd and total_invested else None),
        "core_weight_frac": (round(total_mv / core_target_usd, 4)
                             if core_target_usd else None),
        "capital_remaining_usd": (round(core_target_usd - total_invested, 2)
                                  if core_target_usd and total_invested else None),
        "ledger_filled": any(ledger["positions"][t]["fills"] for t in CORE_HOLDINGS
                             if t in ledger.get("positions", {})),
        # 成本类结论（摊低均价/浮盈/高抛低吸配对）的前置条件
        "cost_basis_complete": pending == 0,
        "cost_pending_shares": pending,
    }
    if price_offbar:
        degraded.append(
            "PRICE_BAR_OFFSET(" + ", ".join(f"{t}@{d}" for t, d in price_offbar.items())
            + f" 的最后有效收盘 ≠ as-of {date_str} —— 多为 Yahoo 的 NaN 尾行占位"
            "（有 Volume 无 OHLC，逐票填充故快慢不一），它会**静默改写缠论末笔**；"
            "这些名的价位与结构位不是 as-of 当日的，不得与其他名并列比较)")
    macro, mflags = _vix_view(cache)
    if macro:
        portfolio["macro"] = macro
    degraded += mflags

    # ── 建仓政策（用户配置意图；缺 policy 则整块缺席，skill 退回旧口径）──
    policy = _policy_view(ledger, records, date_str)
    if policy:
        degraded += [f"POLICY:{d}" for d in policy.pop("degraded", [])]
        portfolio["policy"] = policy
        for t, row in policy.get("per_name", {}).items():
            if t in records and row.get("target_usd"):
                records[t].setdefault("ledger", {}).update(
                    {k: row[k] for k in ("target_usd", "gap_usd", "built_frac") if k in row})
        if policy.get("target_sum_usd") and core_target_usd and \
                abs(policy["target_sum_usd"] - core_target_usd) > 1.0:
            # 逐名目标之和须等于 core_target_usd，否则两个口径会各说各话
            degraded.append(
                f"POLICY:TARGET_SUM_MISMATCH(逐名合计 {policy['target_sum_usd']} "
                f"≠ core_target_usd {core_target_usd})")

    # ── 战术 sleeve 对账（缺席则整块不出现，报告退回纯核心口径）──
    tactical, tflags = _tactical_link(date_str, records)
    degraded += tflags

    out_dir = Path("output") / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "portfolio": portfolio,
        "holdings": records,
        "degraded": sorted(set(degraded)),
    }
    if tactical:
        payload["tactical"] = tactical
    out_path = out_dir / "core_inputs.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    logger.info(f"[Core] 预取完成 → {out_path}（degraded {len(set(degraded))} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
