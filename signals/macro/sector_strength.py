from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger


def compute_bucket_ir(
    bucket_name: str,
    tickers: list[str],
    prices: dict[str, pd.DataFrame],
    lookback: int = 60,
) -> tuple[float, float]:
    """
    计算桶相对 QQQ 的 Information Ratio，返回 (ir, score)。

    IR = mean(daily_excess) / std(daily_excess) × sqrt(252)
    score = clip(ir / 0.5, -1, 1)  —— IR ±0.5 → ±1

    Args:
        lookback: 回看交易日数（默认60，约3个月）
    Returns:
        (ir, score)  ir 为年化 IR，score 在 [-1, 1]
    """
    qqq_df = prices.get("QQQ")
    if qqq_df is None or qqq_df.empty:
        logger.warning(f"[Macro] QQQ 价格缺失，桶 {bucket_name} IR 设为 0")
        return 0.0, 0.0

    # 收集桶内有效股票的收益率
    member_rets: list[pd.Series] = []
    for t in tickers:
        df = prices.get(t)
        if df is None or df.empty or "Close" not in df.columns:
            continue
        close = df["Close"].dropna()
        if len(close) < lookback + 5:
            continue
        r = close.pct_change().iloc[-(lookback + 1):]
        member_rets.append(r.rename(t))

    if not member_rets:
        logger.warning(f"[Macro] 桶 {bucket_name} 无有效成员，IR=0")
        return 0.0, 0.0

    # 等权桶收益率
    bucket_ret = pd.concat(member_rets, axis=1).dropna(how="all").mean(axis=1)

    # QQQ 对齐
    qqq_close = qqq_df["Close"].dropna()
    qqq_ret   = qqq_close.pct_change().iloc[-(lookback + 1):]

    # 对齐共同日期
    common = bucket_ret.index.intersection(qqq_ret.index)
    if len(common) < 10:
        logger.warning(f"[Macro] 桶 {bucket_name} 共同日期不足")
        return 0.0, 0.0

    excess = (bucket_ret.loc[common] - qqq_ret.loc[common]).dropna()
    if excess.std() < 1e-9:
        return 0.0, 0.0

    ir    = float(excess.mean() / excess.std() * np.sqrt(252))
    score = float(np.clip(ir / 0.5, -1.0, 1.0))

    logger.debug(f"[Macro] {bucket_name}: IR={ir:.3f} score={score:+.2f}")
    return ir, score


def compute_all_bucket_ir(
    buckets: dict[str, list[str]],
    prices: dict[str, pd.DataFrame],
    lookback: int = 60,
) -> tuple[dict[str, float], dict[str, float]]:
    """
    批量计算所有桶的 IR 和 score。
    Returns:
        (bucket_ir, bucket_scores)
    """
    bucket_ir:     dict[str, float] = {}
    bucket_scores: dict[str, float] = {}

    for bname, tickers in buckets.items():
        ir, score = compute_bucket_ir(bname, tickers, prices, lookback)
        bucket_ir[bname]     = round(ir,    4)
        bucket_scores[bname] = round(score, 4)

    return bucket_ir, bucket_scores
