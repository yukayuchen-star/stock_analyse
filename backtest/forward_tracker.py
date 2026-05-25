"""
前向信号验证模块 (Forward Signal Tracker)

原理：
  记录每日 Buy/Overweight 信号，5 个交易日后（≈7 历日）
  回头评估该信号是否盈利，衡量策略真实有效性。

流程：
  1. log_signals()      — 每日分析结束后记录信号
  2. evaluate_pending() — 查找已到期未评估的信号并计算盈亏
  3. write_forward_report() — 生成验证报告写入 output/{date}/

存储：cache/forward_signals.db（独立 SQLite，不与价格缓存混用）

评估规则：
  - 入场价：信号日收盘价（与 SL/TP 同价基；缺价格时回退到 entry_price_range 中点）
  - 出场：第 5 个交易日收盘，或止损先触及时以 min(止损价, 当日开盘) 计（含跳空穿越）
  - 止损检测：逐日扫描 Low <= stop_loss
  - 方向：仅做多（Buy/Overweight）
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from decision.strategy import StockDecision

# ── 常量 ────────────────────────────────────────────────────────

HOLD_TRADING_DAYS = 5        # 持仓周期（交易日，≈1 周）
REPORT_WINDOW_DAYS = 90      # 报告统计窗口（自然日）
MIN_REPORT_TRADES = 3        # 报告所需最少已评估数
BUY_RATINGS = {"Buy", "Overweight"}

DB_PATH = Path("cache") / "forward_signals.db"


# ── 数据库 ────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS forward_signals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_date  TEXT NOT NULL,
            ticker       TEXT NOT NULL,
            rating       TEXT,
            signal_type  TEXT,
            bucket       TEXT,
            entry_price  REAL,
            stop_loss    REAL,
            take_profit  REAL,
            final_score  REAL,
            UNIQUE(logged_date, ticker)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS forward_outcomes (
            signal_id     INTEGER PRIMARY KEY REFERENCES forward_signals(id),
            eval_date     TEXT NOT NULL,
            hold_days     INTEGER,
            exit_price    REAL,
            pnl_pct       REAL,
            hit_stop      INTEGER,
            stop_hit_date TEXT,
            outcome       TEXT
        )
    """)
    c.commit()
    return c


# ── 信号记录 ──────────────────────────────────────────────────────

def log_signals(
    decisions: Dict[str, StockDecision],
    buckets: Dict[str, List[str]],
    date_str: str,
    prices: Optional[Dict[str, "pd.DataFrame"]] = None,
) -> int:
    """
    记录当日 Buy/Overweight 决策到数据库。
    同一天同一 ticker 已存在时跳过（UNIQUE 约束）。
    返回新增信号数。

    入场价取信号日收盘价（与 stop_loss/take_profit 同一价基，
    二者均由 current_price 推导）。无价格时回退到 entry_price_range 中点。
    """
    ticker_bucket: Dict[str, str] = {}
    for bname, tlist in buckets.items():
        for t in tlist:
            ticker_bucket[t] = bname

    c = _conn()
    inserted = 0
    for ticker, d in decisions.items():
        if d.rating not in BUY_RATINGS:
            continue
        # 入场价 = 信号日收盘价（与 SL/TP 同价基）；缺价格时回退到入场区间中点。
        # logged_date 锚定到该收盘 bar 的真实日期（数据基准 = close[t-1]），
        # 而非调用方传入的日历 today——否则评估期 df.index>logged_date 会与
        # 入场 bar 错位（少算/多算一日），并使去重键随日历日漂移。
        entry_price = 0.0
        entry_date  = date_str
        df_t = prices.get(ticker) if prices else None
        if df_t is not None and not df_t.empty:
            entry_price = float(df_t["Close"].iloc[-1])
            entry_date  = str(pd.Timestamp(df_t.index[-1]).date())
        if entry_price <= 0:
            entry_price = (d.entry_price_range[0] + d.entry_price_range[1]) / 2.0
        if entry_price <= 0:
            continue
        signal_type = d.chan_signal.buy_point_type if d.chan_signal else None
        bucket = ticker_bucket.get(ticker, "other")
        try:
            c.execute(
                """INSERT OR IGNORE INTO forward_signals
                   (logged_date, ticker, rating, signal_type, bucket,
                    entry_price, stop_loss, take_profit, final_score)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (entry_date, ticker, d.rating, signal_type, bucket,
                 entry_price, d.stop_loss, d.take_profit, d.final_score),
            )
            if c.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as exc:
            logger.warning(f"[ForwardTracker] log {ticker}: {exc}")
    c.commit()
    c.close()
    if inserted:
        logger.info(f"[ForwardTracker] 记录买入信号 {inserted} 只 ({date_str})")
    return inserted


# ── 信号评估 ──────────────────────────────────────────────────────

def evaluate_pending(pipeline) -> int:
    """
    遍历未评估的信号，若已过 HOLD_TRADING_DAYS 个交易日则计算盈亏。
    返回本次评估完成的信号数。
    """
    c = _conn()
    pending = c.execute("""
        SELECT fs.*
        FROM forward_signals fs
        LEFT JOIN forward_outcomes fo ON fo.signal_id = fs.id
        WHERE fo.signal_id IS NULL
        ORDER BY fs.logged_date
    """).fetchall()
    c.close()

    if not pending:
        return 0

    today_str = datetime.today().strftime("%Y-%m-%d")
    evaluated = 0

    # 按 ticker 分组批量拉价格
    by_ticker: Dict[str, list] = defaultdict(list)
    for row in pending:
        by_ticker[row["ticker"]].append(row)

    c = _conn()
    for ticker, rows in by_ticker.items():
        try:
            df = pipeline.get_backtest_price(ticker)
        except Exception as exc:
            logger.warning(f"[ForwardTracker] {ticker} 拉价格失败: {exc}")
            continue
        if df is None or df.empty:
            logger.warning(f"[ForwardTracker] {ticker}: 无价格数据")
            continue

        first_bar = df.index.min()

        for row in rows:
            logged_date = row["logged_date"]
            entry_price = row["entry_price"]
            stop_loss   = row["stop_loss"] or 0.0

            # 信号日须落在价格窗口内：否则 df.index>logged_date 会把整段历史
            # 当作"未来"，iloc[4] 取到错误的出场 bar，产生虚假盈亏。
            if pd.Timestamp(logged_date) < first_bar:
                logger.warning(
                    f"[ForwardTracker] {ticker} {logged_date} 早于价格窗口"
                    f"({first_bar.date()})，无法定位入场日，跳过"
                )
                continue

            # 多头止损必须低于入场价；若不然（价基不一致等），视为无效，不扫描止损
            if stop_loss >= entry_price:
                stop_loss = 0.0

            # 确认已过足够交易日
            future = df[df.index > logged_date]
            if len(future) < HOLD_TRADING_DAYS:
                continue  # 数据不足，继续等待

            # 第 HOLD_TRADING_DAYS 个交易日作为常规出场日
            exit_bar   = future.iloc[HOLD_TRADING_DAYS - 1]
            exit_date  = str(future.index[HOLD_TRADING_DAYS - 1].date())
            exit_price = float(exit_bar["Close"])

            # 扫描 hold 区间是否触及止损
            hold_window = future.iloc[:HOLD_TRADING_DAYS]
            hit_stop = 0
            stop_hit_date: Optional[str] = None
            if stop_loss > 0:
                for bar_ts, bar in hold_window.iterrows():
                    if float(bar["Low"]) <= stop_loss:
                        hit_stop = 1
                        stop_hit_date = str(bar_ts.date())
                        # 跳空穿越：若开盘已低于止损，成交价为开盘价（更差），
                        # 而非理想化的止损价——否则会系统性高估止损单收益
                        exit_price = min(stop_loss, float(bar["Open"]))
                        exit_date  = stop_hit_date
                        break

            pnl_pct = (exit_price - entry_price) / entry_price
            outcome = "WIN" if pnl_pct > 0 else "LOSS"

            c.execute("""
                INSERT OR REPLACE INTO forward_outcomes
                (signal_id, eval_date, hold_days, exit_price, pnl_pct,
                 hit_stop, stop_hit_date, outcome)
                VALUES (?,?,?,?,?,?,?,?)
            """, (row["id"], exit_date, HOLD_TRADING_DAYS,
                  exit_price, pnl_pct, hit_stop, stop_hit_date, outcome))
            evaluated += 1
            sl_tag = f" ⚠️止损{stop_hit_date}" if hit_stop else ""
            logger.debug(
                f"[ForwardTracker] {ticker} {logged_date}→{exit_date}: "
                f"{pnl_pct:+.1%} {'✅' if outcome == 'WIN' else '❌'}{sl_tag}"
            )

    c.commit()
    c.close()
    if evaluated:
        logger.info(f"[ForwardTracker] 完成评估 {evaluated} 只信号")
    return evaluated


# ── 报告生成 ──────────────────────────────────────────────────────

def _format_pf(profit_factor: float) -> str:
    return "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}"


def build_report(date_str: str) -> str:
    """返回 Markdown 格式的前向验证报告字符串。"""
    c = _conn()
    cutoff = (datetime.today() - timedelta(days=REPORT_WINDOW_DAYS)).strftime("%Y-%m-%d")

    rows = c.execute("""
        SELECT fs.ticker, fs.logged_date, fs.signal_type, fs.bucket,
               fs.entry_price, fs.stop_loss, fs.final_score,
               fo.exit_price, fo.pnl_pct, fo.hit_stop,
               fo.stop_hit_date, fo.outcome, fo.eval_date
        FROM forward_signals fs
        JOIN forward_outcomes fo ON fo.signal_id = fs.id
        WHERE fs.logged_date >= ?
        ORDER BY fs.logged_date DESC
    """, (cutoff,)).fetchall()

    pending_count = c.execute("""
        SELECT COUNT(*) FROM forward_signals fs
        LEFT JOIN forward_outcomes fo ON fo.signal_id = fs.id
        WHERE fo.signal_id IS NULL
    """).fetchone()[0]

    total_logged = c.execute(
        "SELECT COUNT(*) FROM forward_signals WHERE logged_date >= ?", (cutoff,)
    ).fetchone()[0]

    c.close()

    lines = [
        f"# 前向信号验证报告 — {date_str}",
        "",
        f"> **方法说明**  ",
        f"> 每日记录 Buy/Overweight 买入信号，持仓 **{HOLD_TRADING_DAYS} 个交易日**（≈1周）后评估盈亏。  ",
        "> 入场价 = 信号日收盘价；止损触及时以 min(止损价,当日开盘) 出场（含跳空）；方向仅做多。  ",
        f"> 统计窗口：最近 {REPORT_WINDOW_DAYS} 天",
        "",
        f"| | 数量 |",
        f"|--|------|",
        f"| 窗口内记录信号 | {total_logged} |",
        f"| 已评估 | {len(rows)} |",
        f"| 待评估（持仓中） | {pending_count} |",
        "",
    ]

    if len(rows) < MIN_REPORT_TRADES:
        lines.append(
            f"> 已评估信号不足（{len(rows)} / {MIN_REPORT_TRADES}），数据积累中…\n"
        )
        return "\n".join(lines)

    pnls   = [r["pnl_pct"] for r in rows]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate     = len(wins) / len(pnls)
    avg_pnl      = float(np.mean(pnls))
    avg_win      = float(np.mean(wins))  if wins   else 0.0
    avg_loss     = float(np.mean(losses)) if losses else 0.0
    sum_wins     = sum(wins)
    sum_losses   = abs(sum(losses))
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else float("inf")
    expected_val  = win_rate * avg_win + (1 - win_rate) * avg_loss
    stop_hits     = sum(1 for r in rows if r["hit_stop"])

    lines += [
        "## 综合指标",
        "",
        "| 指标 | 值 |",
        "|------|----|",
        f"| 胜率 | {win_rate:.1%} |",
        f"| 平均盈亏 | {avg_pnl:+.2%} |",
        f"| 平均盈利（赢单） | {avg_win:+.2%} |",
        f"| 平均亏损（亏单） | {avg_loss:+.2%} |",
        f"| 盈亏比 (Profit Factor) | {_format_pf(profit_factor)} |",
        f"| 期望值 (Expected Value) | {expected_val:+.2%} |",
        f"| 止损触发率 | {stop_hits/len(rows):.1%} ({stop_hits}/{len(rows)}) |",
        "",
    ]

    # ── 按缠论信号类型 ────────────────────────────────────────────
    sig_groups: Dict[str, list] = defaultdict(list)
    for r in rows:
        sig_groups[r["signal_type"] or "unknown"].append(r["pnl_pct"])

    lines += [
        "## 按缠论买点类型",
        "",
        "| 类型 | 次数 | 胜率 | 均盈亏 |",
        "|------|------|------|--------|",
    ]
    for sig, plist in sorted(sig_groups.items()):
        wr = sum(1 for p in plist if p > 0) / len(plist)
        lines.append(
            f"| {sig.upper()} | {len(plist)} | {wr:.0%} | {np.mean(plist):+.2%} |"
        )

    # ── 按板块 ───────────────────────────────────────────────────
    bkt_groups: Dict[str, list] = defaultdict(list)
    for r in rows:
        bkt_groups[r["bucket"] or "other"].append(r["pnl_pct"])

    lines += [
        "",
        "## 按板块",
        "",
        "| 板块 | 次数 | 胜率 | 均盈亏 |",
        "|------|------|------|--------|",
    ]
    for bkt, plist in sorted(bkt_groups.items()):
        wr = sum(1 for p in plist if p > 0) / len(plist)
        lines.append(
            f"| {bkt} | {len(plist)} | {wr:.0%} | {np.mean(plist):+.2%} |"
        )

    # ── 最近明细 ─────────────────────────────────────────────────
    lines += [
        "",
        "## 最近信号明细（最新 15 笔）",
        "",
        "| 信号日 | 股票 | 缠论 | 入场价 | 出场价 | 盈亏 | 结果 |",
        "|--------|------|------|--------|--------|------|------|",
    ]
    for r in rows[:15]:
        icon = "✅" if r["outcome"] == "WIN" else "❌"
        sl_tag = f" ⚠️止损" if r["hit_stop"] else ""
        lines.append(
            f"| {r['logged_date']} | {r['ticker']} "
            f"| {(r['signal_type'] or '—').upper()} "
            f"| {r['entry_price']:.2f} | {r['exit_price']:.2f} "
            f"| {r['pnl_pct']:+.2%} | {icon}{sl_tag} |"
        )

    lines += [
        "",
        "---",
        f"*生成时间: {date_str}  |  持仓 {HOLD_TRADING_DAYS} 交易日  |  统计窗口 {REPORT_WINDOW_DAYS} 天*",
    ]
    return "\n".join(lines)


def write_forward_report(date_str: str, output_dir: Path) -> Path:
    """将前向验证报告写入文件，返回路径。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    content = build_report(date_str)
    path = output_dir / "forward_validation.md"
    path.write_text(content, encoding="utf-8")
    return path
