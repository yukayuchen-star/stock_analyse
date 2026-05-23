from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from signals.quant.fundamental import compute_fundamental_score
from signals.quant.trend       import compute_trend_score
from signals.quant.momentum    import compute_momentum_score
from signals.quant.relative    import compute_relative_strength_score
from signals.quant.volume      import compute_volume_score


# ── 权重（与 quant.md 五层架构对应）─────────────────────────
W_FUND = 0.15   # Layer1 基本面质量
W_TREND = 0.25  # Layer2 趋势方向
W_MOM   = 0.30  # Layer3 买点动量（与缠论择时结合的核心）
W_REL   = 0.20  # 相对强度（横截面选股）
W_VOL   = 0.10  # 量价配合


@dataclass
class QuantSignalResult:
    """
    量化选股引擎输出：系统化多因子横截面评分。

    五组子因子（权重）：
      基本面   15%  — Revenue/EPS Growth, ROE, Gross Margin, D/E, PEG
      趋势因子 25%  — SMA/EMA 位置排列 + ADX
      动量因子 30%  — ROC20/MACD/RSI14/KAMA + Pullback/Breakout 信号
      相对强度 20%  — vs QQQ/SPY + 桶内横截面 Z-score
      量价因子 10%  — OBV 趋势 + VWMA 偏离
    """

    ticker: str
    indicators: Dict[str, float] = field(default_factory=dict)

    # 子因子得分（-1~1）
    fundamental_score:       float = 0.0
    trend_score:             float = 0.0
    momentum_score:          float = 0.0
    relative_strength_score: float = 0.0
    volume_score:            float = 0.0

    # 合成得分
    score:     float = 0.0       # -1~1，正多负空
    trend:     str   = "neutral" # "up" | "down" | "neutral"
    reasoning: str   = ""


# ── 主函数 ────────────────────────────────────────────────────

def compute_quant_signal(
    ticker: str,
    prices: Dict[str, pd.DataFrame],
    bucket_tickers: List[str],
    info: Optional[dict] = None,
) -> QuantSignalResult:
    """
    为单只股票计算完整量化因子评分。

    Args:
        ticker:         目标股票代码
        prices:         全股票池价格字典 {ticker: df}（含 QQQ/SPY）
        bucket_tickers: 同桶股票列表（含 ticker 自身），用于横截面对比
        info:           yfinance Ticker.info 基本面字段（可为 None）
    """
    df = prices.get(ticker)
    if df is None or df.empty:
        logger.warning(f"[Quant] 无价格数据: {ticker}")
        return QuantSignalResult(ticker=ticker, reasoning="无价格数据")

    all_ind: Dict[str, float] = {}

    def _run(name: str, fn, *args):
        try:
            score, ind = fn(*args)
            all_ind.update({f"{name}_{k}": v for k, v in ind.items() if isinstance(v, (int, float))})
            return float(score)
        except Exception as e:
            logger.warning(f"[Quant] {name} error [{ticker}]: {e}")
            return 0.0

    f = _run("fund",  compute_fundamental_score,       ticker, info or {})
    t = _run("trend", compute_trend_score,              df)
    m = _run("mom",   compute_momentum_score,           df)
    r = _run("rel",   compute_relative_strength_score,  ticker, prices, bucket_tickers)
    v = _run("vol",   compute_volume_score,             df)

    score = float(np.clip(
        W_FUND * f + W_TREND * t + W_MOM * m + W_REL * r + W_VOL * v,
        -1, 1,
    ))

    trend = "up" if score >= 0.25 else ("down" if score <= -0.25 else "neutral")

    reasoning = (
        f"fund={f:+.2f}({W_FUND:.0%}) "
        f"trend={t:+.2f}({W_TREND:.0%}) "
        f"mom={m:+.2f}({W_MOM:.0%}) "
        f"rel={r:+.2f}({W_REL:.0%}) "
        f"vol={v:+.2f}({W_VOL:.0%}) "
        f"→ score={score:+.3f} [{trend}]"
    )

    return QuantSignalResult(
        ticker=ticker,
        indicators=all_ind,
        fundamental_score=f,
        trend_score=t,
        momentum_score=m,
        relative_strength_score=r,
        volume_score=v,
        score=score,
        trend=trend,
        reasoning=reasoning,
    )


def placeholder_quant_signal(ticker: str) -> QuantSignalResult:
    return QuantSignalResult(ticker=ticker, reasoning="[无数据占位]")
