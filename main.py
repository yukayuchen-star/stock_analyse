import copy
import sys
from pathlib import Path
from loguru import logger

import utils.logger  # 触发 setup_logger()
from config.settings import settings
from config.stocks import STOCK_POOL, BENCHMARKS, BUCKETS
from data.pipeline import DataPipeline
from signals.quant.factor_engine import compute_quant_signal
from signals.macro.macro_signal  import compute_macro_signal
from signals.chan.chan_signal     import compute_chan_signal
from decision.strategy           import make_decision, StockDecision
from report.report_writer        import write_all_reports
from backtest.engine             import run_all_backtests
from backtest.report             import write_backtest_report
from utils.time_utils import today_str, prev_trading_day


def _interactive_pool_editor(
    pool: list[str],
    buckets: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    """
    交互式编辑股票池。非 TTY（cron/pipe）时直接跳过，使用默认配置。
    返回 (最终pool, 最终buckets)。
    """
    if not sys.stdin.isatty():
        return pool, buckets

    pool    = list(pool)
    buckets = copy.deepcopy(buckets)

    sep = "=" * 52
    print(f"\n{sep}")
    print("  股票池编辑器（直接回车跳过任一步骤）")
    print(sep)
    print(f"当前股票池 ({len(pool)} 只): {', '.join(pool)}")
    for bname, btickers in buckets.items():
        print(f"  [{bname}]: {', '.join(btickers)}")
    print()

    # ── 添加 ────────────────────────────────────────────
    raw_add = input("添加股票代码（逗号分隔）: ").strip()
    if raw_add:
        for ticker in [t.strip().upper() for t in raw_add.split(",") if t.strip()]:
            # F2: 先拒绝以 ^ 开头的指数代码（不可直接买卖）
            if ticker.startswith("^"):
                print(f"  ✗ {ticker} 是指数代码，不可加入股票池")
                continue
            # 格式校验：去除合法分隔符后必须全为字母数字，且不能纯数字
            clean = ticker.replace(".", "").replace("-", "")
            if not clean.isalnum() or clean.isdigit():
                print(f"  ✗ 格式无效，跳过: {ticker}")
                continue
            if ticker in pool:
                print(f"  ! {ticker} 已在池中")
            else:
                pool.append(ticker)
                buckets.setdefault("custom", []).append(ticker)
                print(f"  + 已添加 {ticker} → [custom]")

    # ── 删除 ────────────────────────────────────────────
    print(f"\n当前股票池 ({len(pool)} 只): {', '.join(pool)}")
    raw_del = input("删除股票代码（逗号分隔）: ").strip()
    if raw_del:
        for ticker in [t.strip().upper() for t in raw_del.split(",") if t.strip()]:
            if ticker in pool:
                pool.remove(ticker)
                for btickers in buckets.values():
                    if ticker in btickers:
                        btickers.remove(ticker)
                print(f"  - 已删除 {ticker}")
            else:
                print(f"  ! {ticker} 不在池中，跳过")
        # 移除因删除而空掉的 bucket
        buckets = {k: v for k, v in buckets.items() if v}

    # ── 确认 ────────────────────────────────────────────
    if not pool:
        print("股票池为空，退出。")
        raise SystemExit(1)

    print(f"\n最终股票池 ({len(pool)} 只): {', '.join(pool)}")
    confirm = input("确认使用此股票池？[Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        print("已取消，退出。")
        raise SystemExit(0)

    print(f"{sep}\n")
    return pool, buckets


def run() -> None:
    date_str   = today_str()
    output_dir = Path(settings.output_dir) / date_str
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 股票池编辑（交互式，非 TTY 时跳过）─────────────────
    stock_pool, buckets = _interactive_pool_editor(list(STOCK_POOL), BUCKETS)

    logger.info(f"{'='*50}")
    logger.info(f"美股量化分析系统  {date_str}")
    logger.info(f"{'='*50}")
    logger.info(f"股票池 ({len(stock_pool)} 只): {stock_pool}")
    logger.info(f"基准: {BENCHMARKS}")
    logger.info(f"数据基准日 (t-1): {prev_trading_day()}")

    # ── P1: 数据层 ────────────────────────────────────────
    pipeline = DataPipeline()
    data = pipeline.fetch_all(stock_pool=stock_pool)

    prices       = data["prices"]
    snapshot     = data["snapshot"]
    fundamentals = data["fundamentals"]

    logger.info(
        f"数据就绪: {len(prices)} 只价格 / "
        f"VIX={snapshot.get('VIXCLS', 'N/A'):.2f} / "
        f"10Y={snapshot.get('DGS10',  'N/A'):.2f}% / "
        f"{len(fundamentals)} 只基本面"
    )

    # ── P2: 量化信号层 ────────────────────────────────────
    logger.info("── P2 量化信号层 ──")
    quant_signals = {}

    for bucket_name, bucket_tickers in buckets.items():
        logger.info(f"  [{bucket_name}]")
        for ticker in bucket_tickers:
            info   = fundamentals.get(ticker, {})
            result = compute_quant_signal(ticker, prices, bucket_tickers, info)
            quant_signals[ticker] = result
            logger.info(f"    {ticker:5s}: {result.reasoning}")

    # F1: 兜底——stock_pool 中不在任何 bucket 的股票，以全池为同行组计算
    for ticker in stock_pool:
        if ticker not in quant_signals:
            logger.warning(f"  [P2] {ticker} 不在任何 bucket，以全池为同行组计算")
            info   = fundamentals.get(ticker, {})
            result = compute_quant_signal(ticker, prices, stock_pool, info)
            quant_signals[ticker] = result
            logger.info(f"    {ticker:5s} [fallback]: {result.reasoning}")

    # ── P3: 宏观信号层 ───────────────────────────────────────
    logger.info("── P3 宏观信号层 ──")
    macro = compute_macro_signal(snapshot, prices, buckets)
    logger.info(
        f"  VIX={macro.vix_level:.1f} [{macro.vix_regime}] "
        f"仓位上限={macro.position_limit:.0%}  "
        f"yield_spread={macro.yield_spread:+.2f}%  "
        f"macro_score={macro.score:+.3f}"
    )
    logger.info(f"  桶 IR: " + "  ".join(
        f"{k}={macro.bucket_ir[k]:+.3f}(score={macro.bucket_scores[k]:+.2f})"
        for k in macro.bucket_ir
    ))

    # ── P4: 缠论信号层 ───────────────────────────────────────
    logger.info("── P4 缠论信号层 ──")
    chan_signals = {}
    for ticker in stock_pool:
        result = compute_chan_signal(ticker, prices)
        chan_signals[ticker] = result
        point = result.buy_point_type or result.sell_point_type or "neutral"
        logger.info(
            f"  {ticker:5s}: {point:8s} score={result.score:+.2f} "
            f"笔={result.stroke_count:2d} 中枢={'有' if result.current_pivot else '无'} "
            f"周线={result.weekly_trend:7s} res={result.level_resonance} "
            f"conf={result.confidence:.2f}"
        )

    # ── P5: 决策层 ───────────────────────────────────────
    logger.info("── P5 决策层 ──")
    decisions: dict[str, StockDecision] = {}
    for ticker in stock_pool:
        d = make_decision(
            ticker=ticker,
            chan=chan_signals[ticker],
            quant=quant_signals[ticker],
            macro=macro,
            prices=prices,
        )
        decisions[ticker] = d

    # ── 决策排行（按 final_score 降序）────────────────────
    logger.info("── P5 综合评级排行 ──")
    for d in sorted(decisions.values(), key=lambda x: x.final_score, reverse=True):
        bar   = _score_bar(d.final_score)
        flags = " | ".join(d.risk_flags) if d.risk_flags else "—"
        pivot = d.chan_signal.current_pivot
        entry = f"{d.entry_price_range[0]:.1f}~{d.entry_price_range[1]:.1f}"
        logger.info(
            f"  {d.ticker:5s} [{d.rating:11s}] {bar} {d.final_score:+.3f}  "
            f"pos={d.suggested_position:.0%}  "
            f"SL={d.stop_loss:.1f}  TP={d.take_profit:.1f}  "
            f"entry={entry}"
        )
        logger.info(f"         得分: {d.score_reasoning}")
        if d.risk_flags:
            logger.info(f"         风控: {flags}")

    # ── P6: 报告层 ───────────────────────────────────────
    logger.info("── P6 报告层 ──")
    written = write_all_reports(
        decisions=decisions,
        macro=macro,
        date_str=date_str,
        output_dir=output_dir,
    )
    for p in written:
        logger.info(f"  已写入: {p}")

    # ── 量化评分排行 ─────────────────────────────────────
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

    # ── P7: 回测层 ───────────────────────────────────────
    logger.info("── P7 回测层（缠论信号 walk-forward）──")
    bt_results = run_all_backtests(pipeline, stock_pool)
    bt_path    = write_backtest_report(bt_results, output_dir, date_str)
    for ticker, r in bt_results.items():
        logger.info(f"  {ticker:5s}: {r.reasoning}")
    logger.info(f"  回测报告: {bt_path}")

    logger.info(f"{'='*50}")
    logger.info("P1 + P2 + P3 + P4 + P5 + P6 + P7 运行完毕 ✓")
    logger.info(f"报告目录: {output_dir}")


def _score_bar(score: float, width: int = 10) -> str:
    filled = round((score + 1) / 2 * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


if __name__ == "__main__":
    run()
