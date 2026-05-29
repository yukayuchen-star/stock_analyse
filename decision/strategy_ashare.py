"""
A 股纯缠论决策层

美股 decision/strategy.py 强耦合 VIX 门控 + 量化轴；A 股无对应数据，故另建
轻量决策：评级/仓位/入场区间/止损止盈/R 比率全部由缠论信号 + 板块属性派生。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pandas as pd

from signals.chan.chan_signal import ChanSignalResult
from data.ashare_loader import board_limit
from config.stocks_ashare import (
    BOARD_POSITION_CAP, BUY_SCORE_MIN, WATCH_SCORE_MIN, TP_MULT,
    RISK_BUDGET, R_MAX,
)


@dataclass
class AShareDecision:
    code: str
    board: str = "main"

    rating: str = "Hold"          # Buy / Watch / Hold / Avoid
    score:  float = 0.0
    confidence: float = 0.0

    buy_point:  Optional[str] = None   # b2/b3/b1
    sell_point: Optional[str] = None

    current_price:     float = 0.0
    entry_price_range: Tuple[float, float] = (0.0, 0.0)
    chase_ceiling:     float = 0.0   # 不追上限：使 R 达 R_MAX 的价位（=止损/(1−R_MAX)），高于此则放弃
    stop_loss:         float = 0.0
    take_profit:       float = 0.0
    r_ratio:           Optional[float] = None
    suggested_position: float = 0.0

    weekly:     str = "neutral"
    trend_type: str = "none"
    pivot:      Optional[dict] = None

    reasoning: str = ""
    chan: Optional[ChanSignalResult] = None


def _rating(chan: ChanSignalResult) -> str:
    bp = chan.buy_point_type
    r  = chan.r_ratio
    # 结构止损离入场太远（R/R 差）→ 不追，降级为观察
    too_far = r is not None and r > R_MAX
    if bp in ("b2", "b3") and chan.score >= BUY_SCORE_MIN and not too_far:
        return "Buy"
    if bp and chan.score > WATCH_SCORE_MIN:
        return "Watch"            # 含被削分的 b1 / 弱 b2b3 / 止损过远的 b3 / 类二买 lb2
                                  # lb2 回测仅 42% 胜率 → 只观察不自动买（Watch-only）
    if chan.sell_point_type:
        return "Avoid"
    return "Hold"


def make_ashare_decision(
    code: str,
    chan: ChanSignalResult,
    df: pd.DataFrame,
    board: str = "main",
) -> AShareDecision:
    price = float(df["Close"].iloc[-1]) if df is not None and not df.empty else 0.0
    rating = _rating(chan)

    # 止损：缠论结构止损价；以板块单日跌停为参考下界（一字跌停实际无法成交）
    stop = float(chan.stop_loss) if chan.stop_loss else 0.0
    limit_floor = price * (1 - board_limit(board)) if price > 0 else 0.0
    # 止盈：2:1 R/R（基于结构止损）
    take_profit = 0.0
    if rating in ("Buy", "Watch") and stop > 0 and price > 0:
        risk = price - stop
        if risk > 0:
            take_profit = round(price + risk * TP_MULT, 4)

    # 仓位：利弗莫尔 2% 风险法则——仓位 = 风险预算 / R，使单笔风险≈RISK_BUDGET；
    # 再乘置信度，按板块上限封顶。止损越远（R 越大）仓位自动越小。
    position = 0.0
    if rating == "Buy" and chan.r_ratio and chan.r_ratio > 0:
        risk_sized = RISK_BUDGET / chan.r_ratio
        position = round(min(risk_sized * (0.5 + 0.5 * chan.confidence),
                             BOARD_POSITION_CAP.get(board, 1.0)), 3)

    entry_lo = round(price * 0.99, 4)
    entry_hi = round(price * 1.01, 4)

    # ── 次日执行（日线层面最精确）──────────────────────────────
    # 信号已"停顿✓"确认 → 现价即可市价买；不追上限 = R 达 R_MAX 的价位 = 止损/(1−R_MAX)，
    # 现价或次日高开高于它则 R>15%（追高），放弃。想优化 R 可在现价下方至止损上方挂 limit。
    chase_ceiling = 0.0
    if rating in ("Buy", "Watch") and stop > 0:
        chase_ceiling = round(stop / (1 - R_MAX), 4)

    return AShareDecision(
        code=code, board=board,
        rating=rating, score=chan.score, confidence=chan.confidence,
        buy_point=chan.buy_point_type, sell_point=chan.sell_point_type,
        current_price=price,
        entry_price_range=(entry_lo, entry_hi),
        chase_ceiling=chase_ceiling,
        stop_loss=round(stop, 4) if stop else 0.0,
        take_profit=take_profit,
        r_ratio=chan.r_ratio,
        suggested_position=position,
        weekly=chan.weekly_trend,
        trend_type=chan.trend_type,
        pivot=chan.current_pivot,
        reasoning=chan.reasoning,
        chan=chan,
    )
