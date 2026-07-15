"""
决策迟滞层（B）：抑制"昨日看多 → 今日清仓"的隔夜翻转。

跨日持久化每只票的 (rating, position, flip_streak, sell_pt_streak)：
  1. 评级翻转"昨多→今出"需连续 CONFIRM_DAYS 天确认才执行清仓；未确认前沿用昨日仓位。
  2. 缠论卖点(s1/s2/s3)同样需连续 CONFIRM_DAYS 天出现才裁定 chan_sell_confirmed=True，
     组合层据此才执行卖点清仓（跌破结构止损的卖出不受迟滞约束，风控优先）。
VIX panic 等紧急状态不受迟滞约束（放行即时离场）。
同日重跑不重复累加 streak（幂等）。

时效校验见 hysteresis_core.fresh_prior：运行若隔多日，旧态视为全新开始，不跨缺口迟滞。

动机：缠论右端笔重画 + 高波动名会让单日信号剧烈摆动（如 LITE 由 Overweight46% 隔夜变清仓），
A(定笔)从源头压制，B 再加一层"反向需连续确认"的状态机，二者叠加去掉隔夜甩动。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from config.settings import settings
from decision.strategy import StockDecision
from decision.hysteresis_core import CONFIRM_DAYS, load_state, save_state, fresh_prior

_STATE_PATH = Path(settings.output_dir) / "signal_state.json"
_LONG = {"Buy", "Overweight"}          # 多头持仓档
_EXIT = {"Sell", "Underweight"}        # 离场/做空档


def apply_hysteresis(decisions: Dict[str, StockDecision], date_str: str) -> None:
    """就地调整 decisions：
    1. 昨多→今出 的评级翻转需连续 CONFIRM_DAYS 天确认，否则沿用昨日仓位。
    2. 缠论卖点需连续 CONFIRM_DAYS 天出现才裁定 chan_sell_confirmed（panic 直通）。
    """
    prior_state = load_state(_STATE_PATH)
    new_state: dict = {}

    for ticker, d in decisions.items():
        prior      = fresh_prior(prior_state.get(ticker, {}), date_str)
        prior_rate = prior.get("rating")
        prior_pos  = float(prior.get("position", 0.0))
        streak     = int(prior.get("flip_streak", 0))
        # 同日重跑：首跑已把今日计入 streak，不再累加（幂等）
        same_day   = prior.get("date") == date_str

        panic = (d.macro_signal is not None
                 and getattr(d.macro_signal, "vix_regime", "") == "panic")

        # ── 缠论卖点确认：连续 CONFIRM_DAYS 天出现才允许组合清仓 ──
        sell_pt = (d.chan_signal.sell_point_type
                   if d.chan_signal is not None else None)
        sell_streak = int(prior.get("sell_pt_streak", 0))
        if sell_pt is not None:
            sell_streak = max(sell_streak, 1) if same_day else sell_streak + 1
        else:
            sell_streak = 0
        d.chan_sell_confirmed = (sell_pt is not None
                                 and (panic or sell_streak >= CONFIRM_DAYS))
        if sell_pt is not None and not d.chan_sell_confirmed:
            d.risk_flags.append(
                f"HYSTERESIS_HOLD: 缠论卖点({sell_pt})第{sell_streak}/{CONFIRM_DAYS}天，"
                f"未连续确认，组合暂不清仓")

        is_flip = (prior_rate in _LONG) and (d.rating in _EXIT) and not panic

        if is_flip:
            flip_streak = max(streak, 1) if same_day else streak + 1
            if flip_streak < CONFIRM_DAYS:
                d.risk_flags.append(
                    f"HYSTERESIS_HOLD: 昨日{prior_rate}→今日{d.rating}，"
                    f"反向信号第{flip_streak}/{CONFIRM_DAYS}天，暂不清仓（沿用{prior_pos:.0%}）")
                d.rating             = "Hold"
                d.suggested_position = round(prior_pos, 2)
                new_state[ticker] = {"rating": prior_rate, "position": d.suggested_position,
                                     "flip_streak": flip_streak,
                                     "sell_pt_streak": sell_streak, "date": date_str}
                continue
            d.risk_flags.append(
                f"HYSTERESIS_CONFIRMED: 反向信号已连续{flip_streak}天，执行{d.rating}")

        new_state[ticker] = {"rating": d.rating, "position": d.suggested_position,
                             "flip_streak": 0,
                             "sell_pt_streak": sell_streak, "date": date_str}

    save_state(_STATE_PATH, new_state)
