"""R7.1 弱正交信号合成因子 Composite（对症 R6「单信号 t<2」的第一条路）。

R6 总账：3 类想法 × 0 幸存者，共同败因**同一个**——经济符号对、跨年同号，但在
高效大盘宇宙上 |t|<2（amihud_20 t=1.76、svr_z、结构因子皆然）。本模块检验一个**有
代数依据**的补救：若 k 个各自弱（每日横截面 IC≈c、符号对）且彼此近正交（平均两两
相关 ρ̄≈0）的信号等权合成，则合成因子每日 IC ≈ c·√k / √(1+(k−1)ρ̄)。c≈0.02、k=4、
ρ̄≈0 → ≈0.04（是 0.02 门槛的两倍）。这不是许愿，是**分散化摊薄特质噪声**的必然；
唯一失败模式是 ρ̄ 偏高（成分其实是同一个信号换皮）。

**预注册（防子集择优 snooping）**：只测**一个**合成——4 条经济上相异的异象轴、
等权、横截面 rank(pct) 平均。每条轴的 FactorFn 已把符号摆正为「高值=预期高前向」：
  1. 非流动性 amihud_20（+，流动性溢价；R6.4 本宇宙个体近失 IC+.025）
  2. 短期反转 reversal_5（+，已取负 ROC5）
  3. 低风险 lowvol_20（+，已取负 20日波动）
  4. 趋势强度 dist_high252（+，距 52 周高，越近越强）
反转(短)与趋势(长)天然低相关、流动性与低风险另属两轴 → 四轴张开、ρ̄ 应小。

**合成用 rank 平均而非 z 平均**：amihud 尾巴肥，z-score 会被极值主导；横截面 rank(pct)
稳健且沿用 `relative.py` 既有 idiom；rank 只在**单日跨票**施用 → 无时间前视。

诚实边界（承接 [[insight_chan_backtest_survivorship]] / R5 pullback 门证伪教训）：
过 R6.1 门（|IC|≥.02 ∧ |t|≥2 ∧ 单调 ∧ 跨年同号）才谈 merge；且合成天然与自身成分
高相关，独立性要看它对**既有五因子 sleeve** 是否新增正交信息，而非对成分——两问分开，
本轮先答主假设（分散化能否顶过 t=2），如实记录，不过不 merge。

用法：`python -m signals.quant.composite`（先 `python main.py` 预热价格 cache）。
"""
from __future__ import annotations

from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

from backtest.factor_lab import FactorFn, REFERENCE_FACTORS
from signals.quant.transforms import DERIVED_CANDIDATES


# ── 成分（均已摆正为「高值=预期高前向」）──────────────────────────
COMPONENTS: Dict[str, FactorFn] = {
    "amihud_20":    DERIVED_CANDIDATES["amihud_20"],   # 非流动性（+）
    "reversal_5":   REFERENCE_FACTORS["reversal_5"],   # 短期反转（+，已取负）
    "lowvol_20":    REFERENCE_FACTORS["lowvol_20"],    # 低波动（+，已取负）
    "dist_high252": REFERENCE_FACTORS["dist_high252"], # 距 52 周高（+，越近越强）
}


def build_composite_column(
    panel: pd.DataFrame,
    components: Sequence[str],
    out: str = "composite",
) -> pd.DataFrame:
    """在长面板上加一列合成因子 = 各成分横截面 rank(pct) 的等权均值。

    rank 只在**单个日期跨票**（groupby date）施用 → 仅同日数据 → 无时间前视。
    某成分当日全 NaN 时该成分不计入该行均值（`mean(skipna)` 天然处理）。
    """
    ranks = [panel.groupby("date")[c].rank(pct=True) for c in components]
    panel = panel.copy()
    panel[out] = pd.concat(ranks, axis=1).mean(axis=1, skipna=True)
    return panel


def main() -> None:
    from backtest.factor_lab import (
        VALIDATION_UNIVERSE, _load_universe_prices, build_factor_panel,
        assert_asof_consistent, evaluate_factor, _report_factor, daily_ic,
        ir_stats, factor_correlations, MIN_NAMES_PER_DATE, CORR_MAX, FWD, PRIMARY_H,
    )

    prices = _load_universe_prices(VALIDATION_UNIVERSE)
    if len(prices) < MIN_NAMES_PER_DATE:
        print(f"验证宇宙价格不足（{len(prices)}<{MIN_NAMES_PER_DATE}）——先跑 `python main.py` 预热 cache")
        return

    # as-of 守卫：每个成分须全序列≡截断（合成用的横截面 rank 是面板后处理、单日跨票，另证无前视）
    for name, fn in COMPONENTS.items():
        assert_asof_consistent(prices, fn)
    print(f"[asof] 成分因子全序列≡截断 as-of 一致（{len(COMPONENTS)} 成分）")

    comp_cols = list(COMPONENTS)
    panel = build_factor_panel(prices, COMPONENTS)
    if panel.empty or panel["ticker"].nunique() < MIN_NAMES_PER_DATE:
        print("成分面板不足——检查数据窗口")
        return
    panel = build_composite_column(panel, comp_cols, out="composite")
    span = f"{panel['date'].min():%Y-%m-%d}→{panel['date'].max():%Y-%m-%d}"
    print(f"\n面板: tickers={panel['ticker'].nunique()} rows={len(panel)} "
          f"dates={panel['date'].nunique()} ({span})")

    print("\n" + "=" * 72)
    print("R7.1 合成因子 — 先看各成分个体强度（预期各自弱），再看合成能否顶过 t=2")
    print("=" * 72)
    # 成分个体（诊断分散化前提：符号对不对、彼此相关有多低）
    for col in comp_cols:
        s = ir_stats(daily_ic(panel, col, f"fwd{PRIMARY_H}"), PRIMARY_H)
        print(f"  · {col:<13} fwd{PRIMARY_H}: IC={s['ic_mean']:+.4f} "
              f"t={s['t_stat']:+.2f} hit={s['hit_rate']:.2f}")
    print("\n成分两两 |corr|（ρ̄ 越低分散化收益越大）:")
    print(factor_correlations(panel, comp_cols).round(2).to_string())
    pair = factor_correlations(panel, comp_cols)
    off = pair.where(~np.eye(len(comp_cols), dtype=bool))
    print(f"平均两两 |corr| ρ̄={np.nanmean(off.values):.3f}  "
          f"理论合成 IC≈c·√{len(comp_cols)}/√(1+(k-1)ρ̄)")

    print("\n" + "=" * 72)
    print("合成因子（4 轴等权 rank 均值）— R6.1 门")
    print("=" * 72)
    # 独立性看对「非成分」参照因子（成分自身高相关是构造使然，不作独立性判据）
    others = [c for c in REFERENCE_FACTORS if c not in COMPONENTS]  # mom_roc20
    _report_factor(panel, "composite", others)
    res = evaluate_factor(panel, "composite", others)

    verdict = res["verdict"]
    print(f"\nVERDICT: composite = {verdict}。"
          f"{'→ 进 R7 集成候选（再验对生产五因子独立性）' if verdict == 'PASS' else '→ 如实记录不 merge（同 R5/R6）'}")


if __name__ == "__main__":
    main()
