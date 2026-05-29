"""
迟滞状态机共享核心（US 与 A 股共用）。

集中两件事，避免 US/A股 两份拷贝漂移：
  1. 状态持久化 load_state / save_state（按 path 区分两市）。
  2. **时效校验 fresh_prior**：迟滞按"连续交易日"语义计 flip_streak，但运行可能隔几天才跑一次。
     若上次状态距今 > MAX_STALE_DAYS（覆盖周末+短假），视为运行有大缺口 → 当作全新开始（不跨缺口迟滞），
     避免把"几天前的昨日态"误当成昨天。

各市场的评级词表 / 紧急放行 / flag 落点不同，apply 逻辑各自保留。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from loguru import logger

CONFIRM_DAYS   = 2     # 反向信号需连续确认天数才执行清仓
MAX_STALE_DAYS = 5     # 上次状态距今 > 此天数（周末+短假内）即视为陈旧 → 重置，不跨缺口迟滞


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}                      # 首次运行，正常
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:           # 文件存在却读不出 = 损坏，告警(避免静默禁用迟滞)
        logger.warning(f"[Hysteresis] 状态损坏 {path.name}，本次重置: {exc}")
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[Hysteresis] 状态落盘失败 {path.name}: {exc}")


def fresh_prior(prior: dict, today_str: str) -> dict:
    """上次状态够新（与今日相差 0~MAX_STALE_DAYS 天）才沿用；否则视为全新开始返回 {}。"""
    d = prior.get("date")
    if not d:
        return {}
    try:
        gap = (date.fromisoformat(today_str) - date.fromisoformat(d)).days
    except Exception:
        return {}
    return prior if 0 <= gap <= MAX_STALE_DAYS else {}
