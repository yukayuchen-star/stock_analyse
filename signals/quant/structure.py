"""R6.2 缠论原生结构因子 Chan-Native Structure Factor（候选，待 R6.1 门验证）。

动机：缠论引擎内部算出的**结构数值**（背驰力度、中枢几何、价格位置）目前被塌成布尔门
或只进 `reasoning` 日志、在构造 `ChanSignalResult` 前丢弃。本模块把它们 **harvest+expose**
成可量化的横截面因子候选，经 `backtest/factor_lab` 门（IC/分位/相关性）验证——**过门才**
暴露到 `ChanSignalResult` + 并入 `factor_engine`（R6.5），不过则如实记录不 merge（R5 pullback 纪律）。

兼容性：**不改缠论 55% 本体**（`chan_signal.py` 零改动）——复用其公开结构构建器
（process_bars/build_strokes/find_latest_pivot）与两个 MACD 工具（`_macd_hist`/`_stroke_area`）。

无前视：结构特征按 `df[:t+1]` 逐日重放（`extract_chan_events` 同款 as-of），仅用 ≤t 数据。
同一 `_structure_features` 既供逐日面板、又供最新一根（live），保证面板==实盘末行不漂移。

用法：`python -m signals.quant.structure`（先跑 `python main.py` 预热价格 cache）。
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from signals.chan.fractal import process_bars, detect_fractals
from signals.chan.stroke  import build_strokes, Stroke
from signals.chan.pivot   import find_latest_pivot, Pivot
from signals.chan.chan_signal import _macd_hist, _stroke_area

# 结构因子候选列名（chan_ 前缀避免与既有五因子命名冲突）
FEATURES: List[str] = [
    "chan_area_ratio",   # 背驰力度：末笔 vs 前一同向笔 MACD 面积比（原 <0.8 布尔门的连续量）
    "chan_pivot_width",  # 中枢震荡幅度 (ZG-ZD)/mid
    "chan_pivot_age",    # 中枢新鲜度：距中枢末笔的交易日数
    "chan_price_pos",    # 价格带内位置 (price-ZD)/(ZG-ZD)：<0 带下 / 0~1 带内 / >1 带上
    "chan_dist_swhi",    # 到近端摆动高点的上行空间 (swing_high-price)/price
    "chan_dist_swlo",    # 距近端摆动低点的距离 (price-swing_low)/price
]

_START_BAR = 60          # 与 extract_chan_events 一致：结构成熟起点
_SWING_STROKES = 6       # 近端摆动高低点取最近 N 笔


def _structure_features(
    strokes: List[Stroke],
    pivot: Optional[Pivot],
    df: pd.DataFrame,
    close: pd.Series,
    hist: pd.Series,
) -> Dict[str, float]:
    """从**已构建**的 strokes/pivot 读出结构特征（不重建结构，不改信号逻辑）。缺项 NaN。"""
    feat: Dict[str, float] = {k: float("nan") for k in FEATURES}
    if not strokes:
        return feat
    price = float(close.iloc[-1])
    last = strokes[-1]

    # 背驰力度：末笔 vs 前一同向笔的 MACD 面积比（_detect_buy/_sell 内塌成 <0.8 布尔的连续量）
    same_dir = [s for s in strokes if s.direction == last.direction]
    if len(same_dir) >= 2:
        curr = _stroke_area(last, df, hist)
        prev = _stroke_area(same_dir[-2], df, hist)
        if prev > 1e-6:
            feat["chan_area_ratio"] = float(np.clip(curr / prev, 0.0, 5.0))

    # 近端摆动高低点（取最近 N 笔的极值，缠论 swing 而非原始 rolling）
    recent = strokes[-_SWING_STROKES:]
    if price > 0 and recent:
        sw_hi = max(s.high for s in recent)
        sw_lo = min(s.low for s in recent)
        feat["chan_dist_swhi"] = (sw_hi - price) / price
        feat["chan_dist_swlo"] = (price - sw_lo) / price

    # 中枢几何
    if pivot is not None and pivot.is_valid:
        width = pivot.zg - pivot.zd
        if pivot.mid > 0:
            feat["chan_pivot_width"] = width / pivot.mid
        if width > 0:
            feat["chan_price_pos"] = (price - pivot.zd) / width
        if pivot.end_date is not None:
            feat["chan_pivot_age"] = float((df.index > pivot.end_date).sum())
    return feat


def latest_structure_features(df: pd.DataFrame) -> Dict[str, float]:
    """最新一根（live）结构特征——与逐日面板末行同口径（供未来暴露到 ChanSignalResult）。"""
    if df is None or "Close" not in df or len(df) < _START_BAR:
        return {k: float("nan") for k in FEATURES}
    try:
        strokes = build_strokes(detect_fractals(process_bars(df)))
    except Exception:
        return {k: float("nan") for k in FEATURES}
    if len(strokes) < 3:
        return {k: float("nan") for k in FEATURES}
    pivot = find_latest_pivot(strokes, lookback=12)
    return _structure_features(strokes, pivot, df, df["Close"], _macd_hist(df["Close"]))


def structure_features_asof(df: pd.DataFrame, start: int = _START_BAR) -> pd.DataFrame:
    """逐日 as-of 重放：每根仅用 `df[:t+1]` 重建结构 → 结构特征（无前视，同 extract_chan_events）。"""
    out: Dict[pd.Timestamp, Dict[str, float]] = {}
    for t in range(start, len(df)):
        sub = df.iloc[: t + 1]
        try:
            strokes = build_strokes(detect_fractals(process_bars(sub)))
        except Exception:
            continue
        if len(strokes) < 3:
            continue
        pivot = find_latest_pivot(strokes, lookback=12)
        out[df.index[t]] = _structure_features(
            strokes, pivot, sub, sub["Close"], _macd_hist(sub["Close"]))
    if not out:
        return pd.DataFrame(columns=FEATURES)
    return pd.DataFrame.from_dict(out, orient="index")[FEATURES]


# ── R6.1 门验证（python -m signals.quant.structure）──────────────
def _build_structure_panel(prices: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    from backtest.factor_lab import forward_returns, FWD, MIN_ROWS
    frames = []
    for tk, df in prices.items():
        if df is None or "Close" not in df or len(df) < MIN_ROWS:
            continue
        feats = structure_features_asof(df)
        if feats.empty:
            continue
        close = df["Close"].astype(float)
        for k, v in forward_returns(close, FWD).items():
            feats[k] = v.reindex(feats.index)
        feats = feats.dropna(subset=[f"fwd{max(FWD)}"])
        if feats.empty:
            continue
        feats["ticker"] = tk
        feats["date"] = feats.index
        frames.append(feats.reset_index(drop=True))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["date", "ticker"]).reset_index(drop=True)


def _asof_spotcheck(prices: Dict[str, pd.DataFrame], n: int = 6, seed: int = 3) -> None:
    """抽样断言 structure_features_asof(df).loc[date_t] == asof(df[:t+1]).iloc[-1]（截断不变=无前视）。"""
    items = [(tk, df) for tk, df in prices.items() if df is not None and len(df) >= 400]
    rng = np.random.default_rng(seed)
    mism = 0
    for _ in range(n):
        tk, df = items[rng.integers(len(items))]
        t = int(rng.integers(300, len(df) - 21))
        full = structure_features_asof(df)
        trunc = structure_features_asof(df.iloc[: t + 1])
        dt = df.index[t]
        if dt not in full.index or trunc.empty:
            continue
        a, b = full.loc[dt], trunc.iloc[-1]
        for c in FEATURES:
            va, vb = a[c], b[c]
            if np.isfinite(va) and np.isfinite(vb) and abs(va - vb) > 1e-9:
                mism += 1
    assert mism == 0, f"结构特征 as-of 漂移 {mism} 处（用了未来数据）"
    print(f"[asof] structure_features 全序列≡截断 一致（{n} 抽样）")


def main() -> None:
    from backtest.factor_lab import (
        VALIDATION_UNIVERSE, _load_universe_prices, _report_factor,
        factor_correlations, evaluate_factor, MIN_NAMES_PER_DATE, CORR_MAX,
    )
    prices = _load_universe_prices(VALIDATION_UNIVERSE)
    if len(prices) < MIN_NAMES_PER_DATE:
        print(f"验证宇宙价格不足（{len(prices)}<{MIN_NAMES_PER_DATE}）——先跑 `python main.py` 预热 cache")
        return

    _asof_spotcheck(prices)
    panel = _build_structure_panel(prices)
    if panel.empty:
        print("结构面板为空——检查数据窗口")
        return
    span = f"{panel['date'].min():%Y-%m-%d}→{panel['date'].max():%Y-%m-%d}"
    print(f"\n结构面板: tickers={panel['ticker'].nunique()} rows={len(panel)} "
          f"dates={panel['date'].nunique()} ({span})")

    print("\n" + "=" * 72)
    print("R6.2 缠论结构因子 — R6.1 门验证（IC/IR + 分位 + 跨年 + 相关性）")
    print("（⚠️ 横截面 CI 随宇宙宽度收窄；看符号/单调方向，非小数）")
    print("=" * 72)
    verdicts = {}
    for col in FEATURES:
        _report_factor(panel, col, [c for c in FEATURES if c != col])
        verdicts[col] = evaluate_factor(panel, col, [c for c in FEATURES if c != col])["verdict"]

    print("\n因子两两 |corr|（>%.2f 视为冗余，剪枝）:" % CORR_MAX)
    print(factor_correlations(panel, FEATURES).round(2).to_string())

    passed = [c for c, v in verdicts.items() if v == "PASS"]
    print(f"\nVERDICT: 过门 {passed if passed else '无'} / 6。"
          f"{'→ 暴露 ChanSignalResult + 并入 factor_engine(R6.5)' if passed else '→ 如实记录不 merge（同 R5 pullback）'}")


if __name__ == "__main__":
    main()
