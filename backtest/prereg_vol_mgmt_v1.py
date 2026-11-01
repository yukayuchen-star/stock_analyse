"""波动率管理 v1：`^GSPC` 1960–1993 主检验 · `^IXIC` 1976–1999 复现（口径见仓库根 `PREREG-vol-mgmt-v1.md`）。

数据与依赖代码冻结，sha 不符即拒绝运行。数值结果落 `output/prereg_vol_mgmt_v1/results.json`，
RESULTS.md 据此撰写。用法：`python -m backtest.prereg_vol_mgmt_v1`
"""
from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.prereg_vix_gate_v1 import (excess_series, delta_sharpe, gate_exposure, month_block_boot, perf, sharpe,
                                         summarize)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "prereg_vol_mgmt_v1"
VIX_DATA = ROOT / "output" / "prereg_vix_gate_v1" / "data.pkl"

FROZEN = {
    "PREREG-vol-mgmt-v1.md": "4012b46a66f6c26c689e7841e2714bf60dbd9125239d883148795b85c40023a7",
    "output/prereg_vol_mgmt_v1/data.pkl": "f0291ce6ca3ee300743a3e39b2b1ecdea15216843db05d06bb8e9174a0476930",
    "output/prereg_vix_gate_v1/data.pkl": "7f6258930c983f0b3580dc93cf847f37ce7cae0a289f4bab37ba821752a1032c",
    "backtest/prereg_vix_gate_v1.py": "e56aa9c8b63e5a858ad67f8fc34398fb79361b5ad9f4433639d69472035a8b12",
    "signals/macro/regime.py": "b0c7c1c5feb3d07c72efc4a66b712ff6ce11ae240b06245942d7742abfee3d93",
}
SEED = 20261102
TARGET, WINDOW = 0.70, 20
COST, COST_HIGH = 0.0005, 0.0020
MAIN = ("1960-01-04", "1993-01-28")
REPL = ("1976-02-05", "1999-03-09")
SPLIT = pd.Timestamp("1977-01-01")
EPISODES = {"1973-01~1974-12": ("1973-01-01", "1974-12-31"), "1987-08~1987-12": ("1987-08-01", "1987-12-31")}
DIVIDEND = 0.035
MIN_HISTORY = 252      # 校准最少观测；主、复现窗口的暖机都远长于此，只影响 §7 的 QQQ、SPY


def check_frozen() -> None:
    for rel, sha in FROZEN.items():
        got = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if got != sha:
            raise SystemExit(f"冻结输入不符：{rel} {got[:12]}… ≠ {sha[:12]}…")


def _solve_c(h: np.ndarray, target: float) -> float:
    lo, hi = 0.0, float(h.max())
    for _ in range(60):
        c = (lo + hi) / 2
        if np.minimum(1.0, c / h).mean() < target:
            lo = c
        else:
            hi = c
    return (lo + hi) / 2


def vol_exposure(close: pd.Series, target: float = TARGET, win: int = WINDOW) -> pd.Series:
    """每月首个交易日用截至上月末的全部历史校准 c，当月沿用；w_t = min(1, c_t / σ̂²_t)。"""
    rv = close.pct_change().rolling(win).var() * 252
    per = close.index.to_period("M")
    c = pd.Series(np.nan, index=close.index)
    vals = rv.to_numpy()
    for m in per.unique():
        pos = np.flatnonzero(per == m)
        h = vals[: pos[0]]
        h = h[np.isfinite(h) & (h > 0)]
        if len(h) >= MIN_HISTORY:
            c.iloc[pos] = _solve_c(h, target)
    return (c / rv).clip(upper=1.0)


def rf_for(close: pd.Series, irx: pd.Series) -> pd.Series:
    idx = close.index
    return (irx.reindex(idx.union(irx.index)).ffill().reindex(idx) / 100 / 252).shift(1)


def build(close, irx, window=None, cost=COST, **kw) -> pd.DataFrame:
    df = excess_series(close, vol_exposure(close, **kw), rf_for(close, irx), cost)
    return df.loc[window[0]: window[1]] if window else df


def report(df: pd.DataFrame) -> dict:
    return {"n_days": len(df), "start": str(df.index[0].date()), "end": str(df.index[-1].date()),
            "mean_exposure": float(df["w"].mean()), "annual_turnover": float(df["w"].diff().abs().mean() * 252),
            "sharpe_managed": sharpe(df["gate"].to_numpy()), "sharpe_hold": sharpe(df["ex"].to_numpy()),
            "perf_managed": perf(df["gate"] + df["rf"]), "perf_hold": perf(df["r"])}


def d_of(df: pd.DataFrame) -> float:
    return delta_sharpe(df["gate"].to_numpy(), df["ex"].to_numpy())


def boot(df: pd.DataFrame, a="gate", b="ex") -> dict:
    return summarize(delta_sharpe(df[a].to_numpy(), df[b].to_numpy()), month_block_boot(df, a, b, seed=SEED))


def g0(close: pd.Series, irx: pd.Series) -> dict:
    full = vol_exposure(close)
    rng = np.random.default_rng(SEED)
    picks = rng.choice(np.arange(3000, len(close)), size=30, replace=False)
    worst = 0.0
    for p in picks:
        T = close.index[p]
        cut = vol_exposure(close.loc[:T])
        a, b = cut.to_numpy(), full.reindex(cut.index).to_numpy()
        assert (np.isnan(a) == np.isnan(b)).all()
        worst = max(worst, float(np.nanmax(np.abs(a - b))))
    df = excess_series(close, full, rf_for(close, irx), COST)
    lag_ok = bool((df["w"] == full.shift(2).reindex(df.index)).all())
    return {"n": 30, "max_abs_diff": worst, "lag_two_bars": lag_ok, "pass": worst == 0.0 and lag_ok}


def alpha(df: pd.DataFrame) -> dict:
    """OLS（普通 t，未做 HAC，只作参考）。"""
    x, y = df["ex"].to_numpy(), df["gate"].to_numpy()
    A = np.column_stack([np.ones(len(x)), x])
    b, *_ = np.linalg.lstsq(A, y, rcond=None)
    e = y - A @ b
    cov = e.var(ddof=2) * np.linalg.inv(A.T @ A)
    return {"alpha_annual": float(b[0] * 252), "beta": float(b[1]), "t": float(b[0] / np.sqrt(cov[0, 0]))}


def seen_window(asset: str, V: dict) -> dict:
    close = V[asset]["Close"].astype(float)
    rf = rf_for(close, V["IRX"])
    m = excess_series(close, vol_exposure(close), rf, COST)
    g = excess_series(close, gate_exposure(V["VIXCLS"], close.index), rf, COST)
    both = pd.DataFrame({"managed": m["gate"], "gate4": g["gate"], "ex": m["ex"]}).dropna()
    return {**report(m), "vs_hold": boot(m), "vs_four_tier": boot(both, "managed", "gate4"),
            "sharpe_four_tier": sharpe(both["gate4"].to_numpy()), "four_tier_mean_exposure": float(g["w"].reindex(both.index).mean())}


def robustness(close, irx) -> dict:
    out = {}
    for name, kw in {"target_60": {"target": 0.60}, "target_80": {"target": 0.80}, "win_10": {"win": 10}, "win_60": {"win": 60}}.items():
        df = build(close, irx, MAIN, **kw)
        out[name] = {**boot(df), "mean_exposure": float(df["w"].mean())}
    w = vol_exposure(close)
    first = pd.Series(close.index.to_period("M"), index=close.index)
    monthly = w.where(first != first.shift(1)).ffill()
    df = excess_series(close, monthly, rf_for(close, irx), COST).loc[MAIN[0]: MAIN[1]]
    out["monthly_rebalance"] = {**boot(df), "mean_exposure": float(df["w"].mean()),
                                "annual_turnover": float(df["w"].diff().abs().mean() * 252)}
    base = build(close, irx, MAIN)
    d = DIVIDEND / 252
    div = base.assign(ex=base["ex"] + d, gate=base["gate"] + base["w"] * d)
    out["dividend_3_5pct"] = {**boot(div), "sharpe_managed": sharpe(div["gate"].to_numpy()), "sharpe_hold": sharpe(div["ex"].to_numpy())}
    return out


def main() -> None:
    check_frozen()
    D = pickle.loads((OUT / "data.pkl").read_bytes())
    V = pickle.loads(VIX_DATA.read_bytes())
    sp, nq, irx = D["GSPC"].astype(float), D["IXIC"].astype(float), D["IRX"].astype(float)
    df, rp = build(sp, irx, MAIN), build(nq, irx, REPL)

    res = {"main": report(df), "replication": report(rp), "G0": {"GSPC": g0(sp, irx), "IXIC": g0(nq, irx)}}
    res["G0"]["pass"] = res["G0"]["GSPC"]["pass"] and res["G0"]["IXIC"]["pass"]
    g1 = boot(df)
    g1["pass"] = bool(g1["est"] > 0 and g1["z"] >= 3.0)
    res["G1"] = g1
    g2 = boot(rp)
    g2["pass"] = bool(g2["est"] > 0 and g2["z"] >= 2.0)
    res["G2"] = g2
    a, b = d_of(df[df.index < SPLIT]), d_of(df[df.index >= SPLIT])
    res["G3"] = {"d_1960_1976": a, "d_1977_1993": b, "pass": bool(a > 0 and b > 0)}
    ep = {k: d_of(df[(df.index < s) | (df.index > e)]) for k, (s, e) in EPISODES.items()}
    res["G4"] = {"excl": ep, "pass": all(v > 0 for v in ep.values())}
    d5 = d_of(build(sp, irx, MAIN, cost=COST_HIGH))
    res["G5"] = {"d_20bp": d5, "pass": bool(d5 > 0)}

    if all(res[k]["pass"] for k in ("G0", "G1", "G2", "G3", "G4", "G5")):
        res["verdict"] = "支持"
    elif g1["z"] <= -3.0:
        res["verdict"] = "方向证伪"
    elif -0.15 < g1["ci95"][0] and g1["ci95"][1] < 0.15:
        res["verdict"] = "测得准的零效应"
    else:
        res["verdict"] = "弃权"

    res["report_only"] = {"QQQ": seen_window("QQQ", V), "SPY": seen_window("SPY", V), "robustness": robustness(sp, irx),
                          "alpha": {"GSPC": alpha(df), "IXIC": alpha(rp)}}
    (OUT / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print("results written", res["verdict"])


if __name__ == "__main__":
    main()
