"""
P7 回测报告写入器

write_backtest_report() → output/{date}/backtest_summary.md
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from backtest.engine import BacktestResult, Trade


def _trade_rows(trades: List[Trade]) -> List[str]:
    rows = []
    for t in trades:
        rows.append(
            f"| {t.entry_date.date()} | {t.exit_date.date()} | "
            f"{t.signal_type.upper()} | {t.entry_price:.2f} | {t.exit_price:.2f} | "
            f"{t.pnl_pct:+.1%} | {t.exit_reason} | {t.holding_days}d |"
        )
    return rows


def _stock_section(r: BacktestResult) -> List[str]:
    lines: List[str] = [f"### {r.ticker}", ""]

    if not r.trades:
        lines += [f"> {r.reasoning}", ""]
        return lines

    # 指标表
    period = (
        f"{r.period_start.date()} ~ {r.period_end.date()}"
        if r.period_start else "—"
    )
    lines += [
        f"| 指标 | 值 |",
        f"|------|----|",
        f"| 回测区间 | {period} |",
        f"| 交易笔数 | {r.num_trades} |",
        f"| 胜率 | {r.win_rate:.0%} |",
        f"| 均笔收益 | {r.avg_pnl_pct:+.1%} |",
        f"| 策略总收益 | {r.total_return:+.1%} |",
        f"| 买入持有 | {r.benchmark_return:+.1%} |",
        f"| 超额收益 | {r.total_return - r.benchmark_return:+.1%} |",
        f"| Sharpe | {r.sharpe:.2f} |",
        f"| 最大回撤 | {r.max_drawdown:.1%} |",
        f"| 均持有天数 | {r.avg_holding_days:.0f}d |",
        "",
    ]

    # 信号类型拆分
    if r.signal_counts:
        lines += [
            "**信号拆分**",
            "",
            f"| 信号 | 交易次数 | 胜率 |",
            f"|------|---------|------|",
        ]
        for sig in sorted(r.signal_counts):
            cnt = r.signal_counts[sig]
            wr  = r.signal_win_rates.get(sig, 0.0)
            lines.append(f"| {sig.upper()} | {cnt} | {wr:.0%} |")
        lines.append("")

    # 明细交易记录
    lines += [
        "<details><summary>交易明细</summary>",
        "",
        "| 入场日 | 出场日 | 信号 | 入价 | 出价 | 收益 | 出场原因 | 持仓 |",
        "|--------|--------|------|------|------|------|---------|------|",
    ]
    lines += _trade_rows(r.trades)
    lines += ["", "</details>", ""]

    return lines


def write_backtest_report(
    results:    Dict[str, BacktestResult],
    output_dir: Path,
    date_str:   str,
) -> Path:
    """生成 backtest_summary.md，返回文件路径。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    ranked = sorted(
        results.values(),
        key=lambda r: r.total_return - r.benchmark_return,
        reverse=True,
    )

    lines: List[str] = [
        f"# 缠论信号回测报告 — {date_str}",
        "",
        "> **方法说明**  ",
        "> 信号=逐日 as-of 重放实盘引擎（结构只用截至当日数据重算，含定笔/停顿/新鲜度门，无前视偏差）。  ",
        "> 仅做多：买点入场，卖点/SL(7%)/TP(14%)出场。  ",
        "> 预热前200根K线仅用于结构初始化，不计入回测区间。  ",
        "> 基本面因子因 yfinance 无时间点数据（PIT）不纳入回测，仅测试缠论价格信号质量。",
        "",
        "## 汇总排行（按超额收益降序）",
        "",
        f"| 股票 | 交易次数 | 胜率 | 策略收益 | 买入持有 | 超额 | Sharpe | MDD |",
        f"|------|---------|------|---------|---------|------|--------|-----|",
    ]

    for r in ranked:
        if r.num_trades == 0:
            lines.append(f"| {r.ticker} | 0 | — | — | — | — | — | — |")
        else:
            exc = r.total_return - r.benchmark_return
            lines.append(
                f"| {r.ticker} | {r.num_trades} | {r.win_rate:.0%} | "
                f"{r.total_return:+.1%} | {r.benchmark_return:+.1%} | "
                f"{exc:+.1%} | {r.sharpe:.2f} | {r.max_drawdown:.1%} |"
            )
    lines.append("")

    # 各股详情
    lines += ["## 各股详情", ""]
    for r in ranked:
        lines += _stock_section(r)

    lines += [
        "---",
        f"*生成时间: {date_str}  |  止损 7%  止盈 14%  (2:1 R/R)*",
    ]

    path = output_dir / "backtest_summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
