"""
决策迟滞层（B）：抑制"昨日看多 → 今日清仓"的隔夜翻转。

跨日持久化每只票的 (rating, position, flip_streak)，当出现"昨多→今出"的反向翻转时，
要求连续 CONFIRM_DAYS 天确认才执行清仓；未确认前沿用昨日仓位、标记待确认。
VIX panic 等紧急状态不受迟滞约束（放行即时离场）。

动机：缠论右端笔重画 + 高波动名会让单日信号剧烈摆动（如 LITE 由 Overweight46% 隔夜变清仓），
A(定笔)从源头压制，B 再加一层"反向需连续确认"的状态机，二者叠加去掉隔夜甩动。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from loguru import logger

from config.settings import settings
from decision.strategy import StockDecision

_STATE_PATH  = Path(settings.output_dir) / "signal_state.json"
CONFIRM_DAYS = 2                       # 反向信号需连续确认天数才执行清仓
_LONG = {"Buy", "Overweight"}          # 多头持仓档
_EXIT = {"Sell", "Underweight"}        # 离场/做空档


def load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[Hysteresis] 状态落盘失败: {exc}")


def apply_hysteresis(decisions: Dict[str, StockDecision], date_str: str) -> None:
    """就地调整 decisions：昨多→今出 的翻转需连续 CONFIRM_DAYS 确认，否则沿用昨日仓位。"""
    prior_state = load_state()
    new_state: dict = {}

    for ticker, d in decisions.items():
        prior      = prior_state.get(ticker, {})
        prior_rate = prior.get("rating")
        prior_pos  = float(prior.get("position", 0.0))
        streak     = int(prior.get("flip_streak", 0))

        panic = (d.macro_signal is not None
                 and getattr(d.macro_signal, "vix_regime", "") == "panic")
        is_flip = (prior_rate in _LONG) and (d.rating in _EXIT) and not panic

        if is_flip and streak + 1 < CONFIRM_DAYS:
            # 反向翻转但确认不足 → 抑制清仓，沿用昨日仓位一日
            streak += 1
            d.risk_flags.append(
                f"HYSTERESIS_HOLD: 昨日{prior_rate}→今日{d.rating}，"
                f"反向信号第{streak}/{CONFIRM_DAYS}天，暂不清仓（沿用{prior_pos:.0%}）")
            d.rating             = "Hold"
            d.suggested_position = round(prior_pos, 2)
            # 状态保持"多头待确认"，使次日若仍反向可累加确认
            new_state[ticker] = {"rating": prior_rate, "position": d.suggested_position,
                                 "flip_streak": streak, "date": date_str}
            continue

        if is_flip:
            d.risk_flags.append(
                f"HYSTERESIS_CONFIRMED: 反向信号已连续{streak + 1}天，执行{d.rating}")

        new_state[ticker] = {"rating": d.rating, "position": d.suggested_position,
                             "flip_streak": 0, "date": date_str}

    save_state(new_state)
