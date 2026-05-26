"""
A 股缠论历史回测入口

运行：python run_ashare_backtest.py
对 processed_stocks_selected/ 全部个股跑缠论买卖点回测（A 股化撮合：涨跌停/
跳空/A 股调参），池化聚合：总胜率、按 b1/b2/b3 分类型胜率、vs 买入持有基准。

回答核心问题：**缠论买卖点在 A 股的历史回测胜率是多少？**
报告：output/ashare_backtest/ashare_backtest_report.md
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd
from loguru import logger

import utils.logger  # 日志格式
from utils.time_utils import today_str

from data.ashare_loader import load_ashare_prices
from backtest.engine_ashare import run_backtest_ashare
from backtest.engine import BacktestResult, Trade


def _aggregate(results: Dict[str, BacktestResult]) -> dict:
    all_trades: List[Trade] = []
    bh_rets: List[float] = []
    traded_stocks = 0
    for r in results.values():
        if r.trades:
            all_trades.extend(r.trades)
            traded_stocks += 1
        if r.benchmark_return:
            bh_rets.append(r.benchmark_return)

    if not all_trades:
        return {"n_trades": 0}

    pnls = [t.pnl_pct for t in all_trades]
    wins = [p for p in pnls if p > 0]

    by_sig: Dict[str, List[float]] = defaultdict(list)
    for t in all_trades:
        by_sig[t.signal_type].append(t.pnl_pct)

    sig_stats = {}
    for sig, pl in sorted(by_sig.items()):
        w = [p for p in pl if p > 0]
        sig_stats[sig] = {
            "n": len(pl),
            "win_rate": len(w) / len(pl),
            "avg_ret": float(np.mean(pl)),
        }

    return {
        "n_trades": len(all_trades),
        "traded_stocks": traded_stocks,
        "win_rate": len(wins) / len(pnls),
        "avg_ret": float(np.mean(pnls)),
        "median_ret": float(np.median(pnls)),
        "benchmark_mean": float(np.mean(bh_rets)) if bh_rets else 0.0,
        "sig_stats": sig_stats,
    }


def build_report(results: Dict[str, BacktestResult], agg: dict, date_str: str) -> str:
    lines = [
        f"# A 股缠论历史回测 — {date_str}",
        "",
        "> 信号：日线缠论买卖点（分型→笔→中枢→背驰，背驰用预计算 MACD，无前视）  ",
        "> 撮合：A 股化（一字涨停不可买入、一字跌停顺延、跳空按 min(止损,开盘)/max(止盈,开盘)）  ",
        "> 持仓：买点入场，止损/止盈/反向卖点出场；SL≈9%、TP 2:1；预热 120TD  ",
        "> 口径：全池池化聚合（仅做多）",
        "",
    ]
    if agg.get("n_trades", 0) == 0:
        lines += ["**无有效交易**，请检查数据量或信号。"]
        return "\n".join(lines)

    lines += [
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|------|----|",
        f"| 参与个股 | {agg['traded_stocks']} / {len(results)} |",
        f"| 总交易笔数 | {agg['n_trades']} |",
        f"| **总胜率** | **{agg['win_rate']:.1%}** |",
        f"| 笔均收益 | {agg['avg_ret']:+.2%} |",
        f"| 收益中位数 | {agg['median_ret']:+.2%} |",
        f"| 同期买入持有(均) | {agg['benchmark_mean']:+.2%} |",
        "",
        "## 按买卖点类型拆分（核心结论）",
        "",
        "| 类型 | 笔数 | 胜率 | 笔均收益 |",
        "|------|------|------|---------|",
    ]
    _cn = {"b1": "一买(背驰)", "b2": "二买", "b3": "三买(突破)",
           "s1": "一卖(背驰)", "s2": "二卖", "s3": "三卖(破中枢)"}
    for sig in ["b1", "b2", "b3", "s1", "s2", "s3"]:
        if sig in agg["sig_stats"]:
            s = agg["sig_stats"][sig]
            lines.append(
                f"| {_cn.get(sig, sig)} | {s['n']} | {s['win_rate']:.1%} | {s['avg_ret']:+.2%} |")
    lines += [""]

    # 个股明细（按总收益降序，前 25）
    ranked = sorted(
        [r for r in results.values() if r.num_trades > 0],
        key=lambda r: r.total_return, reverse=True)
    lines += [
        "## 个股回测明细（按总收益降序，前 25）",
        "",
        "| 代码 | 交易 | 胜率 | 总收益 | 基准 | MDD |",
        "|------|------|------|--------|------|-----|",
    ]
    for r in ranked[:25]:
        lines.append(
            f"| {r.ticker} | {r.num_trades} | {r.win_rate:.0%} "
            f"| {r.total_return:+.1%} | {r.benchmark_return:+.1%} | {r.max_drawdown:.1%} |")
    lines += ["", "---", f"*生成时间: {date_str} | 数据源: processed_stocks_selected*"]
    return "\n".join(lines)


def main() -> None:
    date_str = today_str()
    logger.info("=" * 55)
    logger.info("A 股缠论历史回测")
    logger.info("=" * 55)

    prices = load_ashare_prices()
    results: Dict[str, BacktestResult] = {}
    for code, df in prices.items():
        try:
            results[code] = run_backtest_ashare(code, df)
        except Exception as exc:
            logger.warning(f"[BacktestA] {code} 异常: {exc}")

    agg = _aggregate(results)
    if agg.get("n_trades", 0) == 0:
        logger.error("无有效交易，回测终止")
        return

    out_dir = Path("output") / "ashare_backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "ashare_backtest_report.md"
    path.write_text(build_report(results, agg, date_str), encoding="utf-8")

    logger.info(f"参与个股 {agg['traded_stocks']}/{len(results)} | 交易 {agg['n_trades']} 笔")
    logger.info(f"总胜率 {agg['win_rate']:.1%} | 笔均 {agg['avg_ret']:+.2%} | 基准 {agg['benchmark_mean']:+.2%}")
    for sig in ["b1", "b2", "b3", "s1", "s2", "s3"]:
        if sig in agg["sig_stats"]:
            s = agg["sig_stats"][sig]
            logger.info(f"  {sig}: {s['n']}笔 胜率{s['win_rate']:.1%} 笔均{s['avg_ret']:+.2%}")
    logger.info(f"报告: {path}")
    logger.info("=" * 55)


if __name__ == "__main__":
    main()
