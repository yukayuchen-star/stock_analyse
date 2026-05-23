"""
P7 回测引擎 — 缠论买卖点 walk-forward 回测

信号源：extract_chan_events()（在每根笔完成时触发，无前视偏差）
模拟规则：
  - 仅做多（买信号入场，卖信号 / SL / TP 出场）
  - 止损：入场价 × (1 - SL_PCT)
  - 止盈：入场价 × (1 + SL_PCT × TP_MULT)  [2:1 R/R]
  - 数据不足或无信号 → 返回空结果

预热：前 WARMUP_BARS 根 K 线仅用于结构初始化，不计入回测区间。
建议使用 backtest_history_days=1825（~1250 TD）获取足够样本。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from signals.chan.chan_signal import ChanEvent, extract_chan_events

# ── 常量 ────────────────────────────────────────────────────────
WARMUP_BARS = 200    # 预热最少 K 线数（chan 结构 + SMA200）
MIN_BACKTEST_BARS = 50   # 回测区间最少 K 线数（否则无意义）
_SL_PCT  = 0.07      # 止损比例（中性制度默认值）
_TP_MULT = 2.0       # 止盈倍数（2:1 R/R）


# ── 数据类 ──────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_date:  pd.Timestamp
    exit_date:   pd.Timestamp
    signal_type: str    # "b1"|"b2"|"b3"
    entry_price: float
    exit_price:  float
    pnl_pct:     float
    exit_reason: str    # "sl"|"tp"|"signal"|"eod"
    holding_days: int


@dataclass
class BacktestResult:
    ticker:       str
    period_start: Optional[pd.Timestamp]
    period_end:   Optional[pd.Timestamp]
    warmup_bars:  int

    trades: List[Trade] = field(default_factory=list)

    # 汇总指标
    num_trades:         int   = 0
    win_rate:           float = 0.0
    avg_pnl_pct:        float = 0.0
    total_return:       float = 0.0   # 复利净值增长
    benchmark_return:   float = 0.0   # 同期买入持有
    sharpe:             float = 0.0
    max_drawdown:       float = 0.0
    avg_holding_days:   float = 0.0

    # 信号类型拆分
    signal_counts:    Dict[str, int]   = field(default_factory=dict)
    signal_win_rates: Dict[str, float] = field(default_factory=dict)

    reasoning: str = ""


# ── 交易模拟 ─────────────────────────────────────────────────────

def _make_trade(
    entry_date:  pd.Timestamp,
    exit_date:   pd.Timestamp,
    signal_type: str,
    entry_price: float,
    exit_price:  float,
    exit_reason: str,
) -> Trade:
    pnl = (exit_price - entry_price) / entry_price
    days = max(int((exit_date - entry_date).days), 0)
    return Trade(entry_date, exit_date, signal_type,
                 entry_price, exit_price, pnl, exit_reason, days)


def _simulate_trades(df: pd.DataFrame, events: List[ChanEvent]) -> List[Trade]:
    """
    给定回测区间的 OHLCV 和信号事件列表，模拟多头交易。
    SL/TP 用区间内日 Low/High 检查（保守假设：SL 在当日 Low 成交）。
    """
    trades: List[Trade] = []
    in_pos     = False
    e_date: Optional[pd.Timestamp] = None
    e_sig  = ""
    e_price = sl = tp = 0.0

    for ev in events:
        if in_pos:
            # 检查从上次入场到本事件之间是否触及 SL / TP
            window = df[(df.index > e_date) & (df.index <= ev.date)]
            hit_date = hit_price = hit_reason = None

            for bar_date, row in window.iterrows():
                if float(row["Low"]) <= sl:
                    hit_date, hit_price, hit_reason = bar_date, sl, "sl"
                    break
                if float(row["High"]) >= tp:
                    hit_date, hit_price, hit_reason = bar_date, tp, "tp"
                    break

            if hit_date:
                trades.append(_make_trade(e_date, hit_date, e_sig,
                                          e_price, hit_price, hit_reason))
                in_pos = False
                # 若当前信号在出场日之后 → 可以新建多头
                if ev.signal_type.startswith("b") and ev.date > hit_date:
                    in_pos  = True
                    e_date  = ev.date
                    e_sig   = ev.signal_type
                    e_price = ev.price
                    sl = e_price * (1 - _SL_PCT)
                    tp = e_price * (1 + _SL_PCT * _TP_MULT)

            elif ev.signal_type.startswith("s") and ev.date > e_date:
                # F5: 卖出信号 → 以当日收盘出场（同日入场的卖信号忽略，避免 0-PnL 幽灵交易）
                close_p = ev.price
                trades.append(_make_trade(e_date, ev.date, e_sig,
                                          e_price, close_p, "signal"))
                in_pos = False

            # 连续买入信号 → 保持持仓不动

        elif ev.signal_type.startswith("b"):
            in_pos  = True
            e_date  = ev.date
            e_sig   = ev.signal_type
            e_price = ev.price
            sl = e_price * (1 - _SL_PCT)
            tp = e_price * (1 + _SL_PCT * _TP_MULT)

    # 收尾：若仍持仓则以最后收盘价出场
    if in_pos and e_date is not None and not df.empty:
        last_d = df.index[-1]
        last_p = float(df["Close"].iloc[-1])
        trades.append(_make_trade(e_date, last_d, e_sig, e_price, last_p, "eod"))

    return trades


# ── 指标计算 ─────────────────────────────────────────────────────

def _compute_metrics(trades: List[Trade], df: pd.DataFrame) -> dict:
    if not trades:
        return {}

    pnls = [t.pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]

    win_rate    = len(wins) / len(pnls)
    avg_pnl     = float(np.mean(pnls))
    total_ret   = float(np.prod([1 + p for p in pnls]) - 1)

    # 简化 Sharpe：以笔均收益 / 标准差 × sqrt(252/均持有天数) 近似
    # 方差趋零时 Sharpe 爆炸 → cap 至 5.0；方差严格为零且收益为正 → 同样给出 5.0
    avg_hold = float(np.mean([t.holding_days for t in trades]))
    if len(pnls) > 1 and np.std(pnls) > 1e-9:
        sharpe = (avg_pnl / np.std(pnls)) * np.sqrt(252 / max(avg_hold, 1))
        sharpe = min(sharpe, 5.0)
    elif len(pnls) > 1 and avg_pnl > 0:
        sharpe = 5.0   # F4: 零方差正收益，使用 cap 上限而非 0
    else:
        sharpe = 0.0

    # 权益曲线最大回撤
    equity = np.cumprod([1 + p for p in pnls])
    peak   = np.maximum.accumulate(equity)
    mdd    = float(np.min((equity - peak) / peak))

    # 基准：同期买入持有
    p_start = trades[0].entry_date
    p_end   = trades[-1].exit_date
    bh_start = df.loc[df.index >= p_start, "Close"]
    bh_end   = df.loc[df.index <= p_end,   "Close"]
    if not bh_start.empty and not bh_end.empty:
        bh_ret = float((bh_end.iloc[-1] - bh_start.iloc[0]) / bh_start.iloc[0])
    else:
        bh_ret = 0.0

    # 信号类型拆分
    sig_wins: Dict[str, list] = defaultdict(list)
    for t in trades:
        sig_wins[t.signal_type].append(t.pnl_pct > 0)
    signal_counts    = {k: len(v) for k, v in sig_wins.items()}
    signal_win_rates = {k: float(np.mean(v)) for k, v in sig_wins.items()}

    return {
        "num_trades":       len(trades),
        "win_rate":         win_rate,
        "avg_pnl_pct":      avg_pnl,
        "total_return":     total_ret,
        "benchmark_return": bh_ret,
        "sharpe":           sharpe,
        "max_drawdown":     mdd,
        "avg_holding_days": avg_hold,
        "period_start":     p_start,
        "period_end":       p_end,
        "signal_counts":    signal_counts,
        "signal_win_rates": signal_win_rates,
    }


# ── 单股回测 ─────────────────────────────────────────────────────

def run_backtest(ticker: str, df: pd.DataFrame) -> BacktestResult:
    """
    对单只股票运行缠论信号回测。

    df 应为 backtest_history_days 窗口的完整 OHLCV DataFrame。
    前 WARMUP_BARS 根 K 线用于结构初始化，不计入回测。
    """
    base = BacktestResult(ticker=ticker,
                          period_start=None,
                          period_end=None,
                          warmup_bars=WARMUP_BARS)

    if df is None or len(df) < WARMUP_BARS + MIN_BACKTEST_BARS:
        base.reasoning = (
            f"数据不足({len(df) if df is not None else 0}根，"
            f"需>={WARMUP_BARS + MIN_BACKTEST_BARS}根)"
        )
        return base

    backtest_start = df.index[WARMUP_BARS]
    backtest_df    = df[df.index >= backtest_start].copy()
    short_history  = len(backtest_df) < 300  # 回测区间不足 300 TD，结论参考性有限

    # 提取信号（使用全量数据，无前视偏差）
    all_events = extract_chan_events(df)
    events     = [e for e in all_events if e.date >= backtest_start]

    warn = "  ⚠️ 历史不足(<300TD)" if short_history else ""

    if not events:
        base.period_start = backtest_start
        base.period_end   = df.index[-1]
        base.reasoning    = (
            f"回测区间无缠论信号 "
            f"({backtest_start.date()}~{df.index[-1].date()}，"
            f"{len(backtest_df)}根K线){warn}"
        )
        return base

    trades  = _simulate_trades(backtest_df, events)
    metrics = _compute_metrics(trades, backtest_df)

    if not metrics:
        base.period_start = backtest_start
        base.period_end   = df.index[-1]
        base.reasoning    = f"无成交记录（信号未触发有效入场）{warn}"
        return base

    sig_desc = "  ".join(
        f"{k}({v}笔 胜率{metrics['signal_win_rates'].get(k, 0):.0%})"
        for k, v in metrics["signal_counts"].items()
    )
    reasoning = (
        f"回测 {metrics['period_start'].date()}~{metrics['period_end'].date()}  "
        f"交易{metrics['num_trades']}笔  胜率{metrics['win_rate']:.0%}  "
        f"总收益{metrics['total_return']:+.1%}  基准{metrics['benchmark_return']:+.1%}  "
        f"Sharpe={metrics['sharpe']:.2f}  MDD={metrics['max_drawdown']:.1%}  "
        f"[ {sig_desc} ]{warn}"
    )
    logger.debug(f"[Backtest] {ticker}: {reasoning}")

    return BacktestResult(
        ticker=ticker,
        period_start=metrics["period_start"],
        period_end=metrics["period_end"],
        warmup_bars=WARMUP_BARS,
        trades=trades,
        num_trades=metrics["num_trades"],
        win_rate=metrics["win_rate"],
        avg_pnl_pct=metrics["avg_pnl_pct"],
        total_return=metrics["total_return"],
        benchmark_return=metrics["benchmark_return"],
        sharpe=metrics["sharpe"],
        max_drawdown=metrics["max_drawdown"],
        avg_holding_days=metrics["avg_holding_days"],
        signal_counts=metrics["signal_counts"],
        signal_win_rates=metrics["signal_win_rates"],
        reasoning=reasoning,
    )


# ── 全池入口 ─────────────────────────────────────────────────────

def run_all_backtests(
    pipeline,
    tickers: List[str],
) -> Dict[str, BacktestResult]:
    """
    为股票池每只股票拉取长窗口数据并运行回测。
    数据通过 pipeline.get_backtest_price() 获取（独立于 P1 信号数据）。
    """
    results: Dict[str, BacktestResult] = {}
    for ticker in tickers:
        try:
            df = pipeline.get_backtest_price(ticker)
            results[ticker] = run_backtest(ticker, df)
        except Exception as exc:
            logger.warning(f"[Backtest] {ticker} 异常: {exc}")
            results[ticker] = BacktestResult(
                ticker=ticker,
                period_start=None,
                period_end=None,
                warmup_bars=WARMUP_BARS,
                reasoning=f"异常: {exc}",
            )
    return results
