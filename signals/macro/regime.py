from __future__ import annotations
from dataclasses import dataclass


@dataclass
class VIXRegime:
    level: float
    regime: str          # "calm" | "neutral" | "tense" | "panic"
    position_limit: float
    score: float         # -1~1：对 macro_score 的 VIX 贡献


def classify_vix(vix: float) -> VIXRegime:
    """
    VIX 四档制度（来自 CLAUDE.md）：
      <15   calm    100%仓位上限  score=+0.50
      15-25 neutral  70%          score= 0.00
      25-35 tense    40%          score=-0.50
      >35   panic     0%          score=-1.00
    """
    if vix < 15:
        return VIXRegime(vix, "calm",    1.00, +0.50)
    elif vix < 25:
        return VIXRegime(vix, "neutral", 0.70,  0.00)
    elif vix < 35:
        return VIXRegime(vix, "tense",   0.40, -0.50)
    else:
        return VIXRegime(vix, "panic",   0.00, -1.00)


def chan_buy_threshold(regime: VIXRegime) -> list[str]:
    """返回当前 VIX 制度下可接受的缠论买点类型。"""
    if regime.regime == "calm":
        return ["b1", "b2", "b3"]
    elif regime.regime == "neutral":
        return ["b1", "b2"]
    elif regime.regime == "tense":
        return ["b1_multi"]   # 1买+多级共振
    else:
        return []             # 全部观望
