"""
P5 决策层 — 三引擎得分合成（缠论 × 宏观 双主轴并行）

设计依据（2026-05-25 ML 回测实证）：
  - 缠论买点 79.8% 历史胜率，是最强择时信号 → 主轴一
  - 宏观（VIX/利率/油价）特征重要性 81.7%，决定胜率环境 → 主轴二
  - 量化因子是弱预测器，且与缠论的边反相关 → 降为横截面配角

标准：final_score = 0.50×chan + 0.30×macro + 0.20×quant
背离：chan↑ quant↓ 时以缠论为准 → 0.65×chan + 0.25×macro + 0.10×quant

并行一致性检测（缠论↔宏观互验）：
  共振   chan≥+0.30 且 macro≥0      → 双主轴看多，信号更可信
  逆风   chan≥+0.30 且 macro≤-0.15  → 缠论想买但宏观环境敌对，需警惕
"""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np

from signals.chan.chan_signal     import ChanSignalResult
from signals.quant.factor_engine import QuantSignalResult
from signals.macro.macro_signal  import MacroSignalResult


# 标准权重：缠论 + 宏观 = 80% 双主轴，量化 20% 横截面配角
W_CHAN  = 0.50
W_QUANT = 0.20
W_MACRO = 0.30

# 背离权重：缠强量弱时进一步加重缠论
W_CHAN_DIV  = 0.65
W_QUANT_DIV = 0.10
W_MACRO_DIV = 0.25

DIV_CHAN_MIN  =  0.30
DIV_QUANT_MAX = -0.10

# 缠论↔宏观并行一致性阈值
RESONANCE_CHAN_MIN = 0.30
HEADWIND_MACRO_MAX = -0.15


@dataclass
class ScorerOutput:
    final_score:        float
    chan_weight:        float
    quant_weight:       float
    macro_weight:       float
    divergence_applied: bool
    chan_macro_state:   str    # "resonance" | "headwind" | "neutral"
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
        rule = "标准(缠论×宏观双主轴)"

    final = float(np.clip(wc * chan_s + wq * quant_s + wm * macro_s, -1.0, 1.0))

    # 缠论↔宏观并行一致性（不重复计分，仅标记供风控与报告使用）
    if chan_s >= RESONANCE_CHAN_MIN and macro_s >= 0:
        cm_state = "resonance"
        cm_note  = " | 缠论×宏观共振↑"
    elif chan_s >= RESONANCE_CHAN_MIN and macro_s <= HEADWIND_MACRO_MAX:
        cm_state = "headwind"
        cm_note  = " | ⚠️宏观逆风(缠论看多但环境敌对)"
    else:
        cm_state = "neutral"
        cm_note  = ""

    reasoning = (
        f"{rule}: {wc:.0%}×chan({chan_s:+.2f}) + "
        f"{wm:.0%}×macro({macro_s:+.2f}) + "
        f"{wq:.0%}×quant({quant_s:+.2f}) = {final:+.3f}{cm_note}"
    )

    return ScorerOutput(
        final_score=final,
        chan_weight=wc,
        quant_weight=wq,
        macro_weight=wm,
        divergence_applied=divergence,
        chan_macro_state=cm_state,
        reasoning=reasoning,
    )
