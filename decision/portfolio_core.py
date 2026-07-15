"""
模拟组合记账核心（paper-trading，US 与 A 股共用）。

在【不改变策略】的前提下，于策略信号之上加一层"执行记账"：
从初始资金开始，严格按策略信号自动买卖、跨日追踪持仓、每日记权益快照，
方便按每日持仓看盈亏。这不是回测（回测是历史检验），而是从启用日起的前向模拟组合。

每日流程（顺序很重要）：
  1. 先卖：持仓中出现卖点信号 或 跌破结构止损 → 按当日收盘价卖出，回笼现金。
  2. 再买：当日 Buy 信号且未持有 → 目标市值 = position_frac × 初始资金，
     受可用现金约束，按当日收盘价买入（A 股按 lot_size 整手取整）。
  3. 记账：持仓市值 + 现金 = 总权益，追加一条当日快照与当日成交。

状态持久化为 JSON（US/A 股各一份），结构见 _empty_state。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger


@dataclass
class Signal:
    """喂给组合的当日单票信号（由各市场决策转换而来，与具体决策类解耦）。"""
    code:          str
    price:         float           # 当日收盘价（成交价）
    is_buy:        bool            # 当日是否为买入信号（Buy/Overweight）
    is_sell:       bool            # 当日是否为卖点信号（Avoid/s1/s2/s3）
    position_frac: float = 0.0     # 策略建议仓位（0~1），买入时用
    stop_loss:     float = 0.0     # 结构止损价（>0 时跌破触发卖出）
    rank:          int = 0         # 当日 score 排名（1 最高，用于现金不足时排序优先）


def _empty_state(initial_capital: float) -> dict:
    return {
        "initial_capital": initial_capital,
        "cash": initial_capital,
        "positions": {},   # code -> {shares, cost_price, buy_date, stop_loss}
        "history": [],     # 每日权益快照
        "trades": [],      # 每笔成交
    }


def load_portfolio(path: Path, initial_capital: float) -> dict:
    if not path.exists():
        return _empty_state(initial_capital)
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        # 容错：缺字段补齐
        for k, v in _empty_state(initial_capital).items():
            st.setdefault(k, v)
        return st
    except Exception as exc:
        logger.warning(f"[Portfolio] 状态损坏 {path.name}，重置: {exc}")
        return _empty_state(initial_capital)


def save_portfolio(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[Portfolio] 状态落盘失败 {path.name}: {exc}")


def update_portfolio(state: dict, date_str: str, signals: List[Signal],
                     lot_size: int = 1) -> dict:
    """就地推进组合一天：先卖后买，记快照。返回 state。

    幂等保护：若当日已记过快照（history 末条 date==date_str），先回滚当日成交与快照，
    避免同一天重复运行把仓位记重（重算后覆盖当日结果）。
    """
    sig_by_code = {s.code: s for s in signals}
    price_of = {s.code: s.price for s in signals if s.price > 0}

    # ── 幂等：同日重跑则回滚当日记录后重算 ──
    if state["history"] and state["history"][-1].get("date") == date_str:
        state["trades"] = [t for t in state["trades"] if t.get("date") != date_str]
        state["history"].pop()
        # 注意：仓位无法精确回滚到买卖前，故约定"同日重跑以最后一次为准"仅重记快照层；
        # 为安全，重跑时不再二次买卖（positions 已是今日结果），直接跳到记快照。
        _snapshot(state, date_str, price_of)
        return state

    positions: Dict[str, dict] = state["positions"]
    initial = state["initial_capital"]

    # ── 1. 先卖 ──
    for code in list(positions.keys()):
        sig = sig_by_code.get(code)
        if sig is None or sig.price <= 0:
            continue   # 当日无数据，保持持仓
        pos = positions[code]
        hit_stop = pos.get("stop_loss", 0) > 0 and sig.price < pos["stop_loss"]
        if sig.is_sell or hit_stop:
            proceeds = pos["shares"] * sig.price
            pnl = (sig.price - pos["cost_price"]) * pos["shares"]
            state["cash"] += proceeds
            reason = "卖点信号" if sig.is_sell else f"跌破止损{pos['stop_loss']:.2f}"
            state["trades"].append({
                "date": date_str, "code": code, "action": "卖出",
                "price": round(sig.price, 3), "shares": pos["shares"],
                "pnl": round(pnl, 2), "reason": reason,
            })
            del positions[code]

    # ── 2. 再买（按排名优先，受现金约束）──
    # 同票当日既有卖出信号又有买入评级 → 以卖为准，当日不回补（防卖后即买的洗仓）
    buys = sorted([s for s in signals
                   if s.is_buy and not s.is_sell
                   and s.code not in positions and s.price > 0],
                  key=lambda s: (s.rank if s.rank else 1e9))
    for s in buys:
        target_value = max(0.0, s.position_frac) * initial
        if target_value <= 0:
            continue
        budget = min(target_value, state["cash"])
        raw_shares = budget / s.price
        shares = int(raw_shares // lot_size) * lot_size if lot_size > 1 else int(raw_shares)
        if shares <= 0:
            continue
        cost = shares * s.price
        if cost > state["cash"]:
            continue
        state["cash"] -= cost
        positions[s.code] = {
            "shares": shares, "cost_price": round(s.price, 3),
            "buy_date": date_str, "stop_loss": round(s.stop_loss, 3),
        }
        state["trades"].append({
            "date": date_str, "code": s.code, "action": "买入",
            "price": round(s.price, 3), "shares": shares, "pnl": 0.0,
            "reason": f"策略买点 仓位{s.position_frac:.0%}",
        })

    # ── 3. 记快照 ──
    _snapshot(state, date_str, price_of)
    return state


def _snapshot(state: dict, date_str: str, price_of: Dict[str, float]) -> None:
    """按当日价给持仓估值，追加一条权益快照。无当日价的持仓用成本价兜底。"""
    positions = state["positions"]
    mkt_value = 0.0
    for code, pos in positions.items():
        px = price_of.get(code, pos["cost_price"])
        mkt_value += pos["shares"] * px
    equity = state["cash"] + mkt_value
    initial = state["initial_capital"]
    state["history"].append({
        "date": date_str,
        "equity": round(equity, 2),
        "cash": round(state["cash"], 2),
        "market_value": round(mkt_value, 2),
        "n_positions": len(positions),
        "total_pnl_pct": round((equity - initial) / initial, 4) if initial else 0.0,
    })
