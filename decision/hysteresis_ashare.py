"""
A 股决策迟滞层（B，对应美股 decision/hysteresis.py）。

抑制"昨日 Buy → 今日 Avoid(卖点)"的隔夜翻转：跨日持久化每只票的
(rating, position, flip_streak)，反向翻转需连续 CONFIRM_DAYS 天确认才执行清仓，
未确认前沿用昨日仓位、在 reasoning 标记待确认。

A 股选股侧已有 #5 定笔确认从源头压制右端临时翻转；B 再补一层针对"已确认反向"的
状态机。A 股无 VIX，无紧急放行分支。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from loguru import logger

from decision.strategy_ashare import AShareDecision

_STATE_PATH  = Path("output") / "ashare_signal_state.json"
CONFIRM_DAYS = 2
_LONG = "Buy"
_EXIT = "Avoid"


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
        logger.warning(f"[HysteresisA] 状态落盘失败: {exc}")


def apply_hysteresis_ashare(decisions: List[AShareDecision], date_str: str) -> None:
    """就地调整 decisions：昨 Buy→今 Avoid 的翻转需连续 CONFIRM_DAYS 确认，否则沿用昨日仓位。"""
    prior_state = load_state()
    new_state: dict = {}

    for d in decisions:
        prior      = prior_state.get(d.code, {})
        prior_rate = prior.get("rating")
        prior_pos  = float(prior.get("position", 0.0))
        streak     = int(prior.get("flip_streak", 0))

        is_flip = (prior_rate == _LONG) and (d.rating == _EXIT)

        if is_flip and streak + 1 < CONFIRM_DAYS:
            streak += 1
            d.reasoning += (f" | 迟滞:昨Buy→今Avoid，反向第{streak}/{CONFIRM_DAYS}天，"
                            f"暂不清仓(沿用{prior_pos:.0%})")
            d.rating             = "Hold"
            d.suggested_position = round(prior_pos, 2)
            new_state[d.code] = {"rating": prior_rate, "position": d.suggested_position,
                                 "flip_streak": streak, "date": date_str}
            continue

        if is_flip:
            d.reasoning += f" | 迟滞:反向已连续{streak + 1}天，确认Avoid"

        new_state[d.code] = {"rating": d.rating, "position": d.suggested_position,
                             "flip_streak": 0, "date": date_str}

    save_state(new_state)
