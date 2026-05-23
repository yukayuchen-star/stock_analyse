from __future__ import annotations
from typing import Dict, List

import numpy as np
import pandas as pd


def compute_relative_strength_score(
    ticker: str,
    prices: Dict[str, pd.DataFrame],
    bucket_tickers: List[str],
    lookback: int = 20,
) -> tuple[float, dict]:
    """
    相对强度得分 (-1 ~ 1)。

    子分项：
      vs QQQ   50%  — 个股 20 日超额收益 vs QQQ（科技股基准）
      桶内排名  35%  — 同桶横截面 Z-score（Qlib 式横截面选股）
      vs SPY   15%  — 个股 20 日超额收益 vs SPY（大市基准）

    桶内只有 1 只股票时桶排名自动归零。
    """
    df = prices.get(ticker)
    if df is None or df.empty:
        return 0.0, {}

    close = df["Close"].dropna()
    if len(close) < lookback + 1:
        return 0.0, {}

    stock_ret = float(close.iloc[-1] / close.iloc[-lookback] - 1)

    # ── vs QQQ ───────────────────────────────────────────
    vs_qqq = 0.0
    qqq_ret = _bench_ret("QQQ", prices, lookback)
    if qqq_ret is not None:
        excess_qqq = stock_ret - qqq_ret
        vs_qqq = float(np.clip(excess_qqq / 0.06, -1, 1))  # 6% 超额 → ±1

    # ── vs SPY ───────────────────────────────────────────
    vs_spy = 0.0
    spy_ret = _bench_ret("SPY", prices, lookback)
    if spy_ret is not None:
        excess_spy = stock_ret - spy_ret
        vs_spy = float(np.clip(excess_spy / 0.06, -1, 1))

    # ── 桶内横截面 Z-score ────────────────────────────────
    bucket_score = 0.0
    peers = [
        t for t in bucket_tickers
        if t in prices and not prices[t].empty
        and len(prices[t]["Close"].dropna()) >= lookback + 1
    ]
    if len(peers) >= 2:
        peer_rets = np.array([
            float(prices[t]["Close"].dropna().iloc[-1] / prices[t]["Close"].dropna().iloc[-lookback] - 1)
            for t in peers
        ])
        std = peer_rets.std()
        if std > 0:
            idx = peers.index(ticker) if ticker in peers else -1
            if idx >= 0:
                z = (peer_rets[idx] - peer_rets.mean()) / std
                bucket_score = float(np.clip(z / 1.5, -1, 1))  # Z±1.5 → ±1

    # ── 合成 ─────────────────────────────────────────────
    score = 0.50 * vs_qqq + 0.35 * bucket_score + 0.15 * vs_spy

    indicators = {
        f"ret_{lookback}d": stock_ret,
        "vs_qqq_excess":    stock_ret - (qqq_ret or 0),
        "vs_spy_excess":    stock_ret - (spy_ret or 0),
        "bucket_z":         bucket_score * 1.5,
    }

    return float(np.clip(score, -1, 1)), indicators


def _bench_ret(symbol: str, prices: Dict[str, pd.DataFrame], lookback: int) -> float | None:
    df = prices.get(symbol)
    if df is None or df.empty:
        return None
    c = df["Close"].dropna()
    if len(c) < lookback + 1:
        return None
    return float(c.iloc[-1] / c.iloc[-lookback] - 1)
