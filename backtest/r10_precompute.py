"""R10 特征的**子进程**计算器（2026-09-16 加）。

════════════════════════════════════════════════════════════════════════
为什么必须是独立进程
════════════════════════════════════════════════════════════════════════
本机 `.venv` 里存在两份 OpenMP 运行时：`torch/lib/libomp.dylib` 与
`sklearn/.dylibs/libomp.dylib`。实测（2026-09-16，torch 2.14 + lightgbm 4.6 +
Python 3.12 / macOS）**两个方向都会段错误（exit 139，无异常无栈）**：

    torch 先导入      → torch 前向正常，之后 lightgbm 的 fit() **段错误**
    lightgbm 先导入   → lightgbm 正常，之后 torch 的前向 **段错误**

`KMP_DUPLICATE_LIB_OK=TRUE`、`OMP_NUM_THREADS=1`、两者同时设 —— **全部无效**。
即：**没有任何导入顺序或环境变量能让二者在同一进程里共存**。

而 `build_dataset` 恰好要求两件事同时发生：先用 torch 算 TimesFM/DLinear 特征，
再用 LightGBM 走步前向训练。所以这不是"调个导入顺序"能解决的问题，是进程模型问题。

⇒ 本模块在**子进程**里跑（只导 torch，绝不导 lightgbm），把特征 pickle 回父进程；
父进程（`ml_backtest`）**永不导入 torch**。这也是 `ml_backtest.py` 顶部那段
`try: import torch` 被删除的原因 —— 它保护了 torch 前向，代价是打死了
`run_ml_backtest.py` 自己。

用法（一般由 `ml_backtest._compute_r10_features` 自动调起，不必手工执行）：

    python -m backtest.r10_precompute <输入.pkl> <输出.pkl>
"""

from __future__ import annotations

import pickle
import sys

from loguru import logger


def main(in_path: str, out_path: str) -> int:
    with open(in_path, "rb") as fh:
        spec = pickle.load(fh)

    close_panel = spec["close_panel"]
    cov_panel = spec.get("cov_panel")
    start = spec.get("start")
    out: dict = {"tfm": None, "dlin": None, "tfm_columns": [], "dlin_columns": []}

    if spec.get("timesfm") is not None:
        from backtest.timesfm_features import (
            FEATURE_COLUMNS as TFM_COLUMNS,
            TimesFMFeatureConfig,
            compute_timesfm_features,
        )

        cfg = TimesFMFeatureConfig(**spec["timesfm"])
        logger.info(f"[R10] 子进程：计算 TimesFM 特征 {cfg}")
        out["tfm"] = compute_timesfm_features(close_panel, cov_panel, cfg, start=start)
        out["tfm_columns"] = list(TFM_COLUMNS)

    if spec.get("dlinear") is not None:
        from backtest.dlinear_control import (
            FEATURE_COLUMNS as DLIN_COLUMNS,
            DLinearConfig,
            compute_dlinear_features,
        )

        cfg = DLinearConfig(**spec["dlinear"])
        logger.info(f"[R10] 子进程：计算 DLinear 对照特征 {cfg}")
        out["dlin"] = compute_dlinear_features(close_panel, cfg, start=start)
        out["dlin_columns"] = list(DLIN_COLUMNS)

    with open(out_path, "wb") as fh:
        pickle.dump(out, fh)
    logger.info(
        f"[R10] 子进程完成：tfm={None if out['tfm'] is None else len(out['tfm'])} 行，"
        f"dlin={None if out['dlin'] is None else len(out['dlin'])} 行"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
