from pathlib import Path
from loguru import logger

import utils.logger  # 触发 setup_logger()
from config.settings import settings
from config.stocks import STOCK_POOL, BENCHMARKS, BUCKETS
from data.pipeline import DataPipeline
from signals.quant.factor_engine import compute_quant_signal
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

    # ── P3: 宏观信号层（待实现）──────────────────────────
    logger.warning("[P3] 宏观信号层待实现")

    # ── P4: 缠论信号层（等待精髓输入）───────────────────
    logger.warning("[P4] 缠论信号层待实现（等待精髓输入）")

    # ── P5: 决策层（待实现）──────────────────────────────
    logger.warning("[P5] 决策层待实现")

    # ── P6: 报告层（待实现）──────────────────────────────
    logger.warning("[P6] 报告层待实现")

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
    logger.info("P1 + P2 量化信号层运行完毕 ✓")


def _score_bar(score: float, width: int = 10) -> str:
    filled = round((score + 1) / 2 * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


if __name__ == "__main__":
    run()
