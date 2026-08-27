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
from signals.chan.chan_signal import compute_chan_signal
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
    if next_earnings and ed and str(next_earnings)[:10] != str(ed):
        flags.append(f"GATES_STALE(表针对 {ed}，而下次财报是 {str(next_earnings)[:10]}"
                     f"→ 该季已过，须为新一季写表)")
    if not gate.get("locked"):
        flags.append("GATES_UNLOCKED(locked≠true，表未锁定，可能被事后修改)")
    view = dict(gate)
    view["days_to_earnings"] = None
    if ed:
        try:
            view["days_to_earnings"] = (pd.Timestamp(ed) - pd.Timestamp.today().normalize()).days
        except Exception:
            pass
    view["integrity"] = "ok" if not flags else "degraded"
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
        for q, r in hist.tail(8).iloc[::-1].iterrows():      # 新→旧
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
    date_str = None
    for df in prices.values():
        if df is not None and not df.empty:
            date_str = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
            break
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
            rec["technical"] = {
                "chan_score": round(float(chan.score), 3) if hasattr(chan, "score") else None,
                "buy_point": chan.buy_point_type,
                "sell_point": chan.sell_point_type,
                "weekly_trend": chan.weekly_trend,
                "trend_type": chan.trend_type,
                "sma200": round(sma200, 2),
                "dev_200dma": round(price / sma200 - 1, 4) if price else None,
                "atr_pct": round(float(chan.atr_pct), 4),
            }

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
        if cons.get("actual_basis") == "gaap" and abs(oneoff or 0) > ONEOFF_FLAG_THRESHOLD:
            lr = cons["last_report"]
            degraded.append(
                f"{t}:SURPRISE_MIXED_BASIS(epsActual {lr['eps_actual']} 为 GAAP、"
                f"epsEstimate {lr['eps_estimate']} 为街面 non-GAAP，而该票一次性损益占 "
                f"{oneoff:.1%} → surprise {lr['surprise_pct']:+.1%} 是口径假象，"
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

    out_dir = Path("output") / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "portfolio": portfolio,
        "holdings": records,
        "degraded": sorted(set(degraded)),
    }
    out_path = out_dir / "core_inputs.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    logger.info(f"[Core] 预取完成 → {out_path}（degraded {len(set(degraded))} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
