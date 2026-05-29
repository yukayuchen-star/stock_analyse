"""
P5 决策层 — 风控叠加层

1. VIX 仓位门控（macro.position_limit）
2. VIX tense 时缠论买点约束（仅1买+多级共振有效）
3. 止损/止盈（基于 VIX 制度的止损比例 + 2:1 R/R）
4. 入场价格区间（优先缠论中枢结构）
5. 风险标签汇总
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Tuple

from signals.chan.chan_signal    import ChanSignalResult
from signals.macro.macro_signal import MacroSignalResult

# 各 VIX 制度下的止损比例（从当前价格起算）
_STOP_PCT = {
    "calm":    0.07,
    "neutral": 0.08,
    "tense":   0.06,   # 高波动期收紧止损
    "panic":   0.05,
}
_TP_RATIO = 2.0        # 止盈 = 止损 × 2（2:1 风险回报）


@dataclass
class RiskOverlay:
    suggested_position: float
    entry_price_range:  Tuple[float, float]
    stop_loss:          float
    take_profit:        float
    risk_flags:         List[str] = field(default_factory=list)


def apply_risk_overlay(
    final_score:        float,
    chan:                ChanSignalResult,
    macro:              MacroSignalResult,
    current_price:      float,
    divergence_applied: bool,
    chan_macro_state:   str = "neutral",
) -> RiskOverlay:
    flags: List[str] = []
    vix_regime     = macro.vix_regime
    position_limit = macro.position_limit

    # ── 仓位计算 ──────────────────────────────────────────────
    if vix_regime == "panic":
        flags.append("VIX_PANIC: 全市场恐慌，禁止开新仓")
        suggested_pos = 0.0
    else:
        raw_pos = max(0.0, final_score)

        # VIX tense：缠论买点须为 b1 且有多级共振才算有效
        if vix_regime == "tense" and chan.buy_point_type is not None:
            if chan.buy_point_type != "b1" or chan.level_resonance < 2:
                flags.append(
                    f"VIX_TENSE_CHAN: 高波动下仅接受b1+多级共振"
                    f"（当前={chan.buy_point_type} res={chan.level_resonance}）"
                )
                raw_pos *= 0.5

        if vix_regime == "tense":
            flags.append("VIX_TENSE: 高波动，严控仓位")

        suggested_pos = round(min(raw_pos, position_limit), 2)

    # ── 风险标签 ──────────────────────────────────────────────
    if divergence_applied:
        flags.append("CHAN_QUANT_DIV: 缠论↑量化↓，结构信号优先")

    # 缠论↔宏观并行一致性（macro_s 已计入得分，此处仅标记不重复扣仓）
    if chan_macro_state == "resonance":
        flags.append("CHAN_MACRO_RESONANCE: 缠论×宏观双主轴共振，信号更可信")
    elif chan_macro_state == "headwind":
        flags.append("MACRO_HEADWIND: 缠论看多但宏观环境敌对，谨慎建仓")

    if chan.weekly_trend == "down" and final_score > 0:
        flags.append("WEEKLY_DOWN: 周线下跌，多头信号已折半")

    if getattr(chan, "atr_pct", 0.0) >= 0.06:
        flags.append(f"HIGH_VOL: 日均振幅{chan.atr_pct:.0%}，日线结构噪声大、信号可信度低")

    # ── 止损/止盈 ─────────────────────────────────────────────
    stop_pct = _STOP_PCT.get(vix_regime, 0.08)
    if final_score > 0 and current_price > 0:
        stop_loss   = round(current_price * (1 - stop_pct), 2)
        take_profit = round(current_price * (1 + stop_pct * _TP_RATIO), 2)
    else:
        stop_loss = take_profit = 0.0

    # ── 入场区间（优先缠论中枢） ──────────────────────────────
    entry_range = _entry_range(chan, current_price)

    return RiskOverlay(
        suggested_position=suggested_pos,
        entry_price_range=entry_range,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_flags=flags,
    )


def _entry_range(chan: ChanSignalResult, price: float) -> Tuple[float, float]:
    pivot = chan.current_pivot
    btype = chan.buy_point_type

    if pivot:
        zd, zg = pivot["ZD"], pivot["ZG"]
        if btype == "b2":
            return (round(zd, 2), round((zd + zg) / 2, 2))
        elif btype == "b3":
            return (round(zg * 0.99, 2), round(zg * 1.03, 2))

    if price > 0:
        return (round(price * 0.995, 2), round(price * 1.005, 2))
    return (0.0, 0.0)
