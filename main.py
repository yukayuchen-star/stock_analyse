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
from utils.time_utils import today_str, prev_trading_day


def run() -> None:
    date_str   = today_str()
    output_dir = Path(settings.output_dir) / date_str
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"{'='*50}")
    logger.info(f"美股量化分析系统  {date_str}")
    logger.info(f"{'='*50}")
    logger.info(f"股票池 ({len(STOCK_POOL)} 只): {STOCK_POOL}")
    logger.info(f"基准: {BENCHMARKS}")
    logger.info(f"数据基准日 (t-1): {prev_trading_day()}")

    # ── P1: 数据层 ────────────────────────────────────────
    pipeline = DataPipeline()
    data = pipeline.fetch_all()

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

    for bucket_name, bucket_tickers in BUCKETS.items():
        logger.info(f"  [{bucket_name}]")
        for ticker in bucket_tickers:
            info   = fundamentals.get(ticker, {})
            result = compute_quant_signal(ticker, prices, bucket_tickers, info)
            quant_signals[ticker] = result
            logger.info(f"    {ticker:5s}: {result.reasoning}")

    # ── P3: 宏观信号层 ───────────────────────────────────────
    logger.info("── P3 宏观信号层 ──")
    macro = compute_macro_signal(snapshot, prices, BUCKETS)
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
    for ticker in STOCK_POOL:
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
    for ticker in STOCK_POOL:
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

    logger.info(f"{'='*50}")
    logger.info("P1 + P2 + P3 + P4 + P5 + P6 运行完毕 ✓")
    logger.info(f"报告目录: {output_dir}")


def _score_bar(score: float, width: int = 10) -> str:
    filled = round((score + 1) / 2 * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


if __name__ == "__main__":
    run()
