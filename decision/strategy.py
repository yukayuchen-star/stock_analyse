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

    # 实际生效的三引擎权重（R4.1，ScorerOutput 透传）：
    # 标准 0.55/0.35/0.10；缠强量弱背离票 0.70/0.20/0.10——报告按此渲染，不再硬编码
    chan_weight:        float = 0.55
    macro_weight:       float = 0.35
    quant_weight:       float = 0.10
    divergence_applied: bool  = False

    # 辅助信息
    risk_flags:      List[str] = field(default_factory=list)
    score_reasoning: str       = ""

    # 迟滞层(B)裁定：缠论卖点连续 CONFIRM_DAYS 天确认（或 VIX panic 直通）才为 True，
    # 组合层仅在确认后才执行卖点清仓（apply_hysteresis 填写）
    chan_sell_confirmed: bool = False


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
        chan_macro_state=scored.chan_macro_state,
    )

    # R_MAX 超标时降级评级（risk_overlay 已将仓位清零，同步评级避免显示矛盾）
    if any("R_MAX_EXCEEDED" in f for f in risk.risk_flags):
        if rating in ("Buy", "Overweight"):
            rating = "Hold"

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
        chan_weight=scored.chan_weight,
        macro_weight=scored.macro_weight,
        quant_weight=scored.quant_weight,
        divergence_applied=scored.divergence_applied,
        risk_flags=risk.risk_flags,
        score_reasoning=scored.reasoning,
    )
