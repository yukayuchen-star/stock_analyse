"""VIX 门检验 v1：战术四档敞口（V1）· 核心 VIX 加速（V2）（口径见仓库根 `PREREG-vix-gate-v1.md`）。

数据与依赖代码冻结，sha 不符即拒绝运行。数值结果落 `output/prereg_vix_gate_v1/results.json`，
RESULTS.md 据此撰写。用法：`python -m backtest.prereg_vix_gate_v1`
"""
from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from signals.macro.regime import classify_vix

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "prereg_vix_gate_v1"

FROZEN = {
    "PREREG-vix-gate-v1.md": "190fe2e4a3cda47e77fc60bd63598365be650a78ce8b64df272a46bfe11c57c5",
    "output/prereg_vix_gate_v1/data.pkl": "7f6258930c983f0b3580dc93cf847f37ce7cae0a289f4bab37ba821752a1032c",
    "signals/macro/regime.py": "b0c7c1c5feb3d07c72efc4a66b712ff6ce11ae240b06245942d7742abfee3d93",
}
SEED, BOOT_N, PLACEBO_N = 20261101, 10_000, 1_000
COST, COST_HIGH = 0.0005, 0.0020
SPLIT = pd.Timestamp("2013-01-01")
EPISODES = {"2000-03~2002-12": ("2000-03-01", "2002-12-31"),
            "2007-10~2009-06": ("2007-10-01", "2009-06-30"),
            "2020-02~2020-06": ("2020-02-01", "2020-06-30")}
TRIGGER = 25.0
ANN = np.sqrt(252)


def check_frozen() -> None:
    for rel, sha in FROZEN.items():
        got = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if got != sha:
            raise SystemExit(f"冻结输入不符：{rel} {got[:12]}… ≠ {sha[:12]}…")


# ── 公共 ────────────────────────────────────────────────────

def gate_exposure(vix: pd.Series, idx: pd.DatetimeIndex) -> pd.Series:
    """t 日 VIX 定 E_t；缺失沿用上一个值。"""
    v = vix.reindex(idx.union(vix.index)).ffill().reindex(idx)
    return v.map(lambda x: classify_vix(float(x)).position_limit if pd.notna(x) else np.nan)


def excess_series(close: pd.Series, E: pd.Series, rf: pd.Series, cost: float) -> pd.DataFrame:
    """E_t 在 t+1 收盘成交，赚 t+1→t+2：d 日持仓 w_d = E_{d−2}，换仓成本记在成交收盘之后的 d 日。"""
    r = close.pct_change()
    w = E.shift(2)
    turn = w.diff().abs().fillna(0.0)
    ex = r - rf
    df = pd.DataFrame({"r": r, "rf": rf, "w": w, "ex": ex, "gate": w * ex - cost * turn}).dropna()
    return df


def sharpe(x: np.ndarray) -> float:
    s = x.std(ddof=1)
    return float(x.mean() / s * ANN) if s > 0 else float("nan")


def delta_sharpe(g: np.ndarray, base: np.ndarray) -> float:
    return sharpe(g) - sharpe(base)


def month_block_boot(df: pd.DataFrame, a: str, b: str, seed=SEED, n=BOOT_N) -> np.ndarray:
    """按月分组、12 个月连续成块的循环 bootstrap。"""
    rng = np.random.default_rng(seed)
    groups = [g for _, g in df.groupby(df.index.to_period("M"))]
    A = [g[a].to_numpy() for g in groups]
    B = [g[b].to_numpy() for g in groups]
    m = len(groups)
    out = np.empty(n)
    n_blocks = int(np.ceil(m / 12))
    for i in range(n):
        starts = rng.integers(0, m, n_blocks)
        pick = np.concatenate([(s + np.arange(12)) % m for s in starts])[:m]
        out[i] = delta_sharpe(np.concatenate([A[j] for j in pick]), np.concatenate([B[j] for j in pick]))
    return out


def summarize(est: float, boots: np.ndarray) -> dict:
    se = float(np.nanstd(boots, ddof=1))
    return {"est": est, "se": se, "z": est / se if se > 0 else float("nan"),
            "ci95": [float(np.nanpercentile(boots, 2.5)), float(np.nanpercentile(boots, 97.5))]}


def perf(ret: pd.Series) -> dict:
    eq = (1 + ret).cumprod()
    dd = eq / eq.cummax() - 1
    under = (dd < 0).astype(int)
    longest = int(under.groupby((under == 0).cumsum()).sum().max())
    yrs = len(ret) / 252
    return {"cagr": float(eq.iloc[-1] ** (1 / yrs) - 1), "vol": float(ret.std(ddof=1) * ANN),
            "max_dd": float(dd.min()), "longest_dd_days": longest}


def compare(df: pd.DataFrame, col: str, **kw) -> dict:
    est = delta_sharpe(df[col].to_numpy(), df["ex"].to_numpy())
    return summarize(est, month_block_boot(df, col, "ex", **kw))


# ── V1 ──────────────────────────────────────────────────────

def load(asset: str, D: dict) -> tuple[pd.Series, pd.Series, pd.Series]:
    close = D[asset]["Close"].astype(float)
    idx = close.index
    rf = (D["IRX"].reindex(idx.union(D["IRX"].index)).ffill().reindex(idx) / 100 / 252).shift(1)
    return close, D["VIXCLS"], rf


def v1_g0(D: dict) -> dict:
    close, vix, _ = load("QQQ", D)
    full = gate_exposure(vix, close.index)
    rng = np.random.default_rng(SEED)
    picks = rng.choice(np.arange(300, len(close)), size=30, replace=False)
    worst = 0.0
    for p in picks:
        T = close.index[p]
        cut = gate_exposure(vix.loc[:T], close.index[close.index <= T])
        worst = max(worst, float((cut - full.reindex(cut.index)).abs().max()))
    df = excess_series(close, full, load("QQQ", D)[2], COST)
    lag_ok = bool((df["w"] == full.shift(2).reindex(df.index)).all())
    return {"n": 30, "max_abs_diff": worst, "lag_two_bars": lag_ok, "pass": worst == 0.0 and lag_ok}


def run_v1(D: dict) -> dict:
    close, vix, rf = load("QQQ", D)
    E = gate_exposure(vix, close.index)
    df = excess_series(close, E, rf, COST)
    res = {"n_days": len(df), "window": [str(df.index[0].date()), str(df.index[-1].date())],
           "mean_exposure": float(df["w"].mean()),
           "exposure_days": df["w"].value_counts().sort_index().to_dict(),
           "annual_turnover": float(df["w"].diff().abs().mean() * 252)}
    res["G0"] = v1_g0(D)

    g1 = compare(df, "gate")
    g1["sharpe_gate"], g1["sharpe_hold"] = sharpe(df["gate"].to_numpy()), sharpe(df["ex"].to_numpy())
    g1["pass"] = bool(g1["est"] > 0 and g1["z"] >= 3.0)
    res["G1"] = g1

    a = df[df.index < SPLIT]
    b = df[df.index >= SPLIT]
    da, db = delta_sharpe(a["gate"].to_numpy(), a["ex"].to_numpy()), delta_sharpe(b["gate"].to_numpy(), b["ex"].to_numpy())
    res["G2"] = {"d_1999_2012": da, "d_2013_2026": db, "pass": bool(da > 0 and db > 0)}

    sc, sv, srf = load("SPY", D)
    sdf = excess_series(sc, gate_exposure(sv, sc.index), srf, COST)
    g3 = compare(sdf, "gate")
    g3.update(n_days=len(sdf), start=str(sdf.index[0].date()), mean_exposure=float(sdf["w"].mean()),
              **{"pass": bool(g3["est"] > 0 and g3["z"] >= 2.0)})
    res["G3"] = g3

    ep = {}
    for name, (s, e) in EPISODES.items():
        keep = df[(df.index < s) | (df.index > e)]
        ep[name] = delta_sharpe(keep["gate"].to_numpy(), keep["ex"].to_numpy())
    res["G4"] = {"excl": ep, "pass": all(v > 0 for v in ep.values())}

    hi = excess_series(close, E, rf, COST_HIGH)
    d5 = delta_sharpe(hi["gate"].to_numpy(), hi["ex"].to_numpy())
    res["G5"] = {"d_20bp": d5, "pass": bool(d5 > 0)}

    if all(res[k]["pass"] for k in ("G0", "G1", "G2", "G3", "G4", "G5")):
        res["verdict"] = "支持"
    elif g1["z"] <= -3.0:
        res["verdict"] = "方向证伪"
    elif -0.15 < g1["ci95"][0] and g1["ci95"][1] < 0.15:
        res["verdict"] = "测得准的零效应"
    else:
        res["verdict"] = "弃权"

    wbar = df["w"].mean()
    res["perf"] = {"gate": perf(df["gate"] + df["rf"]), "constant": perf(wbar * df["ex"] + df["rf"]),
                   "hold": perf(df["r"])}
    res["exploratory"] = v1_exploratory(close, vix, rf, df, E)
    return res


def v1_exploratory(close, vix, rf, df, E) -> dict:
    out = {}
    # 实际绑定版：只保留 25–35 → 40%、≥35 → 0%
    bound = E.where(E < 0.70, 1.0)
    d = excess_series(close, bound, rf, COST)
    out["bound"] = {**compare(d, "gate"), "mean_exposure": float(d["w"].mean()), "perf": perf(d["gate"] + d["rf"])}

    target = df["w"].mean()
    rv = (close.pct_change().rolling(20).var() * 252)
    lo, hi = 1e-4, 10.0
    for _ in range(60):      # 校准 c 使平均敞口与门策略相同
        c = (lo + hi) / 2
        m = (c / rv).clip(upper=1.0).reindex(df.index).mean()
        lo, hi = (c, hi) if m < target else (lo, c)
    d = excess_series(close, (c / rv).clip(upper=1.0), rf, COST)
    out["vol_managed"] = {**compare(d, "gate"), "mean_exposure": float(d["w"].mean()), "c": c, "perf": perf(d["gate"] + d["rf"])}

    trend = (close > close.rolling(200).mean()).astype(float).where(close.rolling(200).mean().notna())
    d = excess_series(close, trend, rf, COST)
    out["trend_200"] = {**compare(d, "gate"), "mean_exposure": float(d["w"].mean()), "perf": perf(d["gate"] + d["rf"])}

    v = vix.reindex(close.index.union(vix.index)).ffill().reindex(close.index)
    t = pd.cut(v, [0, 15, 25, 35, 1e9], right=False, labels=["<15", "15-25", "25-35", ">=35"])
    fw = {}
    for h in (20, 60):
        f = close.shift(-h) / close - 1
        g = f.groupby(t, observed=True)
        fw[f"fwd{h}"] = {k: {"mean": float(s.mean()), "hit": float((s.dropna() > 0).mean()), "n": int(s.notna().sum())} for k, s in g}
    out["forward_by_tier"] = fw
    return out


# ── V2 ──────────────────────────────────────────────────────

class Cohorts:
    """预计算月首日与收益，供不同 VIX 序列复用。"""

    def __init__(self, close: pd.Series, rf: pd.Series, first_month: str):
        self.close = close
        self.P = close.to_numpy()
        self.C = (1 + rf.fillna(0.0)).cumprod().to_numpy()
        self.idx = close.index
        per = self.idx.to_period("M")
        self.months = per.unique()
        self.first = {m: int(np.flatnonzero(per == m)[0]) for m in self.months}
        self.last = {m: int(np.flatnonzero(per == m)[-1]) for m in self.months}
        self.per = per
        start = list(self.months).index(pd.Period(first_month))
        self.starts = [i for i in range(start, len(self.months) - 11)]

    def triggers(self, v: np.ndarray, level: float) -> dict:
        """每月首次 VIX≥level 的成交位置（t+1）。"""
        out = {}
        hits = np.flatnonzero(v >= level)
        for p in hits:
            m = self.per[p]
            if m not in out and p + 1 < len(self.P):
                out[m] = p + 1
        return out

    def _value(self, flows: list, s: int, e: int) -> float:
        P, C = self.P, self.C
        val = 12.0 * C[e] / C[s]
        for ex, amt in flows:
            val += amt * (P[e] / P[ex] - C[e] / C[ex])
        return val

    def run(self, trig: dict) -> pd.DataFrame:
        rows = []
        for i in self.starts:
            ms = self.months[i: i + 12]
            s, e = self.first[ms[0]], self.last[ms[-1]]
            base = [(self.first[m], 1.0) for m in ms]
            ev = sorted(base + [(trig[m], 1.0) for m in ms if m in trig and trig[m] <= e])
            b, used = [], 0
            for ex, amt in ev:
                if used >= 12:
                    break
                b.append((ex, amt))
                used += 1
            va, vb = self._value(base, s, e), self._value(b, s, e)
            rows.append({"start": self.idx[s], "end": self.idx[e], "D": vb / va - 1,
                         "n_extra": sum(1 for m in ms if m in trig and trig[m] <= e)})
        return pd.DataFrame(rows)


def block_mean_boot(x: np.ndarray, seed=SEED, n=BOOT_N, block=12) -> np.ndarray:
    rng = np.random.default_rng(seed)
    m = len(x)
    k = int(np.ceil(m / block))
    starts = rng.integers(0, m, (n, k))
    pos = (starts[:, :, None] + np.arange(block)) % m
    return x[pos.reshape(n, -1)[:, :m]].mean(axis=1)


def vix_on(idx, vix) -> np.ndarray:
    return vix.reindex(idx.union(vix.index)).ffill().reindex(idx).to_numpy()


def run_v2(D: dict) -> dict:
    close, vix, rf = load("QQQ", D)
    co = Cohorts(close, rf, "1999-04")
    v = vix_on(close.index, vix)
    d = co.run(co.triggers(v, TRIGGER))
    x = d["D"].to_numpy()
    res = {"n_cohorts": len(d), "first": str(d["start"].iloc[0].date()), "last_end": str(d["end"].iloc[-1].date()),
           "share_identical": float((np.abs(x) < 1e-12).mean()), "share_positive": float((x > 0).mean()),
           "median_D": float(np.median(x)), "worst_D": float(x.min()), "best_D": float(x.max())}

    # G0：成交晚于信号；截断不变
    trig = co.triggers(v, TRIGGER)
    ok = all(v[p - 1] >= TRIGGER and p - 1 >= 0 for p in trig.values())
    rng = np.random.default_rng(SEED)
    worst = 0
    for T in rng.choice(np.arange(300, len(v)), size=30, replace=False):
        cut = co.triggers(v[: T + 1], TRIGGER)
        worst = max(worst, sum(1 for m, p in cut.items() if p + 1 <= T and trig.get(m) != p))
    res["G0"] = {"signal_before_exec": bool(ok), "truncation_mismatch": worst, "pass": bool(ok and worst == 0)}

    g1 = summarize(float(x.mean()), block_mean_boot(x))
    g1["pass"] = bool(g1["est"] > 0 and g1["z"] >= 3.0)
    res["G1"] = g1

    rngp = np.random.default_rng(SEED + 1)
    pl = np.empty(PLACEBO_N)
    n = len(v)
    for i in range(PLACEBO_N):
        vs = np.roll(v, int(rngp.integers(252, n - 252)))
        pl[i] = co.run(co.triggers(vs, TRIGGER))["D"].mean()
    res["G2"] = {"obs": g1["est"], "placebo_mean": float(pl.mean()), "placebo_p95": float(np.percentile(pl, 95)),
                 "pct_rank": float((pl < g1["est"]).mean()), "pass": bool(g1["est"] > np.percentile(pl, 95))}

    a, b = x[d["start"] < SPLIT], x[d["start"] >= SPLIT]
    res["G3"] = {"mean_1999_2012": float(a.mean()), "mean_2013_2026": float(b.mean()),
                 "n": [len(a), len(b)], "pass": bool(a.mean() > 0 and b.mean() > 0)}

    sc, sv, srf = load("SPY", D)
    cs = Cohorts(sc, srf, "1993-02")
    ds = cs.run(cs.triggers(vix_on(sc.index, sv), TRIGGER))
    xs = ds["D"].to_numpy()
    g4 = summarize(float(xs.mean()), block_mean_boot(xs))
    g4.update(n=len(xs), **{"pass": bool(g4["est"] > 0 and g4["z"] >= 2.0)})
    res["G4"] = g4

    ep = {}
    for name, (s, e) in EPISODES.items():
        keep = d[(d["end"] < s) | (d["start"] > e)]
        ep[name] = {"mean": float(keep["D"].mean()), "n": len(keep)}
    res["G5"] = {"excl": ep, "pass": all(v_["mean"] > 0 for v_ in ep.values())}

    if all(res[k]["pass"] for k in ("G0", "G1", "G2", "G3", "G4", "G5")):
        res["verdict"] = "支持"
    elif g1["z"] <= -3.0:
        res["verdict"] = "方向证伪"
    elif -0.005 < g1["ci95"][0] and g1["ci95"][1] < 0.005:
        res["verdict"] = "测得准的零效应"
    else:
        res["verdict"] = "弃权"

    res["exploratory"] = {f"vix_{lv:g}": (lambda z: {**summarize(float(z.mean()), block_mean_boot(z)),
                                                      "share_identical": float((np.abs(z) < 1e-12).mean())})(
        co.run(co.triggers(v, lv))["D"].to_numpy()) for lv in (30.0, 35.0)}
    return res


def main() -> None:
    check_frozen()
    OUT.mkdir(parents=True, exist_ok=True)
    D = pickle.loads((OUT / "data.pkl").read_bytes())
    res = {"V1": run_v1(D), "V2": run_v2(D)}
    (OUT / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print("results written", {k: v["verdict"] for k, v in res.items()})


if __name__ == "__main__":
    main()
