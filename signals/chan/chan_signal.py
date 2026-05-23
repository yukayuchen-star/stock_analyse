from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Dict
import pandas as pd


@dataclass
class ChanSignalResult:
    ticker: str
    timestamp: pd.Timestamp
    buy_point_type: Optional[str] = None   # "1buy" | "2buy" | "3buy"
    sell_point_type: Optional[str] = None  # "1sell" | "2sell" | "3sell"
    level_resonance: int = 0               # 共振级别数 0–3
    current_pivot: Optional[Dict] = None   # {"ZG": float, "ZD": float, "level": str}
    last_stroke_direction: str = "unknown" # "up" | "down"
    score: float = 0.0                     # -1~1（正多负空）
    confidence: float = 0.0               # 0~1
    reasoning: str = ""


def placeholder_chan_signal(ticker: str) -> ChanSignalResult:
    """Neutral placeholder — replaced when Chan module (P4) is implemented."""
    return ChanSignalResult(
        ticker=ticker,
        timestamp=pd.Timestamp.now(),
        score=0.0,
        confidence=0.0,
        reasoning="[缠论模块 P4 待实现]",
    )
