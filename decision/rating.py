"""
P5 决策层 — 五档评级映射

  Buy         ≥ +0.60
  Overweight  +0.30 ~ +0.60
  Hold        -0.30 ~ +0.30
  Underweight -0.60 ~ -0.30
  Sell        < -0.60

VIX 门控上限：
  panic  → 最高 Hold（禁止开多）
  tense  → 最高 Overweight
  neutral/calm → 无限制
"""
from __future__ import annotations

_THRESHOLDS = [
    ( 0.60, "Buy"),
    ( 0.30, "Overweight"),
    (-0.30, "Hold"),
    (-0.60, "Underweight"),
]

_RATING_ORDER = ["Sell", "Underweight", "Hold", "Overweight", "Buy"]

_VIX_CAP = {
    "calm":    "Buy",
    "neutral": "Buy",
    "tense":   "Overweight",
    "panic":   "Hold",
}


def score_to_rating(final_score: float, vix_regime: str) -> str:
    raw = "Sell"
    for threshold, label in _THRESHOLDS:
        if final_score >= threshold:
            raw = label
            break

    cap = _VIX_CAP.get(vix_regime, "Overweight")
    if _RATING_ORDER.index(raw) > _RATING_ORDER.index(cap):
        return cap
    return raw
