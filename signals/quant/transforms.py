"""R6.4 候选因子变换工具箱 Candidate Transforms（服务加工层，非独立因子）。

用户想法 3：滚动均值 / 同比环比 / 横截面排序 / 去极值 / 标准化，是把**原始序列**
加工成 gate-ready 因子的纯函数层。全部**无副作用、可组合**，并严守无前视：

- **时序变换**（`roll_*`/`pct_change_n`/`diff_n`/`yoy`/`roll_rank_pct`）：仅尾窗（≤t），
  截断不变 → 直接可做 `FactorFn`（`backtest.factor_lab.assert_asof_consistent` 可证）。
- **横截面变换**（`cs_rank`/`winsorize`/`mad_winsorize`/`cs_zscore`）：**只在单个日期跨票**施用
  （同 `relative.py` 的 `rank(pct=True)`），同日数据 → 无时间前视；**切勿沿整条时间序列施用**
  （会用到未来分布 = 前视）。RankIC/qcut 本就 rank-based，故喂进 factor_lab 的因子只需时序变换。

⚠️ **诚实边界**（R4.4）：变换仅施于**有真实历史**的序列（价 / 量 / 做空流量）；
**基本面同比 / 环比排除**——yfinance 快照无 PIT 历史、不可回测。

`DERIVED_CANDIDATES` 用本工具箱从价 / 量组合出一批派生候选，`main()` 过 R6.1 门
（IC/IR + 分位 + 跨年 + 相关性）实测——**过门才入 R6.5，不过如实记录**（同 R5/R6.2/R6.3）。
用法：`python -m signals.quant.transforms`（先 `python main.py` 预热价格 cache）。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ── 时序变换（尾窗，无前视：仅 ≤t）────────────────────────────────
def roll_mean(s: pd.Series, window: int, min_periods: Optional[int] = None) -> pd.Series:
    """滚动均值（尾窗）。"""
    return s.rolling(window, min_periods=min_periods).mean()


def roll_std(s: pd.Series, window: int, min_periods: Optional[int] = None,
             ddof: int = 0) -> pd.Series:
    """滚动标准差（尾窗；ddof=0 与 z-score 口径一致）。"""
    return s.rolling(window, min_periods=min_periods).std(ddof=ddof)


def roll_z(s: pd.Series, window: int, min_periods: Optional[int] = None) -> pd.Series:
    """滚动 z-score `(s - roll_mean) / roll_std`（尾窗标准化，无前视）。std=0 → NaN。"""
    m = roll_mean(s, window, min_periods)
    sd = roll_std(s, window, min_periods)
    return (s - m) / sd.replace(0.0, np.nan)


def pct_change_n(s: pd.Series, n: int) -> pd.Series:
    """n 期环比比率变化 `s/s.shift(n) - 1`（同比传 periods≈252）。适合水平量（价/量）。"""
    return s / s.shift(n) - 1.0


def diff_n(s: pd.Series, n: int) -> pd.Series:
    """n 期绝对差 `s - s.shift(n)`（比率类序列如 SVR 用加法差比 pct 更稳）。"""
    return s - s.shift(n)


def yoy(s: pd.Series, periods: int = 252) -> pd.Series:
    """同比 `s/s.shift(252) - 1`（默认 252 交易日≈1 年）。仅可施于有真实历史序列。"""
    return pct_change_n(s, periods)


def roll_rank_pct(s: pd.Series, window: int) -> pd.Series:
    """当前值在过去 window 内的百分位 ∈[0,1]（尾窗，无前视）。首根=1/window，末根=1.0。"""
    return s.rolling(window).apply(
        lambda w: float(pd.Series(w).rank(pct=True).iloc[-1]), raw=False)


# ── 横截面变换（单日跨票；无时间前视——同日数据）──────────────────
def cs_rank(s: pd.Series) -> pd.Series:
    """横截面百分位 `rank(pct=True)` ∈(0,1]（同 relative.py bucket_pct；抗异常值）。"""
    return s.rank(pct=True)


def winsorize(s: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """分位去极值：裁剪到 [q_lower, q_upper]（横截面施用；NaN 保留）。"""
    lo, hi = s.quantile(lower), s.quantile(upper)
    return s.clip(lower=lo, upper=hi)


def mad_winsorize(s: pd.Series, k: float = 3.0) -> pd.Series:
    """稳健去极值：裁剪到 median ± k·(1.4826·MAD)（横截面；比分位法对薄尾更稳）。"""
    med = s.median()
    mad = (s - med).abs().median()
    if not np.isfinite(mad) or mad == 0:
        return s
    scale = 1.4826 * mad
    return s.clip(lower=med - k * scale, upper=med + k * scale)


def cs_zscore(s: pd.Series) -> pd.Series:
    """横截面标准化 `(s - mean)/std`（同日跨票；std=0 → 全 0）。"""
    sd = s.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return s * 0.0
    return (s - s.mean()) / sd


# ── 派生候选（用工具箱从价 / 量组合；仅价量，无基本面）─────────────
def _dollar_vol(df: pd.DataFrame) -> pd.Series:
    return df["Close"].astype(float) * df.get("Volume", pd.Series(index=df.index, dtype=float))


DERIVED_CANDIDATES = {
    # 标准化动量：20 日动量再经 60 日尾窗 z（去个股波动尺度，横截面更可比）
    "mom_z_20_60":  lambda df: roll_z(pct_change_n(df["Close"].astype(float), 20), 60),
    # 动量尾窗百分位：60 日动量在自身 120 日分布中的位置（自适应制度，抗尺度）
    "mom_rank_120": lambda df: roll_rank_pct(pct_change_n(df["Close"].astype(float), 60), 120),
    # 放量 z：成交量 20 日尾窗 z（量能异动，与价动量正交候选）
    "vol_z_20":     lambda df: roll_z(df.get("Volume", pd.Series(dtype=float)).astype(float), 20),
    # 振幅制度 z：(High-Low)/Close 的 20 日尾窗 z（波动扩张=风险，预期负 IC）
    "hl_range_z":   lambda df: roll_z(
        (df["High"].astype(float) - df["Low"].astype(float)) / df["Close"].astype(float), 20),
    # Amihud 非流动性：|ret|/美元成交额 的 20 日均（越高越不流动，经典正 IC 溢价）
    "amihud_20":    lambda df: (df["Close"].astype(float).pct_change().abs()
                                / _dollar_vol(df).replace(0.0, np.nan)).rolling(20).mean(),
    # 成交额环比动量：美元成交额 20 日环比（资金流入放量的价量确认候选）
    "turnover_mom": lambda df: pct_change_n(_dollar_vol(df), 20),
}


# ── 自检：数值正确 + 无前视（截断不变）──────────────────────────
def _selfcheck() -> None:
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2020-01-01", periods=400)
    s = pd.Series(np.cumsum(rng.normal(size=400)) + 100.0, index=idx)

    # 数值正确性
    assert np.isclose(roll_mean(s, 5).iloc[-1], s.iloc[-5:].mean()), "roll_mean 错"
    assert np.isclose(roll_std(s, 5, ddof=0).iloc[-1], s.iloc[-5:].std(ddof=0)), "roll_std 错"
    z = roll_z(s, 20).iloc[-1]
    assert np.isclose(z, (s.iloc[-1] - s.iloc[-20:].mean()) / s.iloc[-20:].std(ddof=0)), "roll_z 错"
    assert np.isclose(pct_change_n(s, 10).iloc[-1], s.iloc[-1] / s.iloc[-11] - 1.0), "pct_change_n 错"
    assert np.isclose(diff_n(s, 10).iloc[-1], s.iloc[-1] - s.iloc[-11]), "diff_n 错"
    assert np.isclose(roll_rank_pct(s, 50).iloc[-1],
                      pd.Series(s.iloc[-50:].values).rank(pct=True).iloc[-1]), "roll_rank_pct 错"

    # 横截面：去极值 / 标准化 / 排序
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 1000.0])
    assert winsorize(x, 0.0, 0.75).max() <= x.quantile(0.75) + 1e-9, "winsorize 未裁上尾"
    assert mad_winsorize(x).iloc[-1] < 1000.0, "mad_winsorize 未压极值"
    assert np.isclose(cs_zscore(x).mean(), 0.0, atol=1e-9), "cs_zscore 非零均值"
    assert cs_rank(x).iloc[-1] == 1.0, "cs_rank 顶值非 1"

    # 无前视：时序变换截断不变 fn(s)[t] == fn(s[:t+1])[-1]
    ts_fns = {
        "roll_mean": lambda a: roll_mean(a, 20),
        "roll_z": lambda a: roll_z(a, 20),
        "pct_change_n": lambda a: pct_change_n(a, 10),
        "yoy": lambda a: yoy(a, 60),
        "roll_rank_pct": lambda a: roll_rank_pct(a, 30),
    }
    for t in (120, 250, 380):
        for name, fn in ts_fns.items():
            a, b = fn(s).iloc[t], fn(s.iloc[: t + 1]).iloc[-1]
            if np.isfinite(a) and np.isfinite(b):
                assert abs(a - b) < 1e-9, f"{name} 前视漂移 @t={t}: {a} vs {b}"
    print("[selfcheck] transforms 数值正确 + 时序变换截断不变（无前视）")


def main() -> None:
    from backtest.factor_lab import (
        VALIDATION_UNIVERSE, _load_universe_prices, build_factor_panel,
        assert_asof_consistent, evaluate_factor, _report_factor,
        factor_correlations, MIN_NAMES_PER_DATE, CORR_MAX, FWD,
    )
    _selfcheck()

    prices = _load_universe_prices(VALIDATION_UNIVERSE)
    if len(prices) < MIN_NAMES_PER_DATE:
        print(f"验证宇宙价格不足（{len(prices)}<{MIN_NAMES_PER_DATE}）——先跑 `python main.py` 预热 cache")
        return

    # as-of 守卫：每个派生候选须全序列≡截断（无前视）
    for name, fn in DERIVED_CANDIDATES.items():
        assert_asof_consistent(prices, fn)
    print(f"[asof] 派生候选全序列≡截断 as-of 一致（{len(DERIVED_CANDIDATES)} 候选）")

    panel = build_factor_panel(prices, DERIVED_CANDIDATES)
    cols = list(DERIVED_CANDIDATES)
    if panel.empty or panel["ticker"].nunique() < MIN_NAMES_PER_DATE:
        print("派生面板不足——检查数据窗口")
        return
    span = f"{panel['date'].min():%Y-%m-%d}→{panel['date'].max():%Y-%m-%d}"
    print(f"\n派生候选面板: tickers={panel['ticker'].nunique()} rows={len(panel)} "
          f"dates={panel['date'].nunique()} ({span})")

    print("\n" + "=" * 72)
    print("R6.4 派生候选因子（价/量变换）— R6.1 门验证（IC/IR + 分位 + 跨年 + 相关性）")
    print("=" * 72)
    verdicts = {}
    for col in cols:
        _report_factor(panel, col, [c for c in cols if c != col])
        verdicts[col] = evaluate_factor(panel, col, [c for c in cols if c != col])["verdict"]

    print("\n因子两两 |corr|（>%.2f 视为冗余，剪枝）:" % CORR_MAX)
    print(factor_correlations(panel, cols).round(2).to_string())

    passed = [c for c, v in verdicts.items() if v == "PASS"]
    print(f"\nVERDICT: 过门 {passed if passed else '无'} / {len(cols)}。"
          f"{'→ 入 R6.5 集成 + 并入 factor_engine' if passed else '→ 如实记录不 merge（同 R5/R6.2/R6.3）'}")


if __name__ == "__main__":
    main()
