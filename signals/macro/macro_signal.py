from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict
import pandas as pd


@dataclass
class MacroSignalResult:
    timestamp: pd.Timestamp
    vix_level: float = 0.0
    vix_regime: str = "unknown"      # "calm" | "neutral" | "tense" | "panic"
    position_limit: float = 0.7      # 默认中性
    fed_rate: float = 0.0
    sector_rankings: Dict[str, int] = field(default_factory=dict)
    score: float = 0.0               # -1~1
    reasoning: str = ""


def placeholder_macro_signal() -> MacroSignalResult:
    """Neutral placeholder — replaced in P3."""
    return MacroSignalResult(
        timestamp=pd.Timestamp.now(),
        vix_regime="unknown",
        position_limit=0.7,
        reasoning="[宏观信号模块 P3 待实现]",
    )
