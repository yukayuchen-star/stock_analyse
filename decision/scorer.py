"""
P5 决策层 — 三引擎得分合成

标准：final_score = 0.40×chan + 0.40×quant + 0.20×macro
背离：chan↑ quant↓ 时以缠论为准 → 0.65×chan + 0.15×quant + 0.20×macro
"""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np

from signals.chan.chan_signal     import ChanSignalResult
from signals.quant.factor_engine import QuantSignalResult
from signals.macro.macro_signal  import MacroSignalResult


W_CHAN  = 0.40
W_QUANT = 0.40
W_MACRO = 0.20

W_CHAN_DIV  = 0.65
W_QUANT_DIV = 0.15
W_MACRO_DIV = 0.20

DIV_CHAN_MIN  =  0.30
DIV_QUANT_MAX = -0.10


@dataclass
class ScorerOutput:
    final_score:        float
    chan_weight:        float
    quant_weight:       float
    macro_weight:       float
    divergence_applied: bool
    reasoning:          str


def compute_final_score(
    chan:  ChanSignalResult,
    quant: QuantSignalResult,
    macro: MacroSignalResult,
) -> ScorerOutput:
    chan_s  = float(np.clip(chan.score,  -1.0, 1.0))
    quant_s = float(np.clip(quant.score, -1.0, 1.0))
    macro_s = float(np.clip(macro.score, -1.0, 1.0))

    divergence = (chan_s >= DIV_CHAN_MIN and quant_s <= DIV_QUANT_MAX)

    if divergence:
        wc, wq, wm = W_CHAN_DIV, W_QUANT_DIV, W_MACRO_DIV
        rule = "缠强量弱→加权缠论"
    else:
        wc, wq, wm = W_CHAN, W_QUANT, W_MACRO
        rule = "标准三引擎"

    final = float(np.clip(wc * chan_s + wq * quant_s + wm * macro_s, -1.0, 1.0))

    reasoning = (
        f"{rule}: {wc:.0%}×chan({chan_s:+.2f}) + "
        f"{wq:.0%}×quant({quant_s:+.2f}) + "
        f"{wm:.0%}×macro({macro_s:+.2f}) = {final:+.3f}"
    )

    return ScorerOutput(
        final_score=final,
        chan_weight=wc,
        quant_weight=wq,
        macro_weight=wm,
        divergence_applied=divergence,
        reasoning=reasoning,
    )
