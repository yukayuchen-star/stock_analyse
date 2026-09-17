"""
ML 历史回测入口

用法：
  python run_ml_backtest.py                   # 默认宇宙 ~30 只，2022-01 起
  python run_ml_backtest.py --pool-only       # 仅用当前 core pool（9 只，速度快）
  python run_ml_backtest.py --start 2021-01-01  # 自定义起始

R10 前瞻性特征（默认**关闭**，关闭时产出与接入该特征之前逐位一致）：
  python run_ml_backtest.py --timesfm                 # 加 TimesFM 特征
  python run_ml_backtest.py --timesfm --dlinear       # 再加 DLinear 对照组
  python run_ml_backtest.py --timesfm --r10-context 512 --start 2023-01-01

  ⚠️ `--dlinear` 是**对照组不是候选因子**：只用来判断 TimesFM 的预训练在漂移通道上
     是否带来增量。它需要训练期，起步比零样本的 TimesFM 晚一个 `context+horizon`，
     两侧可用日期天然不等长 —— `compute_dlinear_features` 会把缺口报出来，
     **比较时务必先取日期交集**。
  ⚠️ `--r10-context` 越大要求的预热越长。`build_dataset` 默认从回测起点往前 14 个月
     下载，`context=512` 需要约 2 年以上，否则会出现"一天都不合格"并告警。
  ⚠️ 这两个特征在**子进程**里算（torch 与 lightgbm 在本机不能同进程，见
     `backtest/r10_precompute.py`），所以会看到一段 `[R10-子进程]` 的日志。

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
    parser.add_argument("--timesfm", action="store_true",
                        help="启用 TimesFM 前瞻性特征（R10），在子进程中计算")
    parser.add_argument("--dlinear", action="store_true",
                        help="启用 DLinear 对照组（R10 §2.2），判断预训练是否有增量")
    parser.add_argument("--r10-context", type=int, default=None,
                        help="R10 上下文长度（默认 256；调大需相应提前下载起点）")
    parser.add_argument("--r10-horizon", type=int, default=None,
                        help="R10 预测步长（默认 5，与 fwd_ret_5d 标签对齐）")
    args = parser.parse_args()

    # 传 dict 而非配置对象：构造 DLinearConfig 需要 import backtest.dlinear_control，
    # 那会把 torch 拉进**本进程**，而本进程稍后要训 LightGBM —— 两者同进程必段错误。
    r10: dict = {}
    for flag, key in ((args.timesfm, "timesfm_config"), (args.dlinear, "dlinear_config")):
        if not flag:
            continue
        cfg: dict = {}
        if args.r10_context is not None:
            cfg["context"] = args.r10_context
        if args.r10_horizon is not None:
            cfg["horizon"] = args.r10_horizon
        r10[key] = cfg
    if args.dlinear and not args.timesfm:
        logger.warning("[R10] 只开了对照组没开 TimesFM —— 对照组单独跑不构成任何结论")

    tickers = list(STOCK_POOL) if args.pool_only else DEFAULT_UNIVERSE
    # 数据下载从比回测起始早 14 个月预热：需覆盖最长特征窗口
    # （SMA200=200TD、vix_pct252=252TD ≈ 12 个日历月）。早 7 个月不足，
    # 会让回测首批样本的长窗口特征为 NaN→fillna(0)，污染前几折。
    import pandas as pd
    warmup_start = (pd.Timestamp(args.start) - pd.DateOffset(months=14)).strftime("%Y-%m-%d")
    date_str     = today_str()
    output_dir   = Path("output") / "ml_backtest"

    logger.info("=" * 55)
    logger.info(f"ML 历史回测   起始: {args.start}   股票: {len(tickers)} 只")
    logger.info(f"R10 前瞻性特征: {', '.join(r10) if r10 else '未启用（产出与接入前逐位一致）'}")
    logger.info("=" * 55)

    # ── 1. 构建数据集 ────────────────────────────────────────
    logger.info("步骤 1/3: 特征构建")
    try:
        dataset = build_dataset(
            tickers=tickers,
            start=warmup_start,
            backtest_start=args.start,
            **r10,
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
