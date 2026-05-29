"""
A 股缠论选股入口

运行：python mainA.py
读取 processed_stocks_selected/ 的日线 CSV，逐股计算缠论信号（保守门控：
二买/三买为主，一买严格门控），按 score 排名，输出当日 A 股选股结果到
output/ashare/{date}/（Markdown + CSV）。

历史回测胜率验证请运行 run_ashare_backtest.py（职责分离）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pandas as pd
from loguru import logger

import utils.logger  # 触发日志格式初始化
from utils.time_utils import today_str

from data.ashare_loader import load_ashare_prices, board_limit
from signals.chan.chan_signal_ashare import compute_chan_signal_ashare
from decision.strategy_ashare import make_ashare_decision, AShareDecision

_BOARD_CN = {"main": "主板", "chinext": "创业板", "star": "科创板", "bse": "北交所"}


def _score_bar(score: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round(abs(score) * width))))
    ch = "█" if score >= 0 else "▒"
    return ch * filled + "░" * (width - filled)


def run_selection(folder: str = "processed_stocks_selected") -> List[AShareDecision]:
    prices = load_ashare_prices(folder)
    decisions: List[AShareDecision] = []
    for code, df in prices.items():
        board = df.attrs.get("board", "main")
        chan = compute_chan_signal_ashare(code, df, board)
        decisions.append(make_ashare_decision(code, chan, df, board))
    decisions.sort(key=lambda d: d.score, reverse=True)
    return decisions


def _decisions_to_rows(decisions: List[AShareDecision]) -> List[dict]:
    rows = []
    for d in decisions:
        zd = d.pivot["ZD"] if d.pivot else None
        zg = d.pivot["ZG"] if d.pivot else None
        rows.append({
            "代码": d.code,
            "板块": _BOARD_CN.get(d.board, d.board),
            "评级": d.rating,
            "买点": (d.buy_point or d.sell_point or "—"),
            "score": round(d.score, 3),
            "置信度": round(d.confidence, 2),
            "现价": round(d.current_price, 2),
            "中枢下沿": round(zd, 2) if zd else None,
            "中枢上沿": round(zg, 2) if zg else None,
            "不追上限": d.chase_ceiling or None,
            "止损": d.stop_loss or None,
            "止盈": d.take_profit or None,
            "R比率": d.r_ratio,
            "建议仓位": d.suggested_position or None,
            "周线": d.weekly,
            "趋势": d.trend_type,
        })
    return rows


def build_report(decisions: List[AShareDecision], date_str: str) -> str:
    buys  = [d for d in decisions if d.rating == "Buy"]
    watch = [d for d in decisions if d.rating == "Watch"]

    lines = [
        f"# A 股缠论选股 — {date_str}",
        "",
        "> 方法：日线缠论（分型→笔→中枢→买卖点），MACD 背驰为核心，",
        "> KDJ/RSI 背离 + CCI/BOLL 力度为辅助确认；牛短熊长保守门控：",
        "> 以二买/三买为主，一买需「周线非向下 + 底背离」双确认。",
        "",
        f"| | 数量 |",
        f"|--|--|",
        f"| 分析个股 | {len(decisions)} |",
        f"| 买入候选(Buy) | {len(buys)} |",
        f"| 观察(Watch) | {len(watch)} |",
        "",
    ]

    def _table(title: str, items: List[AShareDecision]) -> List[str]:
        if not items:
            return [f"## {title}", "", "（无）", ""]
        out = [
            f"## {title}", "",
            "| 代码 | 板块 | 买点 | 现价 | 中枢 ZD~ZG | 止损 | 止盈 | R | 仓位 | 周线 | score |",
            "|------|------|------|------|-----------|------|------|---|------|------|-------|",
        ]
        for d in items:
            pv = f"{d.pivot['ZD']:.2f}~{d.pivot['ZG']:.2f}" if d.pivot else "—"
            r = f"{d.r_ratio:.3f}" if d.r_ratio else "—"
            pos = f"{d.suggested_position:.0%}" if d.suggested_position else "—"
            out.append(
                f"| {d.code} | {_BOARD_CN.get(d.board, d.board)} | {d.buy_point or '—'} "
                f"| {d.current_price:.2f} | {pv} | {d.stop_loss or '—'} | {d.take_profit or '—'} "
                f"| {r} | {pos} | {d.weekly} | {d.score:+.2f} |"
            )
        out.append("")
        for d in items:
            out.append(f"- **{d.code}**: {d.reasoning}")
        out.append("")
        return out

    lines += _table("买入候选（Buy）", buys)

    # 次日执行计划（仅 Buy）：把"现价附近买"换成可挂单的确定价位
    if buys:
        lines += [
            "## 次日执行计划（日线层面最精确）",
            "",
            "> 信号已「停顿✓」确认 → 现价即可市价买；想优化 R 可在「现价下方至止损上方」挂 limit。",
            "> **不追上限**=R 达 15% 的价位(止损/0.85)，现价或次日高开高于它则放弃（追高）。",
            "> 止损/第一止盈/仓位均为确定价位，非区间。",
            "",
            "| 代码 | 买点 | 现价 | 不追上限 | 止损 | 第一止盈 | 仓位 |",
            "|------|------|------|---------|------|---------|------|",
        ]
        for d in buys:
            pos = f"{d.suggested_position:.0%}" if d.suggested_position else "—"
            lines.append(
                f"| {d.code} | {d.buy_point or '—'} | {d.current_price:.2f} "
                f"| {d.chase_ceiling or '—'} | {d.stop_loss or '—'} "
                f"| {d.take_profit or '—'} | {pos} |")
        lines += [""]

    lines += _table("观察区（Watch）", watch)

    lines += [
        "## 全池排名（按 score 降序，前 30）",
        "",
        "| 代码 | 板块 | 评级 | 买点 | score | 周线 | 趋势 |",
        "|------|------|------|------|-------|------|------|",
    ]
    for d in decisions[:30]:
        lines.append(
            f"| {d.code} | {_BOARD_CN.get(d.board, d.board)} | {d.rating} "
            f"| {d.buy_point or d.sell_point or '—'} | {d.score:+.2f} | {d.weekly} | {d.trend_type} |"
        )
    lines += ["", "---", f"*生成时间: {date_str} | 数据源: processed_stocks_selected*"]
    return "\n".join(lines)


def main() -> None:
    date_str = today_str()
    logger.info("=" * 55)
    logger.info("A 股缠论选股")
    logger.info("=" * 55)

    decisions = run_selection()
    if not decisions:
        logger.error("无可分析个股")
        return

    out_dir = Path("output") / "ashare" / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    # CSV
    rows = _decisions_to_rows(decisions)
    csv_path = out_dir / "ashare_selection.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    # Markdown
    md_path = out_dir / "ashare_selection.md"
    md_path.write_text(build_report(decisions, date_str), encoding="utf-8")

    buys  = [d for d in decisions if d.rating == "Buy"]
    watch = [d for d in decisions if d.rating == "Watch"]
    logger.info(f"分析 {len(decisions)} 支 | 买入候选 {len(buys)} | 观察 {len(watch)}")
    for d in buys:
        logger.info(
            f"  ★ {d.code} [{_BOARD_CN.get(d.board, d.board)}] {d.buy_point} "
            f"{_score_bar(d.score)} {d.score:+.2f} 现价{d.current_price:.2f} "
            f"止损{d.stop_loss} 仓位{d.suggested_position:.0%} 周线{d.weekly}"
        )
    logger.info(f"报告: {md_path}")
    logger.info(f"明细: {csv_path}")
    logger.info("=" * 55)


if __name__ == "__main__":
    main()
