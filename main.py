from pathlib import Path
from loguru import logger

import utils.logger  # 触发 setup_logger()
from config.settings import settings
from config.stocks import STOCK_POOL, BENCHMARKS
from data.pipeline import DataPipeline
from utils.time_utils import today_str, prev_trading_day


def run() -> None:
    date_str = today_str()
    output_dir = Path(settings.output_dir) / date_str
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"{'='*50}")
    logger.info(f"美股量化分析系统  {date_str}")
    logger.info(f"{'='*50}")
    logger.info(f"股票池 ({len(STOCK_POOL)} 只): {STOCK_POOL}")
    logger.info(f"基准: {BENCHMARKS}")
    logger.info(f"数据基准日 (t-1): {prev_trading_day()}")
    logger.info(f"报告输出目录: {output_dir}")

    # ── P1: 数据层 ────────────────────────────────────────
    pipeline = DataPipeline()
    data = pipeline.fetch_all()
    prices   = data["prices"]
    snapshot = data["snapshot"]
    logger.info(f"数据就绪: {len(prices)} 只价格 / VIX={snapshot.get('VIXCLS', 'N/A'):.2f} / 10Y={snapshot.get('DGS10', 'N/A'):.2f}%")

    # ── P2/P3/P4: 信号层（待实现）────────────────────────
    logger.warning("[P2] 技术信号层待实现")
    logger.warning("[P3] 宏观信号层待实现")
    logger.warning("[P4] 缠论信号层待实现（等待精髓输入）")

    # ── P5: 决策层（待实现）──────────────────────────────
    logger.warning("[P5] 决策层待实现")

    # ── P6: 报告层（待实现）──────────────────────────────
    logger.warning("[P6] 报告层待实现")

    logger.info(f"{'='*50}")
    logger.info("P1 数据层运行完毕 ✓")


if __name__ == "__main__":
    run()
