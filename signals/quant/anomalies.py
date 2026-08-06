"""R7.3 大盘可存活异象 Tier-2 Anomalies（对症 R6 的第三条路——换信号本身）。

R6 的因子（amihud 非流动性、缠论结构、做空流量）在**大盘上先天弱**：流动性溢价要小盘、
做空流量占比要难借券。本模块改用**学术文献里被记录为在大盘也存活**的异象，直接冲 R6.1 门：

  1. **MAX 彩票效应**（Bali-Cakici-Whitelaw 2011）：过去 20 日单日最大涨幅越高 → 前向越低
     （博彩偏好把右尾拉贵）。纯价格，orient=−MAX。
  2. **特质偏度 idio-skew**（Boyer-Mitton-Vorkink 2010）：市场残差收益的滚动偏度越高 → 前向越低。
  3. **特质波动 idio-vol**（Ang-Hodrick-Xing-Zhang 2006）：市场残差波动越高 → 前向越低（IVOL 之谜）。
  4. **残差动量 residual-mom**（Blitz-Huij-Martens 2011）：对市场中性化后的 12−1 动量，
     比原始动量更稳、更少崩溃。orient=+。

特质类共用一次**滚动市场中性化**（对 SPY 日收益滚动 β，残差 = r − β·m；尾窗 → 无前视）。
SPY 序列在 `main` 预载、以闭包捕获，`assert_asof_consistent` 截断 df 时 SPY 按 df.index
重取 → 仍只用 ≤t，守卫照过。所有因子 orient 为「高值=预期高前向」，逐个过门（非合成）——
问题是：**文献大盘异象能否在这个宇宙清过 t=2 门**（R6 自造因子做不到的地方）。

诚实边界：过门才谈 merge，不过如实记录（同 R5/R6）；idio 类与既有 vol/trend 因子可能相关，
独立性在门内一并查。用法：`python -m signals.quant.anomalies`（先 `python main.py` 预热 cache）。
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from backtest.factor_lab import FactorFn


_RESID_WIN = 60     # 市场中性化 + 特质偏度/波动窗口
_MOM_LOOK = 100     # 残差动量累计窗口（配合跳过近月）
_MOM_SKIP = 20      # 12−1 动量跳过最近 20 日


def _market_resid(df: pd.DataFrame, mkt_ret: pd.Series, win: int = _RESID_WIN) -> pd.Series:
    """对市场（SPY）日收益的滚动 β 中性化残差 r − β·m（尾窗，无前视）。"""
    r = df["Close"].astype(float).pct_change()
    m = mkt_ret.reindex(df.index)
    cov = r.rolling(win).cov(m)
    var = m.rolling(win).var()
    beta = cov / var.replace(0.0, np.nan)
    return r - beta * m


def build_anomaly_factors(mkt_ret: pd.Series) -> Dict[str, FactorFn]:
    """构造 4 个异象 FactorFn（闭包捕获市场收益；均 orient 高值=预期高前向）。"""
    def max_lottery(df: pd.DataFrame) -> pd.Series:
        return -df["Close"].astype(float).pct_change().rolling(20).max()

    def idio_skew(df: pd.DataFrame) -> pd.Series:
        return -_market_resid(df, mkt_ret).rolling(_RESID_WIN).skew()

    def idio_vol(df: pd.DataFrame) -> pd.Series:
        return -_market_resid(df, mkt_ret).rolling(_RESID_WIN).std()

    def resid_mom(df: pd.DataFrame) -> pd.Series:
        resid = _market_resid(df, mkt_ret)
        # 12−1：累计 [t-20, t-119] 的残差，跳过最近 20 日（尾窗+shift 用旧数据，无前视）
        return resid.shift(_MOM_SKIP).rolling(_MOM_LOOK).sum()

    return {"max_lottery": max_lottery, "idio_skew": idio_skew,
            "idio_vol": idio_vol, "resid_mom": resid_mom}


def _load_market_returns() -> pd.Series:
    """SPY 日收益（市场基准；cache 命中即离线）。"""
    from backtest.factor_lab import _load_universe_prices
    px = _load_universe_prices(["SPY"])
    spy = px.get("SPY")
    if spy is None or spy.empty:
        return pd.Series(dtype=float)
    return spy["Close"].astype(float).pct_change()


def main() -> None:
    from backtest.factor_lab import (
        VALIDATION_UNIVERSE, _load_universe_prices, build_factor_panel,
        assert_asof_consistent, evaluate_factor, _report_factor,
        factor_correlations, MIN_NAMES_PER_DATE, CORR_MAX,
    )

    mkt_ret = _load_market_returns()
    if mkt_ret.dropna().empty:
        print("SPY 基准不可用——先跑 `python main.py` 预热 cache")
        return
    factors = build_anomaly_factors(mkt_ret)

    prices = _load_universe_prices(VALIDATION_UNIVERSE)
    if len(prices) < MIN_NAMES_PER_DATE:
        print(f"验证宇宙价格不足（{len(prices)}<{MIN_NAMES_PER_DATE}）——先跑 `python main.py`")
        return

    for name, fn in factors.items():
        assert_asof_consistent(prices, fn)
    print(f"[asof] 异象因子全序列≡截断 as-of 一致（{len(factors)} 因子）")

    panel = build_factor_panel(prices, factors)
    cols = list(factors)
    if panel.empty or panel["ticker"].nunique() < MIN_NAMES_PER_DATE:
        print("异象面板不足——检查数据窗口")
        return
    span = f"{panel['date'].min():%Y-%m-%d}→{panel['date'].max():%Y-%m-%d}"
    print(f"\n面板: tickers={panel['ticker'].nunique()} rows={len(panel)} "
          f"dates={panel['date'].nunique()} ({span})")

    print("\n" + "=" * 72)
    print("R7.3 大盘可存活异象 — R6.1 门（MAX / idio-skew / idio-vol / resid-mom）")
    print("=" * 72)
    verdicts = {}
    for col in cols:
        _report_factor(panel, col, [c for c in cols if c != col])
        verdicts[col] = evaluate_factor(panel, col, [c for c in cols if c != col])["verdict"]

    print("\n因子两两 |corr|（>%.2f 视为冗余）:" % CORR_MAX)
    print(factor_correlations(panel, cols).round(2).to_string())

    passed = [c for c, v in verdicts.items() if v == "PASS"]
    print(f"\nVERDICT: 过门 {passed if passed else '无'} / {len(cols)}。"
          f"{'→ 进 R7 集成候选' if passed else '→ 如实记录不 merge（同 R5/R6）'}")


if __name__ == "__main__":
    main()
