from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class QuantSignalResult:
    """
    量化选股引擎输出：系统化多因子横截面评分。

    四组子因子（内部权重）：
      趋势因子  35%  — SMA/EMA/ADX
      动量因子  35%  — ROC/MACD/RSI/KAMA
      相对强度  20%  — vs QQQ + 桶内 Z-score
      量价因子  10%  — OBV/VWMA
    """

    ticker: str
    indicators: Dict[str, float] = field(default_factory=dict)

    # 子因子得分（-1~1）
    trend_score: float = 0.0
    momentum_score: float = 0.0
    relative_strength_score: float = 0.0
    volume_score: float = 0.0

    # 合成得分
    score: float = 0.0      # -1~1，正多负空
    trend: str = "neutral"  # "up" | "down" | "neutral"
    reasoning: str = ""


def placeholder_quant_signal(ticker: str) -> QuantSignalResult:
    """中性占位——P2 实现后替换。"""
    return QuantSignalResult(
        ticker=ticker,
        reasoning="[量化因子模块 P2 待实现]",
    )
