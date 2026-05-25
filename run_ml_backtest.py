"""
ML 历史回测入口

用法：
  python run_ml_backtest.py                   # 默认宇宙 ~30 只，2022-01 起
  python run_ml_backtest.py --pool-only       # 仅用当前 core pool（9 只，速度快）
  python run_ml_backtest.py --start 2021-01-01  # 自定义起始

报告输出：output/ml_backtest/ml_backtest_report.md
"""
import argparse
import sys
from pathlib import Path

from loguru import logger
import utils.logger  # 初始化日志格式

from backtest.ml_backtest import (
    DEFAULT_UNIVERSE, build_dataset, run_walk_forward, write_ml_report,
)
from config.stocks import STOCK_POOL
from utils.time_utils import today_str


def main() -> None:
    parser = argparse.ArgumentParser(description="LightGBM 历史回测")
    parser.add_argument("--pool-only", action="store_true",
                        help="仅用 config/stocks.py 的 core pool（速度快）")
    parser.add_argument("--start", default="2022-01-01",
                        help="回测起始日（默认 2022-01-01）")
    args = parser.parse_args()

    tickers = list(STOCK_POOL) if args.pool_only else DEFAULT_UNIVERSE
    # 数据下载从比回测起始早 6 个月（SMA200 预热）
    import pandas as pd
    warmup_start = (pd.Timestamp(args.start) - pd.DateOffset(months=7)).strftime("%Y-%m-%d")
    date_str     = today_str()
    output_dir   = Path("output") / "ml_backtest"

    logger.info("=" * 55)
    logger.info(f"ML 历史回测   起始: {args.start}   股票: {len(tickers)} 只")
    logger.info("=" * 55)

    # ── 1. 构建数据集 ────────────────────────────────────────
    logger.info("步骤 1/3: 特征构建")
    try:
        dataset = build_dataset(
            tickers=tickers,
            start=warmup_start,
            backtest_start=args.start,
        )
    except Exception as exc:
        logger.error(f"数据集构建失败: {exc}")
        sys.exit(1)

    logger.info(
        f"  数据集: {len(dataset.df):,} 行 × {len(dataset.feature_cols)} 特征  "
        f"({dataset.df['date'].min()} ~ {dataset.df['date'].max()})"
    )

    # ── 2. 走步前向训练 ──────────────────────────────────────
    logger.info("步骤 2/3: LightGBM 走步前向验证 ...")
    result = run_walk_forward(dataset, fold_months=6)

    if not result.folds:
        logger.error("没有生成任何有效折，请检查数据量")
        sys.exit(1)

    logger.info(f"  折数: {len(result.folds)}")
    logger.info(f"  综合 AUC:  {result.overall_auc:.3f}")
    logger.info(f"  ML 胜率:   {result.overall_precision:.1%}")
    logger.info(f"  ML 均收益: {result.overall_avg_ret:+.2%}")
    logger.info(f"  随机基准:  {result.baseline_win_rate:.1%}")

    # ── 3. 写报告 ────────────────────────────────────────────
    logger.info("步骤 3/3: 生成报告 ...")
    path = write_ml_report(result, dataset, output_dir, date_str)
    logger.info(f"  报告路径: {path}")
    logger.info("=" * 55)
    logger.info("ML 历史回测完成 ✓")


if __name__ == "__main__":
    main()
