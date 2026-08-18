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


def ttm_eps_series(fin: dict[str, pd.DataFrame]) -> pd.Series:
    """由年/季 Diluted EPS 构建 TTM EPS 序列，index=可得日（期末+滞后），升序。

    季度足 4 期滚动求和；年度 EPS 直接作为该财年末的 TTM 点（补足季度覆盖不到的早年）。
    """
    points: dict[pd.Timestamp, float] = {}
    a = fin.get("annual", pd.DataFrame())
    if not a.empty and "Diluted EPS" in a.index:
        for col, v in a.loc["Diluted EPS"].items():
            if pd.notna(v):
                points[pd.Timestamp(col) + pd.Timedelta(days=REPORT_LAG_DAYS)] = float(v)
    q = fin.get("quarterly", pd.DataFrame())
    if not q.empty and "Diluted EPS" in q.index:
        s = q.loc["Diluted EPS"].dropna()
        s.index = pd.to_datetime(s.index)
        s = s.sort_index()
        ttm = s.rolling(4).sum().dropna()
        for end, v in ttm.items():   # 季度 TTM 更细，覆盖同期时以其为准（后写覆盖）
            points[end + pd.Timedelta(days=REPORT_LAG_DAYS)] = float(v)
    if not points:
        return pd.Series(dtype=float)
    return pd.Series(points).sort_index()


def trailing_pe_series(close: pd.Series, ttm: pd.Series) -> pd.Series:
    """逐日 trailing P/E = close / 当日已可得的最新 TTM EPS（as-of 对齐，无前视）。"""
    if ttm.empty:
        return pd.Series(dtype=float)
    eps_daily = ttm.reindex(close.index.union(ttm.index)).ffill().reindex(close.index)
    pe = close / eps_daily
    return pe.replace([np.inf, -np.inf], np.nan).dropna()
    # 注：EPS≤0 期间产生的负 P/E 由调用方分位数天然边缘化（核心 mega-cap 均盈利，实际不触发）


def valuation_band(ticker: str, price_df: pd.DataFrame,
                   fin: dict[str, pd.DataFrame], info: dict) -> dict:
    """公允价带 + 折溢价 + 交叉验证。数据不足时字段如实缺席（degraded），不臆造。"""
    out: dict = {"ticker": ticker, "method": "trailing_pe_percentile", "degraded": []}
    price = _last_close(price_df)
    out["price"] = price

    ttm = ttm_eps_series(fin)
    pe_hist = trailing_pe_series(price_df["Close"], ttm) if not price_df.empty else pd.Series(dtype=float)
    eps_now = float(ttm.iloc[-1]) if not ttm.empty else info.get("trailingEps")

    if len(pe_hist) >= 250 and eps_now and eps_now > 0:
        pcts = {k: float(np.nanpercentile(pe_hist, p)) for k, p in _PE_PCTS.items()}
        out["pe_band"] = {k: round(v, 2) for k, v in pcts.items()}
        out["band"] = {k: round(v * eps_now, 2) for k, v in pcts.items()}
        out["pe_now"] = round(price / eps_now, 2) if price else None
        out["pe_percentile_now"] = round(float((pe_hist < price / eps_now).mean() * 100), 1) if price else None
        out["premium_vs_mid"] = round(price / out["band"]["mid"] - 1, 4) if price else None
        out["pe_window_days"] = int(len(pe_hist))
        out["eps_ttm"] = round(float(eps_now), 3)
    else:
        out["degraded"].append(f"PE_BAND_UNAVAILABLE(pe_days={len(pe_hist)},eps={eps_now})")

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

    prem, wsum = 0.0, 0.0
    for t, band in core_bands.items():
        p, w = band.get("premium_vs_mid"), core_caps.get(t)
        if p is not None and w:
            prem += p * w
            wsum += w
    if wsum > 0:
        out["core_weighted_premium"] = round(prem / wsum, 4)
    else:
        out["degraded"].append("CORE_PREMIUM_UNAVAILABLE")

    if len(price_df) >= 400:
        close = price_df["Close"].dropna()
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
