"""
P5 决策层 — 三引擎得分合成（缠论 × 宏观 双主轴并行）

设计依据（2026-05-25 ML 回测；⚠️ 2026-07-09 部分被推翻，见下）：
  - 缠论买点当时测得 79.8% 胜率 → 定为主轴一。
    2026-07-09 修复回测幸存者偏差(R1.3，被重画的失败笔曾从统计中消失)后重测：
    缠论规则 53.2% < 随机 55.5%；P7 分类型仅 b3(≈53%)尚可、b1/b2 弱(≈35%)。
  - 2026-07-14 R4.2 重标定落地：不动 55/35/10 顶层权重（无新证据支持特定替代值），
    改在缠论类型内重分配——BUY_SCORES b3=0.75/b2=0.40/b1=0.35（chan_signal.py），
    弱类型自然拉低 final_score；DIV_CHAN_MIN 0.30→0.45，「结构优先」背离加权
    只为 b3 级强结构保留（b1/b2 含趋势加权最高 0.40，不再触发）。
  - 宏观（VIX/利率/油价）特征重要性 81.7%，决定胜率环境 → 主轴二
  - 量化动量 ML 与缠论捕捉相反的边（ML高置信区缠论胜率反而更低）→ 横截面配角

标准：final_score = 0.55×chan + 0.35×macro + 0.10×quant
背离：chan↑ quant↓ 时以缠论为准 → 0.70×chan + 0.20×macro + 0.10×quant

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


# 标准权重：缠论 + 宏观 = 90% 双主轴，量化 10% 横截面配角
W_CHAN  = 0.55
W_QUANT = 0.10
W_MACRO = 0.35

# 背离权重：缠强量弱时进一步加重缠论
W_CHAN_DIV  = 0.70
W_QUANT_DIV = 0.10
W_MACRO_DIV = 0.20

# R4.2：0.30→0.45，背离加权仅对 b3(0.75) 级强结构生效；
# b1/b2 重标定后 ≤0.40（b1×趋势加权1.15=0.4025），弱结构不配触发「结构>统计」。
DIV_CHAN_MIN  =  0.45
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
