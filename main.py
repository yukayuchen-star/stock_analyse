import copy
import sys
from pathlib import Path
from typing import Dict, List, Tuple
from loguru import logger

import utils.logger  # 触发 setup_logger()
from config.settings   import settings
from config.stocks     import (
    STOCK_POOL, BENCHMARKS, BUCKETS,
    PORTFOLIO_INITIAL_CAPITAL, PORTFOLIO_LOT_SIZE,
)
from config.pool_manager import (
    PoolChange, append_pool_changes, load_dynamic_pool, save_pool_snapshot,
)
from data.pipeline import DataPipeline
from data.universe import get_universe
from signals.quant.factor_engine import compute_quant_signal, QuantSignalResult
from signals.macro.macro_signal  import compute_macro_signal
from signals.chan.chan_signal    import compute_chan_signal, ChanSignalResult
from signals.screening           import (
    ScreeningCandidate, screen_for_adds, screen_for_removes,
)
from decision.strategy           import make_decision, StockDecision
from decision.hysteresis         import apply_hysteresis
from decision.portfolio_core     import (
    Signal, load_portfolio, save_portfolio, update_portfolio,
)
from report.report_writer        import write_all_reports
from backtest.engine             import run_all_backtests
from backtest.report             import write_backtest_report
from backtest.forward_tracker    import (
    log_signals, evaluate_pending, write_forward_report,
)
from utils.time_utils   import today_str, prev_trading_day
from utils.housekeeping import cleanup_old_files


# ── 量化 + 缠论批量执行 ────────────────────────────────────────

def _run_quant_for_pool(
    pool: List[str],
    buckets: Dict[str, List[str]],
    prices: Dict,
    fundamentals: Dict[str, dict],
) -> Dict[str, QuantSignalResult]:
    """跑 P2 量化层；不在任何 bucket 的股票以全池为同行组。"""
    results: Dict[str, QuantSignalResult] = {}

    for bname, btickers in buckets.items():
        for ticker in btickers:
            if ticker not in pool:   # bucket 已被清理但仍残留时跳过
                continue
            info = fundamentals.get(ticker, {})
            results[ticker] = compute_quant_signal(ticker, prices, btickers, info)

    # F1 兜底：pool 中无 bucket 归属的股票，以全池为同行组
    for ticker in pool:
        if ticker not in results:
            info = fundamentals.get(ticker, {})
            results[ticker] = compute_quant_signal(ticker, prices, pool, info)

    return results


def _run_chan_for_pool(pool: List[str], prices: Dict) -> Dict[str, ChanSignalResult]:
    return {t: compute_chan_signal(t, prices) for t in pool}


# ── 交互式池编辑器 ──────────────────────────────────────────────

def _print_pool_state(
    core_pool: List[str],
    dynamic_pool: List[str],
    buckets: Dict[str, List[str]],
) -> None:
    print(f"\nCore  ({len(core_pool):2d}): {', '.join(core_pool)}")
    print(f"Dynam ({len(dynamic_pool):2d}): {', '.join(dynamic_pool) or '(空)'}")
    for bname, btickers in buckets.items():
        print(f"  [{bname}]: {', '.join(btickers)}")


def _parse_selection(raw: str, n: int) -> List[int]:
    """解析 '1,2,3' / 'all' / 空 → 索引列表（0-based）。"""
    raw = raw.strip().lower()
    if not raw:
        return []
    if raw == "all":
        return list(range(n))
    indices = []
    for part in raw.split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        i = int(part) - 1
        if 0 <= i < n:
            indices.append(i)
    return list(dict.fromkeys(indices))


def _interactive_pool_editor(
    core_pool: List[str],
    dynamic_pool: List[str],
    buckets: Dict[str, List[str]],
    add_candidates: List[ScreeningCandidate],
    remove_candidates: List[ScreeningCandidate],
) -> Tuple[List[str], List[str], Dict[str, List[str]], List[PoolChange]]:
    """
    展示候选 + 当前池，让用户决定 add/remove。
    返回 (final_pool, new_dynamic, new_buckets, changes_log)。
    非 TTY 时直接返回当前池，不接受任何候选。
    """
    date_str     = today_str()
    dynamic_pool = list(dynamic_pool)
    buckets      = copy.deepcopy(buckets)
    changes: List[PoolChange] = []

    if not sys.stdin.isatty():
        logger.info("[Editor] 非 TTY 环境，跳过编辑器，沿用当前池")
        return (sorted(set(core_pool) | set(dynamic_pool)), dynamic_pool, buckets, changes)

    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  股票池编辑器 — {date_str}")
    print(sep)
    _print_pool_state(core_pool, dynamic_pool, buckets)

    # ── 加仓候选 ───────────────────────────────────────────
    print(f"\n── 加仓候选 ({len(add_candidates)} 只，已通过预过滤+量化筛选) ──")
    if add_candidates:
        for i, c in enumerate(add_candidates, 1):
            print(f"  {i}. {c.ticker:6s} score={c.score:+.3f}  {c.reasoning}")
        sel = input(f"接受哪几只？[1-{len(add_candidates)} / all / 空跳过]: ")
        for idx in _parse_selection(sel, len(add_candidates)):
            c = add_candidates[idx]
            if c.ticker not in core_pool and c.ticker not in dynamic_pool:
                dynamic_pool.append(c.ticker)
                buckets.setdefault("custom", []).append(c.ticker)
                changes.append(PoolChange(
                    date=date_str, action="add", ticker=c.ticker,
                    reason=c.reasoning, score=c.score, source="auto-screen",
                ))
                print(f"  + {c.ticker} → dynamic_pool [custom]")
    else:
        print("  (无)")

    # ── 减仓候选 ───────────────────────────────────────────
    print(f"\n── 减仓候选 ({len(remove_candidates)} 只，dynamic 池中弱势股) ──")
    if remove_candidates:
        for i, c in enumerate(remove_candidates, 1):
            print(f"  {i}. {c.ticker:6s} score={c.score:+.3f}  {c.reasoning}")
        sel = input(f"移除哪几只？[1-{len(remove_candidates)} / all / 空跳过]: ")
        for idx in _parse_selection(sel, len(remove_candidates)):
            c = remove_candidates[idx]
            if c.ticker in dynamic_pool:
                dynamic_pool.remove(c.ticker)
                for btickers in buckets.values():
                    if c.ticker in btickers:
                        btickers.remove(c.ticker)
                changes.append(PoolChange(
                    date=date_str, action="remove", ticker=c.ticker,
                    reason=c.reasoning, score=c.score, source="auto-screen",
                ))
                print(f"  - {c.ticker} ← dynamic_pool")
    else:
        print("  (无)")

    # ── 手动添加 ───────────────────────────────────────────
    raw = input("\n手动添加股票（逗号分隔 / 空跳过）: ").strip()
    if raw:
        for ticker in [t.strip().upper() for t in raw.split(",") if t.strip()]:
            if ticker.startswith("^"):
                print(f"  ✗ {ticker} 是指数代码，不可加入")
                continue
            clean = ticker.replace(".", "").replace("-", "")
            if not clean.isalnum() or clean.isdigit():
                print(f"  ✗ 格式无效: {ticker}")
                continue
            if ticker in core_pool or ticker in dynamic_pool:
                print(f"  ! {ticker} 已在池中")
                continue
            dynamic_pool.append(ticker)
            buckets.setdefault("custom", []).append(ticker)
            changes.append(PoolChange(
                date=date_str, action="add", ticker=ticker,
                reason="manual", source="user",
            ))
            print(f"  + {ticker} → dynamic_pool [custom]")

    # ── 手动移除（core 受保护）─────────────────────────────
    raw = input("手动移除股票（不含 core，逗号分隔 / 空跳过）: ").strip()
    if raw:
        for ticker in [t.strip().upper() for t in raw.split(",") if t.strip()]:
            if ticker in core_pool:
                print(f"  ✗ {ticker} 是 core_pool 不可移除")
                continue
            if ticker not in dynamic_pool:
                print(f"  ! {ticker} 不在 dynamic_pool")
                continue
            dynamic_pool.remove(ticker)
            for btickers in buckets.values():
                if ticker in btickers:
                    btickers.remove(ticker)
            changes.append(PoolChange(
                date=date_str, action="remove", ticker=ticker,
                reason="manual", source="user",
            ))
            print(f"  - {ticker} ← dynamic_pool")

    # 清理空 bucket
    buckets = {k: v for k, v in buckets.items() if v}

    final_pool = sorted(set(core_pool) | set(dynamic_pool))
    if not final_pool:
        print("最终池为空，退出。")
        raise SystemExit(1)

    print(f"\n最终池 ({len(final_pool)} 只): {', '.join(final_pool)}")
    confirm = input("确认运行？[Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        print("已取消，退出。")
        raise SystemExit(0)

    print(f"{sep}\n")
    return final_pool, dynamic_pool, buckets, changes


_PORTFOLIO_PATH = Path(settings.output_dir) / "us_portfolio.json"


def _run_portfolio(decisions: Dict[str, StockDecision],
                   prices: Dict, date_str: str) -> dict:
    """按策略信号推进美股模拟组合一天（不改策略，仅记账）。

    买入：Buy/Overweight 且未持仓 → 目标市值 = 建议仓位 × 初始资金。
    卖出（全部卖出）：评级为 Sell/Underweight，或缠论卖点(s1/s2/s3)经迟滞层
    连续 CONFIRM_DAYS 天确认（chan_sell_confirmed，panic 直通），或跌破结构止损
    （止损不受迟滞约束，风控优先）。成交价 = 信号当日收盘价。
    """
    _BUY  = {"Buy", "Overweight"}
    _SELL = {"Sell", "Underweight"}

    # 当日买入排名（final_score 降序）→ 现金不足时优先靠前的票
    buys_sorted = sorted([d for d in decisions.values() if d.rating in _BUY],
                         key=lambda d: d.final_score, reverse=True)
    rank_of = {d.ticker: i + 1 for i, d in enumerate(buys_sorted)}

    signals: List[Signal] = []
    for ticker, d in decisions.items():
        df = prices.get(ticker)
        price = float(df["Close"].iloc[-1]) if df is not None and not df.empty else 0.0
        signals.append(Signal(
            code=ticker,
            price=price,
            is_buy=(d.rating in _BUY),
            is_sell=(d.rating in _SELL or d.chan_sell_confirmed),
            position_frac=d.suggested_position,
            stop_loss=d.stop_loss or 0.0,
            rank=rank_of.get(ticker, 0),
        ))

    state = load_portfolio(_PORTFOLIO_PATH, PORTFOLIO_INITIAL_CAPITAL)
    update_portfolio(state, date_str, signals, lot_size=PORTFOLIO_LOT_SIZE)
    save_portfolio(_PORTFOLIO_PATH, state)
    return state


def _write_portfolio_report(state: dict, prices: Dict, output_dir: Path,
                            date_str: str) -> Path:
    """写美股模拟组合报告 output/{date}/portfolio.md：权益/盈亏/持仓/成交/曲线。"""
    hist = state.get("history", [])
    cur = hist[-1] if hist else None
    initial = state["initial_capital"]
    price_now = {
        t: float(df["Close"].iloc[-1])
        for t, df in prices.items() if df is not None and not df.empty
    }

    L = [f"# 美股模拟组合 — {date_str}", ""]
    if cur:
        L += [
            f"> 初始资金 ${initial:,.0f}，从启用日起严格按策略 Buy/卖点信号模拟买卖、跨日追踪。",
            "> 成交价=信号当日收盘价，仓位=策略建议；持仓评级转Sell/Underweight、"
            "缠论卖点(s1/s2/s3)连续2天确认（panic直通）、或跌破结构止损则全部卖出。",
            "",
            "| 项目 | 值 |", "|--|--|",
            f"| 总权益 | ${cur['equity']:,.2f} |",
            f"| 累计盈亏 | {cur['total_pnl_pct']:+.2%}（${cur['equity']-initial:+,.2f}）|",
            f"| 持仓市值 | ${cur['market_value']:,.2f} |",
            f"| 可用现金 | ${cur['cash']:,.2f} |",
            f"| 持仓数 | {cur['n_positions']} |",
            f"| 记录天数 | {len(hist)} |",
            "",
        ]
    positions = state.get("positions", {})
    if positions:
        L += ["## 当前持仓（下一交易日初始持仓）", "",
              "| 代码 | 买入价 | 现价 | 浮动盈亏 | 持股 | 市值 | 买入日 | 止损 |",
              "|------|--------|------|---------|------|------|--------|------|"]
        for code, pos in sorted(positions.items(), key=lambda kv: kv[1]["buy_date"]):
            px = price_now.get(code, pos["cost_price"])
            pnl = (px - pos["cost_price"]) / pos["cost_price"] if pos["cost_price"] else 0
            sl = f"{pos['stop_loss']:.2f}" if pos.get("stop_loss") else "—"
            L.append(f"| {code} | {pos['cost_price']:.2f} | {px:.2f} | {pnl:+.1%} "
                     f"| {pos['shares']} | ${pos['shares']*px:,.0f} | {pos['buy_date']} | {sl} |")
        L.append("")
    today_trades = [t for t in state.get("trades", []) if cur and t["date"] == cur["date"]]
    if today_trades:
        L += ["## 当日成交", "",
              "| 代码 | 动作 | 价格 | 股数 | 盈亏 | 原因 |",
              "|------|------|------|------|------|------|"]
        for t in today_trades:
            pnl = f"${t['pnl']:+,.0f}" if t["action"] == "卖出" else "—"
            L.append(f"| {t['code']} | {t['action']} | {t['price']:.2f} | {t['shares']} "
                     f"| {pnl} | {t['reason']} |")
        L.append("")
    if len(hist) > 1:
        L += ["## 权益曲线（最近15天）", "",
              "| 日期 | 总权益 | 累计盈亏 | 持仓数 |", "|------|--------|---------|--------|"]
        for h in hist[-15:]:
            L.append(f"| {h['date']} | ${h['equity']:,.0f} | {h['total_pnl_pct']:+.2%} | {h['n_positions']} |")
        L.append("")
    path = output_dir / "portfolio.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


# ── 主流程 ──────────────────────────────────────────────────────

def run() -> None:
    date_str   = today_str()
    output_dir = Path(settings.output_dir) / date_str
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 清理 7 天前的缓存与输出 ────────────────────────────
    cleanup_old_files()

    # ── 加载 core + dynamic ────────────────────────────────
    core_pool    = list(STOCK_POOL)
    dynamic_pool = load_dynamic_pool()
    current_pool = sorted(set(core_pool) | set(dynamic_pool))
    logger.info(f"启动池: core={len(core_pool)} dynamic={len(dynamic_pool)} 合并={len(current_pool)}")

    # ── universe 扫描 → add 候选 ──────────────────────────
    pipeline = DataPipeline()

    # ── 前向验证：评估 5 TD 前的信号 ─────────────────────────
    logger.info("── 前向信号验证（评估已到期信号）──")
    n_eval = evaluate_pending(pipeline)
    if not n_eval:
        logger.info("  [ForwardTracker] 暂无到期待评估信号")

    logger.info("── universe 扫描（add 候选）──")
    universe   = get_universe(nasdaq_top=30)
    add_cands  = screen_for_adds(
        pipeline=pipeline,
        universe=universe,
        exclude_pool=current_pool,
        benchmarks=BENCHMARKS,
    )

    # ── P1 数据 + P2 量化 + P4 缠论（current_pool）────────
    logger.info(f"{'='*50}")
    logger.info(f"美股量化分析系统  {date_str}")
    logger.info(f"{'='*50}")
    logger.info(f"数据基准日 (t-1): {prev_trading_day()}")

    data         = pipeline.fetch_all(stock_pool=current_pool)
    prices       = data["prices"]
    snapshot     = data["snapshot"]
    fundamentals = data["fundamentals"]
    logger.info(
        f"数据就绪: {len(prices)} 只价格 / "
        f"VIX={snapshot.get('VIXCLS', 'N/A'):.2f} / "
        f"10Y={snapshot.get('DGS10',  'N/A'):.2f}% / "
        f"{len(fundamentals)} 只基本面"
    )

    logger.info("── P2 量化（current_pool）──")
    quant_init = _run_quant_for_pool(current_pool, BUCKETS, prices, fundamentals)
    logger.info("── P4 缠论（current_pool）──")
    chan_init  = _run_chan_for_pool(current_pool, prices)

    # ── remove 候选（基于 current 的 quant + chan）────────
    remove_cands = screen_for_removes(
        quant_results=quant_init,
        chan_results=chan_init,
        dynamic_pool=dynamic_pool,
    )

    # ── 编辑器：展示 + 用户确认 ──────────────────────────
    final_pool, new_dynamic, buckets, changes = _interactive_pool_editor(
        core_pool=core_pool,
        dynamic_pool=dynamic_pool,
        buckets=BUCKETS,
        add_candidates=add_cands,
        remove_candidates=remove_cands,
    )

    # ── 池如有变化：补抓 delta + 复跑 quant/chan ──────────
    if set(final_pool) != set(current_pool):
        logger.info("池变更：补抓数据 + 复跑信号 ...")
        data         = pipeline.fetch_all(stock_pool=final_pool)
        prices       = data["prices"]
        snapshot     = data["snapshot"]
        fundamentals = data["fundamentals"]
        quant_signals = _run_quant_for_pool(final_pool, buckets, prices, fundamentals)
        chan_signals  = _run_chan_for_pool(final_pool, prices)
    else:
        quant_signals = quant_init
        chan_signals  = chan_init

    # ── 日志输出 ─────────────────────────────────────────
    logger.info(f"最终池 ({len(final_pool)} 只): {final_pool}")
    logger.info("── P2 量化得分 ──")
    for ticker, r in quant_signals.items():
        logger.info(f"  {ticker:5s}: {r.reasoning}")
    logger.info("── P4 缠论得分 ──")
    for ticker, r in chan_signals.items():
        point = r.buy_point_type or r.sell_point_type or "neutral"
        logger.info(
            f"  {ticker:5s}: {point:8s} score={r.score:+.2f} "
            f"笔={r.stroke_count:2d} 中枢={'有' if r.current_pivot else '无'} "
            f"周线={r.weekly_trend:7s} res={r.level_resonance} conf={r.confidence:.2f}"
        )

    # ── P3 宏观 ──────────────────────────────────────────
    logger.info("── P3 宏观信号层 ──")
    macro = compute_macro_signal(snapshot, prices, buckets)
    logger.info(
        f"  VIX={macro.vix_level:.1f} [{macro.vix_regime}] "
        f"仓位上限={macro.position_limit:.0%}  "
        f"yield_spread={macro.yield_spread:+.2f}%  "
        f"macro_score={macro.score:+.3f}"
    )
    logger.info("  桶 IR: " + "  ".join(
        f"{k}={macro.bucket_ir[k]:+.3f}(score={macro.bucket_scores[k]:+.2f})"
        for k in macro.bucket_ir
    ))

    # ── P5 决策 ──────────────────────────────────────────
    logger.info("── P5 决策层 ──")
    decisions: dict[str, StockDecision] = {}
    for ticker in final_pool:
        d = make_decision(
            ticker=ticker,
            chan=chan_signals[ticker],
            quant=quant_signals[ticker],
            macro=macro,
            prices=prices,
        )
        decisions[ticker] = d

    # ── B 迟滞：抑制"昨多→今出"隔夜翻转（需连续确认才清仓）──
    apply_hysteresis(decisions, date_str)

    # ── 模拟组合：按策略信号自动买卖、跨日追踪（不改策略，仅记账）──
    portfolio = _run_portfolio(decisions, prices, date_str)
    pf_cur = portfolio["history"][-1]
    logger.info(
        f"── 模拟组合 ── 权益 ${pf_cur['equity']:,.0f} "
        f"累计 {pf_cur['total_pnl_pct']:+.2%} 持仓 {pf_cur['n_positions']}支 "
        f"现金 ${pf_cur['cash']:,.0f}")

    logger.info("── P5 综合评级排行 ──")
    for d in sorted(decisions.values(), key=lambda x: x.final_score, reverse=True):
        bar   = _score_bar(d.final_score)
        flags = " | ".join(d.risk_flags) if d.risk_flags else "—"
        entry = f"{d.entry_price_range[0]:.1f}~{d.entry_price_range[1]:.1f}"
        logger.info(
            f"  {d.ticker:5s} [{d.rating:11s}] {bar} {d.final_score:+.3f}  "
            f"pos={d.suggested_position:.0%}  "
            f"SL={d.stop_loss:.1f}  TP={d.take_profit:.1f}  entry={entry}"
        )
        logger.info(f"         得分: {d.score_reasoning}")
        if d.risk_flags:
            logger.info(f"         风控: {flags}")

    # ── 前向验证：记录今日买入信号 ───────────────────────────
    log_signals(decisions=decisions, buckets=buckets, date_str=date_str, prices=prices)

    # ── P6 报告 ──────────────────────────────────────────
    logger.info("── P6 报告层 ──")
    written = write_all_reports(
        decisions=decisions,
        macro=macro,
        date_str=date_str,
        output_dir=output_dir,
    )
    for p in written:
        logger.info(f"  已写入: {p}")

    pf_path = _write_portfolio_report(portfolio, prices, output_dir, date_str)
    logger.info(f"  已写入: {pf_path}")

    logger.info("── 量化评分排行（按 score 降序）──")
    for r in sorted(quant_signals.values(), key=lambda x: x.score, reverse=True):
        bar = _score_bar(r.score)
        logger.info(
            f"  {r.ticker:5s} {bar} {r.score:+.3f}  "
            f"fund={r.fundamental_score:+.2f} "
            f"trend={r.trend_score:+.2f} "
            f"mom={r.momentum_score:+.2f} "
            f"rel={r.relative_strength_score:+.2f} "
            f"vol={r.volume_score:+.2f}"
        )

    # ── P7 回测 ──────────────────────────────────────────
    logger.info("── P7 回测层（缠论信号 walk-forward）──")
    bt_results = run_all_backtests(pipeline, final_pool)
    bt_path    = write_backtest_report(bt_results, output_dir, date_str)
    for ticker, r in bt_results.items():
        logger.info(f"  {ticker:5s}: {r.reasoning}")
    logger.info(f"  回测报告: {bt_path}")

    # ── P8 前向验证报告 ───────────────────────────────────
    logger.info("── P8 前向信号验证报告 ──")
    fv_path = write_forward_report(date_str, output_dir)
    logger.info(f"  前向验证报告: {fv_path}")

    # ── 落盘：池快照 + 变更日志 ──────────────────────────
    save_pool_snapshot(
        date_str=date_str,
        core_pool=core_pool,
        dynamic_pool=new_dynamic,
        buckets=buckets,
        decisions={
            t: {"rating": d.rating, "score": d.final_score}
            for t, d in decisions.items()
        },
    )
    append_pool_changes(changes)

    logger.info(f"{'='*50}")
    logger.info("P1 + P2 + P3 + P4 + P5 + P6 + P7 + P8 运行完毕 ✓")
    logger.info(f"报告目录: {output_dir}")


def _score_bar(score: float, width: int = 10) -> str:
    filled = round((score + 1) / 2 * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


if __name__ == "__main__":
    run()
