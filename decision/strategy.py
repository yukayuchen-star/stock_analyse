from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

from signals.chan.chan_signal import ChanSignalResult
from signals.quant.factor_engine import QuantSignalResult
from signals.macro.macro_signal import MacroSignalResult


@dataclass
class StockDecision:
    ticker: str
    rating: str = "Hold"                        # Buy/Overweight/Hold/Underweight/Sell
    final_score: float = 0.0                    # -1~1
    suggested_position: float = 0.0            # 0~1
    chan_signal: Optional[ChanSignalResult] = None
    quant_signal: Optional[QuantSignalResult] = None
    macro_signal: Optional[MacroSignalResult] = None
    risk_flags: List[str] = field(default_factory=list)
    entry_price_range: Tuple[float, float] = (0.0, 0.0)
    stop_loss: float = 0.0
    take_profit: float = 0.0
