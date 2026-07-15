"""
P5 决策层 — 五档评级映射

  Buy         ≥ +0.50   （R4.2：0.60→0.50。旧标下 chan 上限 0.55×0.75=0.41，
                          Buy 需 macro≥+0.54 几乎不可达；新标定下
                          b3(0.75)+宏观顺风(≥+0.25) 恰可达 Buy，语义=强结构×环境确认）
  Overweight  +0.30 ~ +0.50
  Hold        -0.30 ~ +0.30
  Underweight -0.60 ~ -0.30
  Sell        < -0.60   （卖侧未动：卖点分值未重标定，无对称依据）

VIX 门控上限：
  panic  → 最高 Hold（禁止开多）
  tense  → 最高 Overweight
  neutral/calm → 无限制
"""
from __future__ import annotations

_THRESHOLDS = [
    ( 0.50, "Buy"),
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
