"""R11 财报反应因子 EAR（Earnings Announcement Return）—— 过 R6.1 门的回测。

为什么是它（2026-09-28）：R6/R7/R10 把纯价格因子测穷了 —— 同一份 OHLCV 的任何变形都只是
重写已有信息（R10 `tfm_qspread` = 已实现波动率）。EAR 带进来的新信息是**财报日**
（yfinance 有 2020 年起的历史日期，可回测），而不是又一个价格变形。
选 EAR 不选 SUE：yfinance 的 surprise 是 GAAP 实际撞 non-GAAP 预期的混口径量
（GOOGL +214% 假象，见 `sleeves.md`），EAR 用市场自己的反应，天然不受口径影响。

**先验诚实**：PEAD 在大盘股上已大幅衰减（Martineau 2021 认为基本消失），本宇宙是大盘股
⇒ **预期大概率 REJECT**。做它是因为便宜、且是唯一能「现在就回答」的新信息候选。

## 预注册口径（2026-09-28 写于运行之前，跑完不改）

- **事件日** d = yfinance `get_earnings_dates` 里 `Reported EPS` 非空的日期（美东本地日）；
  d0 = d 当天或之后的第一个交易日。
- **EAR** = `C[d0+1]/C[d0-1] − 1` − SPY 同窗口收益（[−1,+1] 三日窗，Brandt et al. 2008）。
  用三日窗正是为了**不必判断盘前/盘后发布**：两种情况的反应都落在窗内。
- **生效**：从 d0+2（窗口收盘后的第一根 K）起，持续 `HOLD_TD=60` 个交易日，被下一次事件覆盖。
  方向：高 EAR ⇒ 预期高前向收益（orient=+）。
- **门**：`factor_lab.evaluate_factor` 原样（|IC|≥0.02、overlap 调整 |t|≥2、分位单调、跨年同号、
  与参照因子 |corr|<0.70），主口径 fwd10，并列 fwd5/fwd20。**不为它放宽任何阈值。**
- **张成检验**（R10 那一招）：EAR 的窗口落在 `mom_roc20` 的 20 日内，二者天然相关。
  另报 EAR 对四个参照因子的池化 adj-R²，与逐日横截面残差化后的残差 IC。
  **过门但残差 IC 塌掉 ⇒ 它是动量的另一种写法，不 merge。**
- **处置**：PASS 才讨论 merge 进 quant；REJECT 照实记录、代码保留、默认不接实盘（R6/R7/R10 先例）。

用法：`python -m signals.quant.ear`（首跑会下载验证宇宙价格 + 78 只财报日，之后走 cache）。
"""
from __future__ import annotations

from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

from backtest.factor_lab import FactorFn

HOLD_TD = 60        # 因子有效期（交易日），约一个季度，下一次财报前失效


def make_ear_fn(event_dates: Sequence[str], spy_close: pd.Series) -> FactorFn:
    """单票 EAR 因子函数（闭包捕获该票财报日与 SPY 收盘）。

    全部按 df 自身的位置索引计算：窗口右端 d0+1 超出 df 末尾的事件直接跳过，
    故截断到 t 的结果与全序列在 t 处逐位相同（`assert_asof_consistent` 可证）。
    """
    ev = pd.DatetimeIndex(pd.to_datetime(list(event_dates))).sort_values()

    def ear(df: pd.DataFrame) -> pd.Series:
        close = df["Close"].astype(float)
        idx = df.index
        spy = spy_close.reindex(idx)
        out = np.full(len(idx), np.nan)
        for d in ev:
            i0 = int(idx.searchsorted(d))          # d 当天或之后的第一个交易日
            if i0 < 1 or i0 + 1 >= len(idx):
                continue
            r = close.iloc[i0 + 1] / close.iloc[i0 - 1] - 1.0
            m = spy.iloc[i0 + 1] / spy.iloc[i0 - 1] - 1.0
            if not (np.isfinite(r) and np.isfinite(m)):
                continue
            out[i0 + 2: i0 + 2 + HOLD_TD] = r - m  # 后一事件覆盖前一事件
        return pd.Series(out, index=idx)

    return ear


def residual_ic(panel: pd.DataFrame, col: str, refs: Sequence[str], fwd: str,
                min_names: int) -> pd.Series:
    """逐日横截面：col 的秩对参照因子的秩做 OLS，残差对 fwd 求 Spearman IC。"""
    ics = {}
    for d, g in panel.groupby("date"):
        s = g[[col, *refs, fwd]].dropna()
        if len(s) < max(min_names, len(refs) + 3):
            continue
        X = np.column_stack([np.ones(len(s))] + [s[r].rank().to_numpy() for r in refs])
        y = s[col].rank().to_numpy()
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = pd.Series(y - X @ beta, index=s.index)
        if resid.std() > 0:
            ics[d] = resid.corr(s[fwd], method="spearman")
    return pd.Series(ics, dtype=float).dropna().sort_index()


def pooled_adj_r2(panel: pd.DataFrame, col: str, refs: Sequence[str]) -> float:
    """col 被参照因子线性张成的程度（池化 OLS adj-R²）。"""
    s = panel[[col, *refs]].dropna()
    n, k = len(s), len(refs)
    if n <= k + 1:
        return float("nan")
    X = np.column_stack([np.ones(n), s[list(refs)].to_numpy()])
    y = s[col].to_numpy()
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    ss_res = float(((y - X @ beta) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return 1 - (1 - r2) * (n - 1) / (n - k - 1)


def main() -> None:
    from backtest.factor_lab import (
        FWD, PRIMARY_H, MIN_NAMES_PER_DATE, REFERENCE_FACTORS, VALIDATION_UNIVERSE,
        _load_universe_prices, _report_factor, assert_asof_consistent, build_factor_panel,
        ir_stats,
    )
    from data.pipeline import DataPipeline

    pl = DataPipeline()
    prices = _load_universe_prices([*VALIDATION_UNIVERSE, "SPY"])
    spy = prices.pop("SPY", None)
    if spy is None or spy.empty:
        print("SPY 价格缺失，无法算超额反应 —— 中止")
        return
    spy_close = spy["Close"].astype(float)

    dates = {tk: pl.yf.get_earnings_history(tk) for tk in prices}
    no_dates = sorted(tk for tk, d in dates.items() if not d)
    first = min((d[0] for d in dates.values() if d), default="n/a")
    print(f"财报日：{sum(len(d) for d in dates.values())} 个事件 / {len(prices)} 只，最早 {first}；"
          f"无日期 {len(no_dates)} 只 {no_dates}")

    # as-of 守卫：每只票自己的闭包各抽样检查（EAR 依赖票特定的日期，无法一个 fn 通吃）
    checked = 0
    rng = np.random.default_rng(11)
    dated = sorted(t for t in prices if dates[t])
    for tk in rng.choice(dated, size=min(12, len(dated)), replace=False):
        checked += assert_asof_consistent({tk: prices[tk]}, make_ear_fn(dates[tk], spy_close),
                                          n_samples=10)
    print(f"[asof] EAR 全序列≡截断：{checked} 个抽样点 0 偏差")

    refs = list(REFERENCE_FACTORS)
    frames: List[pd.DataFrame] = []
    for tk, df in prices.items():
        if not dates[tk]:
            continue
        fns: Dict[str, FactorFn] = {**REFERENCE_FACTORS, "ear": make_ear_fn(dates[tk], spy_close)}
        frames.append(build_factor_panel({tk: df}, fns))
    panel = pd.concat(frames, ignore_index=True).sort_values(["date", "ticker"])
    cov = panel["ear"].notna().mean()
    print(f"面板: tickers={panel['ticker'].nunique()} rows={len(panel)} "
          f"dates={panel['date'].nunique()} ({panel['date'].min():%Y-%m-%d}→{panel['date'].max():%Y-%m-%d})"
          f"  EAR 覆盖 {cov:.1%}")

    print("\n" + "=" * 72 + "\nR11 EAR — R6.1 门（预注册口径，阈值不放宽）\n" + "=" * 72)
    _report_factor(panel, "ear", refs)

    print("\n── 张成检验（EAR 是不是动量的另一种写法）──")
    print(f"   池化 adj-R²(ear ~ {'+'.join(refs)}) = {pooled_adj_r2(panel, 'ear', refs):.3f}")
    for h in FWD:
        st = ir_stats(residual_ic(panel, "ear", refs, f"fwd{h}", MIN_NAMES_PER_DATE), h)
        print(f"   残差 fwd{h:>2}: IC={st['ic_mean']:+.4f} IR={st['ir']:+.3f} t={st['t_stat']:+.2f} "
              f"(n_days={st['n_days']})" + ("   ← 主口径" if h == PRIMARY_H else ""))


if __name__ == "__main__":
    main()
