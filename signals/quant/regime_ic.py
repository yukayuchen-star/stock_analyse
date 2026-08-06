"""R7.2 因子择时 / 制度条件 IC Regime-Conditional IC（对症 R6 的第二条路，零新增数据）。

R6 因子全样本 |t|<2，但**全样本 IC 会把状态相依的边平均成零**：若某因子在低 VIX 里
+IC、在高 VIX 里 −IC，两者相消 → 全样本≈0。本模块把每日横截面 IC 序列**按 VIX 制度
分桶**（复用宏观主轴已有的四档，CLAUDE.md：<15 / 15–25 / 25–35 / >35），看边是否
**集中在某个我们本就在实盘按 VIX 门控的制度**里——若是，则「因子×制度」既真实又可执行。

VIX 历史直接取 FRED `VIXCLS`（`DataPipeline().fred.get_macro`，与宏观主轴同源，无新数据）。

⚠️ **多重比较纪律**：4 桶 × 数因子 = 一堆格子，挑最大的那个必然虚高。故**预注册**每个
因子的边**应当**出现在哪个制度（经济先验），只裁决预注册格子，并要求该格 |IC|≥门槛 +
样本日足 + 符号与先验一致。事后翻遍所有格子找显著只作探索性展示、不作 merge 依据
（承接 [[insight_r5_volume_confirmation]] pullback 门照搬证伪的教训）。

预注册假设（经济先验）：
  · amihud_20  非流动性溢价 → **CALM(<15)**：风险偏好高时才为不流动性付溢价；恐慌里
                 不流动名被抛售 → 反号。
  · reversal_5 短期反转     → **STRESS+(≥25)**：恐慌超调后的均值回复最强。
  · lowvol_20  低波动异象   → **STRESS+(≥25)**：避险期资金涌向低 beta。
  · dist_high252 趋势强度   → **CALM/NORMAL(<25)**：趋势市里动量有效；高波动拐点处动量崩溃。

用法：`python -m signals.quant.regime_ic`（先 `python main.py` 预热价格 + FRED cache）。
"""
from __future__ import annotations

from typing import Dict, Sequence

import numpy as np
import pandas as pd

from backtest.factor_lab import FactorFn, REFERENCE_FACTORS
from signals.quant.transforms import DERIVED_CANDIDATES


# VIX 四档（与 CLAUDE.md / regime.classify_vix 边界一致；标签用于分桶）
VIX_BINS = [-np.inf, 15.0, 25.0, 35.0, np.inf]
VIX_LABELS = ["calm(<15)", "normal(15-25)", "stress(25-35)", "panic(>35)"]

# 参与制度检验的因子（R6 里各自弱但符号对的）
REGIME_FACTORS: Dict[str, FactorFn] = {
    "amihud_20":    DERIVED_CANDIDATES["amihud_20"],
    "reversal_5":   REFERENCE_FACTORS["reversal_5"],
    "lowvol_20":    REFERENCE_FACTORS["lowvol_20"],
    "dist_high252": REFERENCE_FACTORS["dist_high252"],
    "mom_roc20":    REFERENCE_FACTORS["mom_roc20"],
}

# 预注册：因子 → 假设生效的制度桶（只裁决这些格子）
PRE_REGISTERED = {
    "amihud_20":    ["calm(<15)"],
    "reversal_5":   ["stress(25-35)", "panic(>35)"],
    "lowvol_20":    ["stress(25-35)", "panic(>35)"],
    "dist_high252": ["calm(<15)", "normal(15-25)"],
    "mom_roc20":    ["calm(<15)", "normal(15-25)"],
}

IC_MIN = 0.02          # 制度内 |IC| 门槛（与全样本门一致）
MIN_BUCKET_DAYS = 40   # 桶内最少 IC 日数（否则不足以判定，overlap 后有效样本更少）


def load_vix_by_date(dates: pd.DatetimeIndex) -> pd.Series:
    """取 FRED VIXCLS 历史并对齐到面板交易日（ffill ≤5 日填非交易缺口）。"""
    from data.pipeline import DataPipeline
    df = DataPipeline().fred.get_macro("VIXCLS")
    if df is None or df.empty:
        return pd.Series(dtype=float)
    vix = df["value"].copy()
    vix.index = pd.to_datetime(vix.index)
    vix = vix.sort_index()
    target = pd.DatetimeIndex(sorted(set(dates)))
    return vix.reindex(target.union(vix.index)).ffill(limit=5).reindex(target)


def regime_ic_table(panel: pd.DataFrame, factor_col: str, vix_by_date: pd.Series,
                    primary_h: int = 10) -> pd.DataFrame:
    """因子在各 VIX 制度桶内的 IC 均值 / 日数 / overlap 调整 t。"""
    from backtest.factor_lab import daily_ic
    ic = daily_ic(panel, factor_col, f"fwd{primary_h}")
    if ic.empty:
        return pd.DataFrame()
    v = vix_by_date.reindex(ic.index)
    bucket = pd.cut(v, bins=VIX_BINS, labels=VIX_LABELS)
    rows = []
    for lab in VIX_LABELS:
        sub = ic[bucket.values == lab].dropna()
        n = len(sub)
        if n == 0:
            rows.append({"regime": lab, "n_days": 0, "ic_mean": np.nan, "t_stat": np.nan})
            continue
        mean = float(sub.mean())
        std = float(sub.std(ddof=1)) if n > 1 else np.nan
        ir = mean / std if (std and np.isfinite(std) and std > 0) else np.nan
        n_eff = max(n / primary_h, 1.0)
        t = ir * np.sqrt(n_eff) if np.isfinite(ir) else np.nan
        rows.append({"regime": lab, "n_days": n, "ic_mean": mean, "t_stat": t})
    return pd.DataFrame(rows).set_index("regime")


def main() -> None:
    from backtest.factor_lab import (
        VALIDATION_UNIVERSE, _load_universe_prices, build_factor_panel,
        assert_asof_consistent, MIN_NAMES_PER_DATE, T_MIN,
    )

    prices = _load_universe_prices(VALIDATION_UNIVERSE)
    if len(prices) < MIN_NAMES_PER_DATE:
        print(f"验证宇宙价格不足（{len(prices)}<{MIN_NAMES_PER_DATE}）——先跑 `python main.py` 预热 cache")
        return

    for name, fn in REGIME_FACTORS.items():
        assert_asof_consistent(prices, fn)
    panel = build_factor_panel(prices, REGIME_FACTORS)
    if panel.empty or panel["ticker"].nunique() < MIN_NAMES_PER_DATE:
        print("面板不足——检查数据窗口")
        return

    vix = load_vix_by_date(pd.DatetimeIndex(panel["date"].unique()))
    if vix.dropna().empty:
        print("VIXCLS 历史不可用（FRED cache 未预热 / 无 key）——先跑 `python main.py`")
        return
    span = f"{panel['date'].min():%Y-%m-%d}→{panel['date'].max():%Y-%m-%d}"
    print(f"\n面板 tickers={panel['ticker'].nunique()} dates={panel['date'].nunique()} ({span}) "
          f"| VIX 覆盖 {vix.dropna().index.min():%Y-%m}→{vix.dropna().index.max():%Y-%m}")

    print("\n" + "=" * 76)
    print("R7.2 制度条件 IC — 逐因子 × VIX 四档（fwd10）。★=预注册假设格子")
    print("=" * 76)
    conditional_pass = []
    for col in REGIME_FACTORS:
        tbl = regime_ic_table(panel, col, vix)
        if tbl.empty:
            continue
        print(f"\n── {col} ──  (预注册生效制度: {PRE_REGISTERED.get(col, [])})")
        for lab in VIX_LABELS:
            r = tbl.loc[lab]
            star = "★" if lab in PRE_REGISTERED.get(col, []) else " "
            ic_s = f"{r['ic_mean']:+.4f}" if np.isfinite(r["ic_mean"]) else "  n/a "
            t_s = f"{r['t_stat']:+.2f}" if np.isfinite(r["t_stat"]) else " n/a "
            print(f"  {star} {lab:<15} n_days={int(r['n_days']):>4}  IC={ic_s}  t={t_s}")
        # 只裁决预注册格子
        for lab in PRE_REGISTERED.get(col, []):
            r = tbl.loc[lab]
            if (r["n_days"] >= MIN_BUCKET_DAYS and np.isfinite(r["ic_mean"])
                    and abs(r["ic_mean"]) >= IC_MIN and np.isfinite(r["t_stat"])
                    and abs(r["t_stat"]) >= T_MIN):
                conditional_pass.append((col, lab, r["ic_mean"], r["t_stat"], int(r["n_days"])))

    print("\n" + "=" * 76)
    if conditional_pass:
        print("预注册制度格子内过门（|IC|≥%.2f ∧ |t|≥%.1f ∧ n_days≥%d）:" % (IC_MIN, T_MIN, MIN_BUCKET_DAYS))
        for col, lab, ic, t, n in conditional_pass:
            print(f"  ✓ {col} @ {lab}: IC={ic:+.4f} t={t:+.2f} (n={n}) → R7 制度门候选")
        print("→ 下一步：OOS 前向 + 与 VIX 仓位门控联动（因子只在该制度计权）")
    else:
        print("预注册制度格子内**无一过门** → 如实记录不 merge（同 R5/R6）。")
        print("（事后翻非预注册格子若见强项，只作探索性提示，多重比较下不作 merge 依据）")


if __name__ == "__main__":
    main()
