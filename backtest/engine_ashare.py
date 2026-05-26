"""
A 股缠论回测引擎

以 backtest/engine.py 为蓝本，撮合改为 A 股化：
  - 涨跌停建模：一字涨停无法买入（跳过入场）；一字跌停锁死当日无法止损（顺延）。
  - 跳空穿越：止损成交价 = min(止损价, 当日开盘)；止盈遇跳空高开 = max(止盈价, 开盘)。
  - 止损止盈按 A 股波动调参（SL_PCT≈9%，TP 2:1）。
  - 预热 WARMUP_BARS=120（每股仅 ~360TD，换取更长回测窗口）。

信号源：extract_chan_events_ashare（背驰用预计算 MACD，无前视）。
复用 engine.py 的 Trade/BacktestResult/_make_trade/_compute_metrics。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from signals.chan.chan_signal_ashare import extract_chan_events_ashare
from signals.chan.chan_signal import ChanEvent
from backtest.engine import Trade, BacktestResult, _make_trade, _compute_metrics
from config.stocks_ashare import SL_PCT, TP_MULT, WARMUP_BARS, MIN_BACKTEST_BARS

_LOCK_EPS = 0.005   # 当日振幅 <0.5% 视为一字锁死（涨跌停无法成交）


def _lock_flags(df: pd.DataFrame):
    """返回 (locked_up, locked_down) 两个按日期索引的布尔 Series。"""
    prev_close = df["Close"].shift(1)
    rng = (df["High"] - df["Low"]) / df["Close"].replace(0, np.nan)
    locked = rng < _LOCK_EPS
    return (locked & (df["Close"] > prev_close),
            locked & (df["Close"] < prev_close))


def _scan_exit(window: pd.DataFrame, sl: float, tp: float, locked_down) -> Optional[tuple]:
    """在给定窗口内逐日扫描止损/止盈，返回 (date, price, reason) 或 None。"""
    for bar_date, row in window.iterrows():
        o, h, l = float(row["Open"]), float(row["High"]), float(row["Low"])
        if l <= sl:
            # 一字跌停锁死：当日无法卖出，顺延到后续 bar
            if bool(locked_down.get(bar_date, False)) and o <= sl:
                continue
            return bar_date, min(sl, o), "sl"     # 跳空穿越按更差的开盘价成交
        if h >= tp:
            return bar_date, max(tp, o), "tp"      # 跳空高开按更优的开盘价成交
    return None


def _simulate_trades_ashare(df: pd.DataFrame, events: List[ChanEvent]) -> List[Trade]:
    """A 股撮合：含涨跌停/跳空约束。仅做多。"""
    locked_up, locked_down = _lock_flags(df)
    trades: List[Trade] = []
    in_pos  = False
    e_date: Optional[pd.Timestamp] = None
    e_sig   = ""
    e_price = sl = tp = 0.0

    def _can_enter(date) -> bool:
        # 一字涨停当日无法买入
        return not bool(locked_up.get(date, False))

    for ev in events:
        if in_pos:
            window = df[(df.index > e_date) & (df.index <= ev.date)]
            hit = _scan_exit(window, sl, tp, locked_down)
            hit_date = hit[0] if hit else None
            hit_price = hit[1] if hit else None
            hit_reason = hit[2] if hit else None

            if hit_date is not None:
                trades.append(_make_trade(e_date, hit_date, e_sig,
                                          e_price, hit_price, hit_reason))
                in_pos = False
                if (ev.signal_type.startswith("b") and ev.date > hit_date
                        and _can_enter(ev.date)):
                    in_pos, e_date, e_sig, e_price = True, ev.date, ev.signal_type, ev.price
                    sl = e_price * (1 - SL_PCT)
                    tp = e_price * (1 + SL_PCT * TP_MULT)

            elif ev.signal_type.startswith("s") and ev.date > e_date:
                trades.append(_make_trade(e_date, ev.date, e_sig,
                                          e_price, ev.price, "signal"))
                in_pos = False

        elif ev.signal_type.startswith("b") and _can_enter(ev.date):
            in_pos, e_date, e_sig, e_price = True, ev.date, ev.signal_type, ev.price
            sl = e_price * (1 - SL_PCT)
            tp = e_price * (1 + SL_PCT * TP_MULT)

    # 收尾：仍持仓 → 先扫描入场至末日是否触及 SL/TP，未触及才以末日收盘出场
    if in_pos and e_date is not None and not df.empty:
        tail = df[df.index > e_date]
        hit = _scan_exit(tail, sl, tp, locked_down)
        if hit is not None:
            trades.append(_make_trade(e_date, hit[0], e_sig, e_price, hit[1], hit[2]))
        else:
            trades.append(_make_trade(e_date, df.index[-1], e_sig,
                                      e_price, float(df["Close"].iloc[-1]), "eod"))
    return trades


def run_backtest_ashare(code: str, df: pd.DataFrame) -> BacktestResult:
    """单只 A 股缠论回测。前 WARMUP_BARS 根仅用于结构初始化，不计回测。"""
    base = BacktestResult(ticker=code, period_start=None,
                          period_end=None, warmup_bars=WARMUP_BARS)
    if df is None or len(df) < WARMUP_BARS + MIN_BACKTEST_BARS:
        base.reasoning = (f"数据不足({len(df) if df is not None else 0}根，"
                          f"需>={WARMUP_BARS + MIN_BACKTEST_BARS})")
        return base

    backtest_start = df.index[WARMUP_BARS]
    backtest_df    = df[df.index >= backtest_start].copy()
    short = len(backtest_df) < 200
    warn  = "  ⚠️ 窗口偏短(<200TD)" if short else ""

    all_events = extract_chan_events_ashare(df)
    events     = [e for e in all_events if e.date >= backtest_start]
    if not events:
        base.period_start, base.period_end = backtest_start, df.index[-1]
        base.reasoning = f"回测区间无缠论信号({len(backtest_df)}根){warn}"
        return base

    trades  = _simulate_trades_ashare(backtest_df, events)
    metrics = _compute_metrics(trades, backtest_df)
    if not metrics:
        base.period_start, base.period_end = backtest_start, df.index[-1]
        base.reasoning = f"无成交记录{warn}"
        return base

    sig_desc = "  ".join(
        f"{k}({v}笔 胜率{metrics['signal_win_rates'].get(k, 0):.0%})"
        for k, v in metrics["signal_counts"].items())
    reasoning = (
        f"回测 {metrics['period_start'].date()}~{metrics['period_end'].date()}  "
        f"交易{metrics['num_trades']}笔 胜率{metrics['win_rate']:.0%} "
        f"总收益{metrics['total_return']:+.1%} 基准{metrics['benchmark_return']:+.1%} "
        f"MDD{metrics['max_drawdown']:.1%} [ {sig_desc} ]{warn}")
    logger.debug(f"[BacktestA] {code}: {reasoning}")

    return BacktestResult(
        ticker=code,
        period_start=metrics["period_start"], period_end=metrics["period_end"],
        warmup_bars=WARMUP_BARS, trades=trades,
        num_trades=metrics["num_trades"], win_rate=metrics["win_rate"],
        avg_pnl_pct=metrics["avg_pnl_pct"], total_return=metrics["total_return"],
        benchmark_return=metrics["benchmark_return"], sharpe=metrics["sharpe"],
        max_drawdown=metrics["max_drawdown"], avg_holding_days=metrics["avg_holding_days"],
        signal_counts=metrics["signal_counts"], signal_win_rates=metrics["signal_win_rates"],
        reasoning=reasoning,
    )
