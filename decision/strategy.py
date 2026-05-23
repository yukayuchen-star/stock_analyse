"""
P5 决策层主模块

make_decision() 将三引擎信号合并为 StockDecision：
  scorer → 得分合成（含缠强量弱背离规则）
  rating → 五档评级（含 VIX 门控）
  risk_overlay → 仓位/止损/入场区间/风险标签
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from signals.chan.chan_signal     import ChanSignalResult
from signals.quant.factor_engine import QuantSignalResult
from signals.macro.macro_signal  import MacroSignalResult

from decision.scorer       import compute_final_score
from decision.rating       import score_to_rating
from decision.risk_overlay import apply_risk_overlay


@dataclass
class StockDecision:
    ticker: str

    # 综合评级
    rating:             str   = "Hold"    # Buy/Overweight/Hold/Underweight/Sell
    final_score:        float = 0.0       # -1~1

    # 仓位与价格
    suggested_position: float = 0.0      # 0~1（已叠加 VIX 上限）
    entry_price_range:  Tuple[float, float] = (0.0, 0.0)
    stop_loss:          float = 0.0
    take_profit:        float = 0.0

    # 原始信号引用
    chan_signal:  Optional[ChanSignalResult]  = None
    quant_signal: Optional[QuantSignalResult] = None
    macro_signal: Optional[MacroSignalResult] = None

    # 辅助信息
    risk_flags:      List[str] = field(default_factory=list)
    score_reasoning: str       = ""


def make_decision(
    ticker: str,
    chan:   ChanSignalResult,
    quant:  QuantSignalResult,
    macro:  MacroSignalResult,
    prices: Dict[str, pd.DataFrame],
) -> StockDecision:
    df = prices.get(ticker)
    current_price = float(df["Close"].iloc[-1]) if df is not None and not df.empty else 0.0

    scored = compute_final_score(chan, quant, macro)
    rating = score_to_rating(scored.final_score, macro.vix_regime)
    risk   = apply_risk_overlay(
        final_score=scored.final_score,
        chan=chan,
        macro=macro,
        current_price=current_price,
        divergence_applied=scored.divergence_applied,
    )

    return StockDecision(
        ticker=ticker,
        rating=rating,
        final_score=scored.final_score,
        suggested_position=risk.suggested_position,
        entry_price_range=risk.entry_price_range,
        stop_loss=risk.stop_loss,
        take_profit=risk.take_profit,
        chan_signal=chan,
        quant_signal=quant,
        macro_signal=macro,
        risk_flags=risk.risk_flags,
        score_reasoning=scored.reasoning,
    )
