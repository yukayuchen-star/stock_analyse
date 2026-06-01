"""
A 股缠论选股入口

运行：python mainA.py
读取 processed_stocks_selected/ 的日线 CSV，逐股计算缠论信号（保守门控：
二买/三买为主，一买严格门控），按 score 排名，输出当日 A 股选股结果到
output/ashare/{date}/（Markdown + CSV）。

历史回测胜率验证请运行 run_ashare_backtest.py（职责分离）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
from loguru import logger

import utils.logger  # 触发日志格式初始化
from utils.time_utils import today_str

from data.ashare_loader import load_ashare_prices, load_one_csv, classify_board, board_limit
from signals.chan.chan_signal_ashare import compute_chan_signal_ashare
from decision.strategy_ashare import make_ashare_decision, AShareDecision
from decision.hysteresis_ashare import apply_hysteresis_ashare
from decision.holdings_ashare import evaluate_holdings
from decision.portfolio_core import (
    Signal, load_portfolio, save_portfolio, update_portfolio,
)
from config.stocks_ashare import PORTFOLIO_INITIAL_CAPITAL, PORTFOLIO_LOT_SIZE

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


def _holdings_section(holdings: list) -> List[str]:
    """渲染「我的持仓跟踪」区。holdings 为 Holding 列表（已体检）。"""
    if not holdings:
        return []
    _ACT_ICON = {"持有": "🟢", "减仓观察": "🟡", "卖出": "🔴", "止损": "⛔"}
    out = [
        "## 我的持仓跟踪",
        "",
        "> 对 holdings.txt 中的持仓逐支缠论体检：浮动盈亏 + 持有/减仓/卖出/止损建议。",
        "",
        "| 代码 | 板块 | 买入价 | 现价 | 浮动盈亏 | 建议 | 结构止损 | 说明 |",
        "|------|------|--------|------|---------|------|---------|------|",
    ]
    for h in holdings:
        icon = _ACT_ICON.get(h.action, "")
        sl = f"{h.stop_loss:.2f}" if h.stop_loss else "—"
        out.append(
            f"| {h.code} | {_BOARD_CN.get(classify_board(h.code), '—')} "
            f"| {h.buy_price:.2f} | {h.current_price:.2f} | {h.pnl_pct:+.1%} "
            f"| {icon}{h.action} | {sl} | {h.reason} |")
    out.append("")
    return out


def build_report(decisions: List[AShareDecision], date_str: str,
                 watchlist_codes: Optional[Set[str]] = None,
                 holdings: Optional[list] = None,
                 portfolio: Optional[dict] = None) -> str:
    buys  = [d for d in decisions if d.rating == "Buy"]
    watch = [d for d in decisions if d.rating == "Watch"]
    n_avoid = sum(1 for d in decisions if d.rating == "Avoid")

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
        f"| 卖出信号(Avoid) | {n_avoid} |",
        "",
    ]

    # 模拟组合（置顶——最关心的是按策略走的整体盈亏）
    if portfolio:
        lines += _portfolio_section(portfolio, decisions)

    # 我的持仓跟踪（holdings.txt 个人实盘持仓，与模拟组合独立）
    lines += _holdings_section(holdings or [])

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

    # 卖出信号区（Avoid）：当日全市场触发缠论卖点(s1/s2/s3)的票
    sells = [d for d in decisions if d.rating == "Avoid"]
    if sells:
        lines += [
            "## 卖出信号（Avoid）",
            "",
            "> 当日触发缠论卖点(s1顶背驰/s2/s3破中枢)的票——若你持有应考虑减仓/离场；",
            "> 注：这是全市场扫描结果，非针对你的持仓（个人持仓见「我的持仓跟踪」）。",
            "",
            "| 代码 | 板块 | 卖点 | 现价 | 中枢 ZD~ZG | 周线 | score |",
            "|------|------|------|------|-----------|------|-------|",
        ]
        for d in sells:
            pv = f"{d.pivot['ZD']:.2f}~{d.pivot['ZG']:.2f}" if d.pivot else "—"
            lines.append(
                f"| {d.code} | {_BOARD_CN.get(d.board, d.board)} | {d.sell_point or '—'} "
                f"| {d.current_price:.2f} | {pv} | {d.weekly} | {d.score:+.2f} |")
        lines += [""]
        for d in sells:
            lines.append(f"- **{d.code}**: {d.reasoning}")
        lines += [""]

    # 手动关注股票走势分析（Buy/Watch 已展示的不重复，只分析其余的）
    if watchlist_codes:
        shown = {d.code for d in buys + watch}
        wl_pending = [d for d in decisions
                      if d.code in watchlist_codes and d.code not in shown]
        wl_shown   = [d for d in decisions
                      if d.code in watchlist_codes and d.code in shown]

        lines += [
            "## 手动关注股票走势分析",
            "",
            "> 对手动输入的关注股票做缠论结构诊断：当前走势阶段 + 距离可买入条件分析。",
            "> 已触发 Buy/Watch 的关注票见上方对应区，此处仅列出尚未达到买点的票。",
            "",
        ]

        if wl_shown:
            lines += [f"**已触发信号（见上方 Buy/Watch 区）**：{', '.join(d.code for d in wl_shown)}", ""]

        if wl_pending:
            lines += [
                "| 代码 | 板块 | 现价 | 周线 | 走势结构 | 笔数 | 中枢 ZD~ZG | 距离买点分析 |",
                "|------|------|------|------|---------|------|-----------|------------|",
            ]
            for d in wl_pending:
                pv  = f"{d.pivot['ZD']:.2f}~{d.pivot['ZG']:.2f}" if d.pivot else "—"
                bn  = str(d.chan.stroke_count) if d.chan else "—"
                analysis = _buy_distance_analysis(d)
                lines.append(
                    f"| {d.code} | {_BOARD_CN.get(d.board, d.board)} | {d.current_price:.2f} "
                    f"| {d.weekly} | {d.trend_type} | {bn} | {pv} | {analysis} |"
                )
            lines += [
                "",
                "### 走势详情",
                "",
            ]
            for d in wl_pending:
                lines.append(f"- **{d.code}**：{d.reasoning}")
            lines.append("")
        else:
            lines += ["（关注股票均已触发信号，见上方 Buy/Watch 区）", ""]

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


def _buy_distance_analysis(d: AShareDecision) -> str:
    """
    分析距离可买入条件还有多远，返回简洁的人类可读描述（供手动关注分析区使用）。
    依赖 d.chan（ChanSignalResult）里的结构字段；若 chan 为 None 则回退到 reasoning 解析。
    """
    # 已触发买点 → 见 Buy/Watch 区
    if d.buy_point in ("b1", "b2", "b3", "lb2"):
        return f"✅ 已触发 {d.buy_point}，见 Buy/Watch 区"

    # 有卖出信号
    if d.sell_point:
        return f"⛔ 当前为 {d.sell_point} 卖出结构，不建议入场"

    issues: List[str] = []
    chan = d.chan
    r = d.reasoning

    # 周线
    if d.weekly == "down":
        issues.append("周线向下(多头信号打折，需等周线企稳)")

    # 结构推断
    if chan is not None:
        stroke_dir = chan.last_stroke_direction
        fractal_ok = chan.fractal_stop
        stroke_ok  = getattr(chan, "stroke_confirmed", True)

        if stroke_dir == "up":
            issues.append("末笔向上 → 需等上涨结束后形成下跌回调笔，才能构成 b2/b3 买点")
        elif stroke_dir == "down":
            if not fractal_ok:
                issues.append("末笔向下✓ 但分型停顿未确认 → 观察 1-2 天等停顿确认")
            elif not stroke_ok:
                issues.append("停顿✓ 但末笔未定笔(右端不稳) → 再等 2 根 K 线确认")
            else:
                # 结构齐备但没发买点 → 中枢/价位问题
                if d.pivot:
                    zg, zd = d.pivot["ZG"], d.pivot["ZD"]
                    price  = d.current_price
                    dist   = (price - zd) / max(price, 1)
                    if dist > 0.10:
                        issues.append(
                            f"结构具备但现价({price:.2f})距中枢下沿 ZD({zd:.2f})"
                            f" 尚有 {dist:.0%}，等进一步回踩")
                    else:
                        issues.append(
                            f"接近买点区(ZD={zd:.2f} ZG={zg:.2f})，"
                            f"可能被 R/门控过滤 → 关注停顿后确认")
                else:
                    issues.append("末笔向下✓停顿✓定笔✓ 但暂无有效中枢，结构不足")
    else:
        # 回退：解析 reasoning 关键字
        if "末笔=up" in r:
            issues.append("末笔向上 → 等形成回调下跌笔")
        elif "停顿×" in r:
            issues.append("末笔向下但停顿未确认 → 观察 1-2 天")
        elif "未定笔" in r:
            issues.append("停顿✓ 但末笔未定笔 → 再等几根 K 确认")

    # R 超限
    if d.r_ratio and d.r_ratio > 0.15:
        issues.append(
            f"R={d.r_ratio:.0%}>15% 入场离支撑太远 → 需价格回踩至支撑附近再看")

    # 无中枢
    if not d.pivot and not issues:
        issues.append("笔数不足或尚未形成有效中枢，需更多走势积累")

    if not issues:
        issues.append("结构条件尚不成熟，持续跟踪")

    return "；".join(issues)


_WATCHLIST_FILE = "watchlist.txt"


def _parse_codes(raw: str) -> List[str]:
    """从原始文本提取去重后的 6 位股票代码。

    支持混合格式：逗号/换行/空格分隔、Python 列表、带引号、带 sh/sz 前后缀。
    以 # 开头的行视为注释忽略。
    """
    # 去掉注释行
    text = "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("#")
    )
    cleaned = (text.replace("[", " ").replace("]", " ")
                   .replace('"', " ").replace("'", " ")
                   .replace(",", " ").replace("\n", " ").replace("\t", " "))
    seen: Set[str] = set()
    result: List[str] = []
    for part in cleaned.split():
        part = part.strip()
        if not part:
            continue
        m = re.search(r"\d{6}", part)
        if m:
            code = m.group(0)
            if code not in seen:
                seen.add(code)
                result.append(code)
        else:
            logger.warning(f"[Watchlist] 无效代码格式，跳过: {part!r}")
    return result


def _load_watchlist() -> List[str]:
    """加载手动关注股票代码列表。

    优先读项目根目录 `watchlist.txt`（每行/逗号分隔代码，# 注释）——
    不依赖 TTY，IDE 运行/后台运行均可用，是推荐方式。
    文件不存在且处于交互终端(TTY)时，回退到 input() 交互输入。
    """
    f = Path(_WATCHLIST_FILE)
    if f.exists():
        raw = f.read_text(encoding="utf-8")
        codes = _parse_codes(raw)
        logger.info(f"[Watchlist] 从 {_WATCHLIST_FILE} 读取 {len(codes)} 支关注股票")
        return codes

    if not sys.stdin.isatty():
        logger.info(
            f"[Watchlist] 无 {_WATCHLIST_FILE} 且非交互环境，跳过手动关注"
            f"（如需：在项目根目录建 {_WATCHLIST_FILE}，每行一个代码）")
        return []

    sep = "─" * 52
    print(f"\n{sep}")
    print("  手动关注股票（可选）")
    print("  格式：603986,301308  或  [\"603986\",\"301308\"]")
    print(f"  提示：也可建 {_WATCHLIST_FILE} 文件免去每次输入")
    print("  直接回车跳过")
    print(sep)
    raw = input("  输入关注代码: ").strip()
    if not raw:
        return []
    return _parse_codes(raw)


def _analyse_watchlist(
    codes: List[str],
    existing_codes: Set[str],
    folder: str = "processed_stocks_selected",
) -> List[AShareDecision]:
    """
    对手动关注股票补跑缠论信号。
    已在全量筛选结果中的代码直接跳过（去重）；找不到数据文件的代码告警跳过。
    """
    new_codes = [c for c in codes if c not in existing_codes]
    if not new_codes:
        logger.info("[Watchlist] 所有关注股票已在全量结果中，无需补跑")
        return []

    base = Path(folder)
    extra: List[AShareDecision] = []
    for code in new_codes:
        matches = sorted(base.glob(f"*{code}*.csv"))
        if not matches:
            logger.warning(f"[Watchlist] {code} 未找到数据文件（不在 {folder}/），跳过")
            continue
        df = load_one_csv(matches[0])
        if df is None or len(df) < 200:
            logger.warning(f"[Watchlist] {code} 数据不足，跳过")
            continue
        board = classify_board(code)
        df.attrs["board"] = board
        chan = compute_chan_signal_ashare(code, df, board)
        d    = make_ashare_decision(code, chan, df, board)
        extra.append(d)
        logger.info(
            f"[Watchlist] {code} [{_BOARD_CN.get(board, board)}] "
            f"{d.rating} score={d.score:+.2f}"
        )
    return extra


_PORTFOLIO_PATH = Path("output") / "ashare_portfolio.json"


def _run_portfolio(decisions: List[AShareDecision], date_str: str) -> dict:
    """按策略信号推进模拟组合一天（不改策略，仅记账）。返回组合状态。"""
    # 当日 Buy 排名（score 降序）→ rank，用于现金不足时优先靠前的票
    buys_sorted = sorted([d for d in decisions if d.rating == "Buy"],
                         key=lambda d: d.score, reverse=True)
    rank_of = {d.code: i + 1 for i, d in enumerate(buys_sorted)}

    signals = [
        Signal(
            code=d.code,
            price=d.current_price,
            is_buy=(d.rating == "Buy"),
            is_sell=(d.rating == "Avoid" or d.sell_point is not None),
            position_frac=d.suggested_position,
            stop_loss=d.stop_loss or 0.0,
            rank=rank_of.get(d.code, 0),
        )
        for d in decisions
    ]
    state = load_portfolio(_PORTFOLIO_PATH, PORTFOLIO_INITIAL_CAPITAL)
    update_portfolio(state, date_str, signals, lot_size=PORTFOLIO_LOT_SIZE)
    save_portfolio(_PORTFOLIO_PATH, state)
    return state


def _portfolio_section(state: dict, decisions: List[AShareDecision]) -> List[str]:
    """渲染「模拟组合」区：当日权益/盈亏 + 持仓明细 + 当日成交。"""
    hist = state.get("history", [])
    if not hist:
        return []
    cur = hist[-1]
    initial = state["initial_capital"]
    price_now = {d.code: d.current_price for d in decisions if d.current_price > 0}
    name_board = {d.code: _BOARD_CN.get(d.board, d.board) for d in decisions}

    out = [
        "## 模拟组合（按策略信号自动买卖）",
        "",
        f"> 初始资金 ¥{initial:,.0f}，从启用日起严格按策略 Buy/卖点信号模拟买卖、跨日追踪。",
        f"> 成交价=信号当日收盘价，仓位=策略建议，A股按{PORTFOLIO_LOT_SIZE}股整手。",
        "",
        f"| 项目 | 值 |",
        f"|--|--|",
        f"| 总权益 | ¥{cur['equity']:,.2f} |",
        f"| 累计盈亏 | {cur['total_pnl_pct']:+.2%}（¥{cur['equity']-initial:+,.2f}）|",
        f"| 持仓市值 | ¥{cur['market_value']:,.2f} |",
        f"| 可用现金 | ¥{cur['cash']:,.2f} |",
        f"| 持仓数 | {cur['n_positions']} |",
        f"| 记录天数 | {len(hist)} |",
        "",
    ]

    positions = state.get("positions", {})
    if positions:
        out += [
            "### 当前持仓",
            "",
            "| 代码 | 板块 | 买入价 | 现价 | 浮动盈亏 | 持股 | 市值 | 买入日 | 止损 |",
            "|------|------|--------|------|---------|------|------|--------|------|",
        ]
        for code, pos in sorted(positions.items(),
                                key=lambda kv: kv[1]["buy_date"]):
            px = price_now.get(code, pos["cost_price"])
            pnl = (px - pos["cost_price"]) / pos["cost_price"] if pos["cost_price"] else 0
            mv = pos["shares"] * px
            sl = f"{pos['stop_loss']:.2f}" if pos.get("stop_loss") else "—"
            out.append(
                f"| {code} | {name_board.get(code, '—')} | {pos['cost_price']:.2f} "
                f"| {px:.2f} | {pnl:+.1%} | {pos['shares']} | ¥{mv:,.0f} "
                f"| {pos['buy_date']} | {sl} |")
        out.append("")

    # 当日成交
    today_trades = [t for t in state.get("trades", []) if t["date"] == cur["date"]]
    if today_trades:
        out += ["### 当日成交", "",
                "| 代码 | 动作 | 价格 | 股数 | 盈亏 | 原因 |",
                "|------|------|------|------|------|------|"]
        for t in today_trades:
            pnl = f"¥{t['pnl']:+,.0f}" if t["action"] == "卖出" else "—"
            out.append(
                f"| {t['code']} | {t['action']} | {t['price']:.2f} | {t['shares']} "
                f"| {pnl} | {t['reason']} |")
        out.append("")

    # 权益曲线（最近10天）
    if len(hist) > 1:
        out += ["### 权益曲线（最近10天）", "",
                "| 日期 | 总权益 | 累计盈亏 | 持仓数 |",
                "|------|--------|---------|--------|"]
        for h in hist[-10:]:
            out.append(f"| {h['date']} | ¥{h['equity']:,.0f} "
                       f"| {h['total_pnl_pct']:+.2%} | {h['n_positions']} |")
        out.append("")
    return out


def main() -> None:
    date_str = today_str()
    logger.info("=" * 55)
    logger.info("A 股缠论选股")
    logger.info("=" * 55)

    decisions = run_selection()
    if not decisions:
        logger.error("无可分析个股")
        return

    # 手动关注股票：用户输入列表，补跑信号后与全量筛选结果合并去重
    watchlist_codes: Optional[Set[str]] = None
    watchlist = _load_watchlist()
    if watchlist:
        watchlist_input = set(watchlist)
        existing = {d.code for d in decisions}
        # 全量筛选中已有的关注股票直接纳入 watchlist_codes，不重复跑
        extra = _analyse_watchlist(watchlist, existing)
        if extra:
            logger.info(f"[Watchlist] 补入 {len(extra)} 支关注股票")
            merged = {d.code: d for d in decisions}
            for d in extra:
                merged[d.code] = d
            decisions = sorted(merged.values(), key=lambda d: d.score, reverse=True)
        # 记录所有关注代码（含已在全量结果里的），供报告分区
        watchlist_codes = watchlist_input

    # B 迟滞：抑制"昨 Buy→今 Avoid"隔夜翻转（需连续确认才清仓）
    apply_hysteresis_ashare(decisions, date_str)

    # 持仓跟踪：对 holdings.txt 中的个人持仓逐支缠论体检
    holdings = evaluate_holdings()
    if holdings:
        logger.info(f"[Holdings] 体检 {len(holdings)} 支持仓")

    # 模拟组合：按策略信号自动买卖、跨日追踪（不改策略，仅记账）
    portfolio = _run_portfolio(decisions, date_str)
    pf_cur = portfolio["history"][-1]
    logger.info(
        f"[Portfolio] 权益¥{pf_cur['equity']:,.0f} "
        f"累计{pf_cur['total_pnl_pct']:+.2%} 持仓{pf_cur['n_positions']}支 "
        f"现金¥{pf_cur['cash']:,.0f}")

    out_dir = Path("output") / "ashare" / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    # CSV
    rows = _decisions_to_rows(decisions)
    csv_path = out_dir / "ashare_selection.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    # Markdown
    md_path = out_dir / "ashare_selection.md"
    md_path.write_text(
        build_report(decisions, date_str, watchlist_codes, holdings, portfolio),
        encoding="utf-8")

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
