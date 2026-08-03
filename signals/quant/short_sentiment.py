"""R6.3 做空情绪因子 Short-Sentiment Factor（候选，待 R6.1 门验证）。

基于 FINRA 日度 Short Volume Ratio (SVR = ShortVolume/TotalVolume) 的做空流量因子。
经济假设：SVR 高 / 上升 = 空头压力（看空）→ 前向负收益？或过度做空 = 反转燃料 → 正收益？
**方向未知，交由 R6.1 门（IC/分位/相关性）裁决**；诚实先验：大盘流动性极佳、做空流量占比小、
套利充分，IC 可能近零——过门则并入，不过则如实记录不 merge（同 R5 pullback / R6.2 结构因子）。

范围：仅 FINRA（SEC FTD 本环境不可达 → 逼空风险因子本期全缓）。数据经 `data/short_data_source`
程序化自取（非爬虫）。**不改任何现有文件**，隔离验证；过门才建 DataSource 接线 + 接入 factor_engine。

用法：`python -m signals.quant.short_sentiment`（首跑下载 FINRA 历史日文件，之后缓存复用）。
"""
from __future__ import annotations

from typing import Callable, Dict, List

import numpy as np
import pandas as pd

SVR_START = "2024-01-01"      # 验证窗口起（2024/2025/2026 三段供跨年稳定性）
_ROLL = 20
_MIN_P = 10

# 做空情绪特征（基于单票 SVR 日序列，全部 as-of 滚动，仅用 ≤t 数据）
FEATURES: List[str] = [
    "svr_level",    # 当日做空流量占比
    "svr_mean20",   # 20 日均 SVR（持续做空压力）
    "svr_z20",      # SVR 20 日 z-score（异常做空）
    "svr_chg5",     # SVR 5 日变化（做空情绪动量）
]


def _svr_features(svr: pd.Series) -> pd.DataFrame:
    """从单票 SVR 日序列派生做空情绪特征（as-of）。"""
    mean20 = svr.rolling(_ROLL, min_periods=_MIN_P).mean()
    std20 = svr.rolling(_ROLL, min_periods=_MIN_P).std(ddof=0)
    return pd.DataFrame({
        "svr_level": svr,
        "svr_mean20": mean20,
        "svr_z20": (svr - mean20) / std20.replace(0, np.nan),
        "svr_chg5": svr - svr.shift(5),
    }, index=svr.index)


def _build_short_panel(universe, start: str, end: str) -> pd.DataFrame:
    from backtest.factor_lab import _load_universe_prices, forward_returns, FWD, MIN_ROWS
    from config.settings import settings
    from data.cache import SQLiteCache
    from data.short_data_source import FinraShortSource

    prices = _load_universe_prices(universe)
    if not prices:
        return pd.DataFrame()
    src = FinraShortSource(SQLiteCache(settings.cache_dir))
    wide = src.get_short_history(list(prices), start, end)   # [date × ticker] SVR
    if wide.empty:
        return pd.DataFrame()

    frames = []
    for tk, df in prices.items():
        if tk not in wide.columns or len(df) < MIN_ROWS:
            continue
        close = df["Close"].astype(float)
        svr = pd.to_numeric(wide[tk], errors="coerce").reindex(df.index)  # 对齐价格交易日
        if svr.notna().sum() < 60:      # SVR 覆盖太少的票跳过
            continue
        feats = _svr_features(svr)
        for k, v in forward_returns(close, FWD).items():
            feats[k] = v
        feats = feats.dropna(subset=[f"fwd{max(FWD)}"]).dropna(subset=FEATURES, how="all")
        if feats.empty:
            continue
        feats["ticker"] = tk
        feats["date"] = feats.index
        frames.append(feats.reset_index(drop=True))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["date", "ticker"]).reset_index(drop=True)


def main() -> None:
    from backtest.factor_lab import (
        VALIDATION_UNIVERSE, _report_factor, evaluate_factor,
        factor_correlations, MIN_NAMES_PER_DATE, CORR_MAX,
    )
    from utils.time_utils import today_str

    panel = _build_short_panel(VALIDATION_UNIVERSE, SVR_START, today_str())
    if panel.empty or panel["ticker"].nunique() < MIN_NAMES_PER_DATE:
        n = 0 if panel.empty else panel["ticker"].nunique()
        print(f"做空面板不足（tickers={n}<{MIN_NAMES_PER_DATE}）——检查 FINRA 取数 / 窗口")
        return

    span = f"{panel['date'].min():%Y-%m-%d}→{panel['date'].max():%Y-%m-%d}"
    print(f"\n做空面板: tickers={panel['ticker'].nunique()} rows={len(panel)} "
          f"dates={panel['date'].nunique()} ({span})")

    print("\n" + "=" * 72)
    print("R6.3 做空情绪因子（FINRA SVR）— R6.1 门验证（IC/IR + 分位 + 跨年 + 相关性）")
    print("（⚠️ 大盘做空流量占比小,IC 可能近零;看符号/单调方向,非小数）")
    print("=" * 72)
    verdicts = {}
    for col in FEATURES:
        _report_factor(panel, col, [c for c in FEATURES if c != col])
        verdicts[col] = evaluate_factor(panel, col, [c for c in FEATURES if c != col])["verdict"]

    print("\n因子两两 |corr|（>%.2f 视为冗余，剪枝）:" % CORR_MAX)
    print(factor_correlations(panel, FEATURES).round(2).to_string())

    passed = [c for c, v in verdicts.items() if v == "PASS"]
    print(f"\nVERDICT: 过门 {passed if passed else '无'} / {len(FEATURES)}。"
          f"{'→ 建 DataSource 接线 + 并入 factor_engine' if passed else '→ 如实记录不 merge（同 R5 pullback / R6.2）'}")


if __name__ == "__main__":
    main()
