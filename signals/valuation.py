"""
R9.3 核心持仓公允价区间（多法三角，非单点）。

主口径 = trailing P/E 历史分位（诚实：历史 forward P/E 序列无免费官方源）：
  由年/季 Diluted EPS 构建 TTM EPS 时间线（按「财报可得日」滞后对齐，无前视），
  除进日收盘价得 trailing P/E 日序列，取 {25/50/75/90} 分位 × 当前 TTM EPS → 价格带。
交叉验证（不作依赖）：PEG-implied 隐含价、分析师目标价带、当前 forward P/E 快照。

带语义（PRD R9.3）：
  floor=p25（深折价，加大 tranche）｜mid=p50（估值门：≤mid 才累积）
  ceiling=p75（增强层高抛区起点）｜extreme=p90（底仓极端高估 trim 触发）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

REPORT_LAG_DAYS = 65     # 期末→财报可得的保守滞后（10-Q ~4-6 周、10-K ~8 周）
PEG_ANCHOR      = 1.25   # PEG=1~1.5 锚取中值
_PE_PCTS        = {"floor": 25, "mid": 50, "ceiling": 75, "extreme": 90}


def _last_close(price_df: pd.DataFrame) -> float | None:
    """最后一根**有效**收盘（Yahoo 尾行可为未完成 NaN K）。"""
    if price_df is None or price_df.empty:
        return None
    s = price_df["Close"].dropna()
    return float(s.iloc[-1]) if not s.empty else None


def availability_date(period_end: pd.Timestamp,
                      filings: pd.DataFrame | None) -> tuple[pd.Timestamp, str]:
    """期末 → 该期 EPS「真正可得日」。返回 (日期, 口径 tier)。

    三档降级：
      ① `filing`      —— 期末后 120 天内最早的 10-Q/10-K 实际申报日（最准）。
      ② `earnings_8k` —— 无 10-Q/10-K 时，取期末后 10~60 天内最早的 8-K：这个窗口里的 8-K
         几乎必是财报发布（EPS 实际公开时点，甚至早于 10-Q）。yfinance 只回 15 条披露，
         GOOGL/MSFT 的近期 10-Q 常被截断在外，仅此档能把它们对到真实时点。
      ③ `lag_model`   —— 都拿不到才回退固定 REPORT_LAG_DAYS。
    固定 65 天滞后对成长股偏差极大：GOOGL 实际 7/22 发布、模型算到 9/3，会把最新一期
    TTM 判成「尚不可得」而用上一季 EPS 定价（13.11 vs 19.91，pe_now 虚高 ~50%）。
    """
    if filings is not None and not filings.empty and {"form", "date"} <= set(filings.columns):
        d = pd.to_datetime(filings["date"], errors="coerce")
        forms = filings["form"]
        rep = d[forms.isin(("10-Q", "10-K"))].dropna()
        rep = rep[(rep > period_end) & (rep <= period_end + pd.Timedelta(days=120))]
        if not rep.empty:
            return rep.min(), "filing"
        ek = d[forms == "8-K"].dropna()
        ek = ek[(ek >= period_end + pd.Timedelta(days=10))
                & (ek <= period_end + pd.Timedelta(days=60))]
        if not ek.empty:
            return ek.min(), "earnings_8k"
    return period_end + pd.Timedelta(days=REPORT_LAG_DAYS), "lag_model"


def ttm_eps_series(fin: dict[str, pd.DataFrame],
                   filings: pd.DataFrame | None = None) -> pd.Series:
    """由年/季 Diluted EPS 构建 TTM EPS 序列，index=可得日（真实申报日优先），升序。

    季度足 4 期滚动求和；年度 EPS 直接作为该财年末的 TTM 点（补足季度覆盖不到的早年）。
    `series.attrs['real_dates']` 记有多少点用上了真实申报日（其余为滞后模型近似）。
    """
    points: dict[pd.Timestamp, float] = {}
    tiers: list[str] = []
    a = fin.get("annual", pd.DataFrame())
    if not a.empty and "Diluted EPS" in a.index:
        for col, v in a.loc["Diluted EPS"].items():
            if pd.notna(v):
                d, tier = availability_date(pd.Timestamp(col), filings)
                points[d] = float(v)
                tiers.append(tier)
    q = fin.get("quarterly", pd.DataFrame())
    if not q.empty and "Diluted EPS" in q.index:
        s = q.loc["Diluted EPS"].dropna()
        s.index = pd.to_datetime(s.index)
        s = s.sort_index()
        ttm = s.rolling(4).sum().dropna()
        for end, v in ttm.items():   # 季度 TTM 更细，覆盖同期时以其为准（后写覆盖）
            d, tier = availability_date(end, filings)
            points[d] = float(v)
            tiers.append(tier)
    if not points:
        return pd.Series(dtype=float)
    out = pd.Series(points).sort_index()
    out.attrs["approx_points"] = sum(t == "lag_model" for t in tiers)
    out.attrs["total_points"] = len(tiers)
    return out


def trailing_pe_series(close: pd.Series, ttm: pd.Series) -> pd.Series:
    """逐日 trailing P/E = close / 当日已可得的最新 TTM EPS（as-of 对齐，无前视）。"""
    if ttm.empty:
        return pd.Series(dtype=float)
    eps_daily = ttm.reindex(close.index.union(ttm.index)).ffill().reindex(close.index)
    pe = close / eps_daily
    return pe.replace([np.inf, -np.inf], np.nan).dropna()
    # 注：EPS≤0 期间产生的负 P/E 由调用方分位数天然边缘化（核心 mega-cap 均盈利，实际不触发）


ONEOFF_FLAG_THRESHOLD = 0.15   # 一次性占报告 EPS 超此比例即打降级标


def normalized_eps(fin: dict[str, pd.DataFrame]) -> dict:
    """营业利润口径的正常化 TTM EPS —— 剥离一次性非经营损益。

    动机（2026-08-21 实测）：trailing P/E 用**报告** EPS，而净利润可被巨额非经营损益灌水。
    GOOGL 26Q2 净利 $112.19B 高于当季毛利 $73.85B（税前 $138.75B vs 营业利润 $40.77B，
    缺口 ~$98B 为投资/权益重估），使 TTM EPS 19.91 里 **50.9% 非经营**；AMZN 同为 50.0%。
    结果 pe_now 被压到 17.1× / 20.9×，报出「折价 40~49%」，而按营业利润重算实为
    **溢价 21.7%** / 恰在公允价——方向完全反了。此函数把该失真显式量化。

    口径：正常化净利 = 四季营业利润 × (1 − 四季有效税率)；
          摊薄股数 = 同一季的 净利润 ÷ 稀释EPS（必须同季配对，跨季配对会算出错误股数）。
    诚实边界：营业口径会剔除掉部分**真实**价值（如长期持股重估确有价值），
              其作用是剥离不可持续的年度波动，不是否认这些收益存在。

    ⚠️ 窗口错配护栏（2026-08-27 加，AMZN 连续命中）：yfinance 各行的季度覆盖**可以不齐**——
    AMZN 的 Diluted EPS 回到 2026-06-30，而 Operating Income/Pretax/Tax 只回到 2026-03-31。
    此时报告 TTM EPS 与正常化 TTM **错开一个季度**，`oneoff_share` 把「真一次性损益」与
    「一个季度的增长」混在一起（AMZN 因此报 50.0%，同窗口重算实为 ~25.7%）。
    检出即写 `normalization_window_mismatch`（含同窗口重算的 `oneoff_share_same_window`），
    由 `valuation_band` 翻成 `NORMALIZATION_WINDOW_MISMATCH` 降级标。**只暴露不修正**：
    缺的那季营业利润无从补，此处不臆造。
    """
    out: dict = {}
    q = fin.get("quarterly", pd.DataFrame())
    need = ("Operating Income", "Pretax Income", "Tax Provision", "Net Income", "Diluted EPS")
    if q.empty or not all(r in q.index for r in need):
        return out

    op = q.loc["Operating Income"].dropna()[:4]
    pre = q.loc["Pretax Income"].dropna()[:4]
    tax = q.loc["Tax Provision"].dropna()[:4]
    ni, eps = q.loc["Net Income"].dropna(), q.loc["Diluted EPS"].dropna()
    if len(op) < 4 or pre.empty or tax.empty:
        return out

    common = [c for c in ni.index if c in eps.index and eps[c]]   # 股数必须同季配对
    if not common:
        return out
    c0 = max(common)
    shares = float(ni[c0]) / float(eps[c0])
    etr = float(tax.sum()) / float(pre.sum()) if float(pre.sum()) else None
    if not shares or shares <= 0 or etr is None or not (0.0 <= etr <= 0.5):
        return out

    out["eps_ttm_normalized"] = round(float(op.sum()) * (1 - etr) / shares, 3)
    out["normalized_basis"] = {
        "operating_income_ttm": round(float(op.sum()), 2),
        "effective_tax_rate": round(etr, 4),
        "diluted_shares": round(shares, 2),
        "quarters": [str(c)[:10] for c in op.index],
        "shares_quarter": str(c0)[:10],
    }

    eps_end, op_end = pd.Timestamp(max(eps.index)), pd.Timestamp(max(op.index))
    out["normalized_basis"]["eps_window_end"] = str(eps_end)[:10]
    if eps_end != op_end:
        mm = {"op_end": str(op_end)[:10], "eps_end": str(eps_end)[:10],
              "offset_quarters": round((eps_end - op_end).days / 91.31)}
        same = [c for c in op.index if c in eps.index]   # 与营业利润同窗口的报告 EPS
        rep = float(eps[same].sum()) if len(same) == len(op) else 0.0
        if rep > 0:
            mm["eps_ttm_reported_same_window"] = round(rep, 3)
            mm["oneoff_share_same_window"] = round(1 - out["eps_ttm_normalized"] / rep, 3)
        out["normalization_window_mismatch"] = mm
    return out


def valuation_band(ticker: str, price_df: pd.DataFrame, fin: dict[str, pd.DataFrame],
                   info: dict, filings: pd.DataFrame | None = None) -> dict:
    """公允价带 + 折溢价 + 交叉验证。数据不足时字段如实缺席（degraded），不臆造。"""
    out: dict = {"ticker": ticker, "method": "trailing_pe_percentile", "degraded": []}
    price = _last_close(price_df)
    out["price"] = price

    ttm = ttm_eps_series(fin, filings)
    pe_hist = trailing_pe_series(price_df["Close"], ttm) if not price_df.empty else pd.Series(dtype=float)
    # EPS 口径须与 pe_hist 同一 vintage：pe_hist 是逐日 as-of，若这里直接取最新一期
    # （其可得日可能还在未来），就成了 pe_now 用新 EPS、pe_hist 用旧 EPS，
    # pe_percentile_now 会在财报后被系统性压低——skill 警告的「估值压缩」假象的机械版。
    asof = ttm[ttm.index <= pd.Timestamp.now()] if not ttm.empty else pd.Series(dtype=float)
    eps_now = float(asof.iloc[-1]) if not asof.empty else info.get("trailingEps")
    if not ttm.empty:
        approx, tot = ttm.attrs.get("approx_points", 0), ttm.attrs.get("total_points", 0)
        out["eps_asof_date"] = str(asof.index[-1].date()) if not asof.empty else None
        if len(asof) < len(ttm):
            out["eps_pending_report"] = round(float(ttm.iloc[-1]), 3)
            out["degraded"].append("EPS_VINTAGE_LAGGED(新一期尚未到可得日，带用上一期 TTM)")
        if approx:
            # 早年点无真实申报日、退回 65 天滞后模型，比近年点更「滞后」；成长股会因此
            # 令历史 P/E 偏高、当前分位偏低 → 分位读数须打折看，不可作唯一裁决。
            out["degraded"].append(f"EPS_DATE_APPROX({approx}/{tot} 点用滞后模型近似)")

    if len(pe_hist) >= 250 and eps_now and eps_now > 0:
        pcts = {k: float(np.nanpercentile(pe_hist, p)) for k, p in _PE_PCTS.items()}
        out["pe_band"] = {k: round(v, 2) for k, v in pcts.items()}
        out["band"] = {k: round(v * eps_now, 2) for k, v in pcts.items()}
        out["pe_now"] = round(price / eps_now, 2) if price else None
        out["pe_percentile_now"] = round(float((pe_hist < price / eps_now).mean() * 100), 1) if price else None
        out["premium_vs_mid"] = round(price / out["band"]["mid"] - 1, 4) if price else None
        out["pe_window_days"] = int(len(pe_hist))
        out["eps_ttm"] = round(float(eps_now), 3)
        # ⚠️ pe_window_days 是**天数**，会给人「样本很大」的错觉：分位的真实自由度只有
        # EPS 观测点数（yfinance 仅回 5 季 + 4 年 ≈ 6 个 TTM 点）。且 2023–26 这些名 EPS
        # 数倍增长，历史 P/E 是「当期价 ÷ 更旧更小的 EPS」→ 系统性偏高，今天必然落在低分位。
        # 这正是 skill 要防的「成长股结构性 P/E 压缩」，此处把不可靠性显式量化，不让分位独断。
        out["eps_points"] = int(len(ttm))
        if len(ttm) >= 2 and float(ttm.iloc[0]) > 0:
            out["eps_growth_window"] = round(float(ttm.iloc[-1]) / float(ttm.iloc[0]), 2)
        if out["eps_points"] < 8 or (out.get("eps_growth_window") or 1) > 2.0:
            out["degraded"].append(
                f"PE_PCTL_UNRELIABLE(EPS 观测仅 {out['eps_points']} 点"
                f"/窗口内增长 {out.get('eps_growth_window')}×，分位偏低系结构性压缩，"
                f"须以 PEG 隐含价与分析师带裁决)")
    else:
        out["degraded"].append(f"PE_BAND_UNAVAILABLE(pe_days={len(pe_hist)},eps={eps_now})")

    # ── 营业利润口径正常化（一次性损益检查，优先于历史带采信）────
    norm = normalized_eps(fin)
    if norm and norm.get("eps_ttm_normalized", 0) > 0:
        out.update(norm)
        ne = norm["eps_ttm_normalized"]
        if price:
            out["pe_now_normalized"] = round(price / ne, 2)
        if eps_now and eps_now > 0:
            out["oneoff_share"] = round(1 - ne / float(eps_now), 3)
        if price and out.get("pe_band"):
            # 与历史 mid 对比。⚠️ 历史带的分母同样是报告 EPS（含当年一次性），非严格同口径，
            # 故此值是「方向性修正」而非精确公允价——但足以翻转折价/溢价的符号判断。
            out["normalized_vs_mid"] = round(price / (ne * out["pe_band"]["mid"]) - 1, 4)
        if abs(out.get("oneoff_share") or 0) > ONEOFF_FLAG_THRESHOLD:
            out["degraded"].append(
                f"EPS_ONEOFF_INFLATED(报告 EPS 中 {out['oneoff_share']:.1%} 为非经营损益，"
                f"pe_now={out.get('pe_now')} 失真；须以 pe_now_normalized="
                f"{out.get('pe_now_normalized')} 裁决)")
        mm = norm.get("normalization_window_mismatch")
        if mm:
            # 阈值判定仍用混窗口的 oneoff_share（保守：不因窗口对齐而撤掉污染警告），
            # 同窗口重算值只作上下界参考——缺季的营业利润无从补，真值落在两者之间。
            same = ""
            if "oneoff_share_same_window" in mm:
                same = f"；同窗口重算一次性占比 {mm['oneoff_share_same_window']:.1%}"
                if out.get("oneoff_share") is not None:
                    same += f"（报告口径 {out['oneoff_share']:.1%}，真值介于两者之间）"
            out["degraded"].append(
                f"NORMALIZATION_WINDOW_MISMATCH(报告 EPS 窗口至 {mm['eps_end']}、"
                f"正常化窗口至 {mm['op_end']}，错开 {mm['offset_quarters']} 季 → "
                f"pe_now_normalized/normalized_vs_mid/oneoff_share 三者同受影响，"
                f"盈利增长期内 pe_now_normalized 偏高){same}")
    else:
        out["degraded"].append("EPS_NORMALIZATION_UNAVAILABLE(缺营业利润/税项季度序列)")

    # ── 交叉验证（不作依赖）──────────────────────────────
    growth = info.get("earningsGrowth") or info.get("revenueGrowth")
    if growth and growth > 0 and eps_now and eps_now > 0:
        fair_pe = float(np.clip(min(growth, 0.50) * 100 * PEG_ANCHOR, 10, 60))
        out["peg_implied_price"] = round(fair_pe * eps_now, 2)
    tgt = {k: info.get(k) for k in ("targetLowPrice", "targetMeanPrice", "targetHighPrice")}
    if tgt.get("targetMeanPrice"):
        out["analyst_targets"] = {k: round(float(v), 2) for k, v in tgt.items() if v}
        out["analyst_n"] = info.get("numberOfAnalystOpinions")
    if info.get("forwardPE"):
        out["forward_pe_now"] = round(float(info["forwardPE"]), 2)
    return out


def qqq_valuation(price_df: pd.DataFrame, core_bands: dict[str, dict],
                  core_caps: dict[str, float]) -> dict:
    """QQQ 指数级估值代理（PRD R9.2 降级三重代理，无公司 filings）：
    ①6 核心成分股 premium_vs_mid 的市值加权均值；②价格对 200DMA 乖离的历史分位；
    ③无 thesis 减仓语义——仅估值带择时。"""
    out: dict = {"ticker": "QQQ", "method": "index_proxy", "degraded": []}
    price = _last_close(price_df)
    out["price"] = price

    prem, wsum, n_norm = 0.0, 0.0, 0
    for t, band in core_bands.items():
        # 成分折溢价优先用正常化口径：否则 GOOGL/AMZN 的一次性损益失真会被加权放大进指数代理
        p = band.get("normalized_vs_mid")
        if p is None:
            p = band.get("premium_vs_mid")
        else:
            n_norm += 1
        w = core_caps.get(t)
        if p is not None and w:
            prem += p * w
            wsum += w
    if wsum > 0:
        out["core_weighted_premium"] = round(prem / wsum, 4)
        out["core_premium_normalized_n"] = n_norm
    else:
        out["degraded"].append("CORE_PREMIUM_UNAVAILABLE")

    close = price_df["Close"].dropna() if not price_df.empty else pd.Series(dtype=float)
    if len(close) >= 400:            # 按**有效**收盘数计门，非原始行数（否则 SMA200 可为 NaN）
        dev = close / close.rolling(200).mean() - 1
        dev = dev.dropna()
        out["dev_200dma_now"] = round(float(dev.iloc[-1]), 4)
        out["dev_200dma_percentile"] = round(float((dev < dev.iloc[-1]).mean() * 100), 1)
        # 乖离历史分位 25/50/75/90 → 对应价格带（近似 floor/mid/ceiling/extreme）
        sma = float(close.rolling(200).mean().iloc[-1])
        out["band"] = {k: round(sma * (1 + float(np.nanpercentile(dev, p))), 2)
                       for k, p in _PE_PCTS.items()}
        out["premium_vs_mid"] = round(price / out["band"]["mid"] - 1, 4) if price else None
    else:
        out["degraded"].append("DEV200_UNAVAILABLE")
    return out
