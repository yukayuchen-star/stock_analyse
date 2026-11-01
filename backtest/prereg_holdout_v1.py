"""2005–2020 留出检验 v1：F4′ 残差动量 · R13 量化分条件化（口径见仓库根 `PREREG-holdout-v1.md`）。

价格与依赖代码全部冻结，sha 不符即拒绝运行。数值结果落 `output/prereg_holdout_v1/results.json`，
RESULTS.md 据此撰写。用法：`python -m backtest.prereg_holdout_v1`
"""
from __future__ import annotations

import hashlib
import json
import pickle
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "prereg_holdout_v1"

FROZEN = {
    "PREREG-holdout-v1.md": "87cc8316723fe48180d9348450e52dc76913049d03c8ca3d26ec2745cd12daa0",
    "output/prereg_holdout_v1/prices.pkl": "45c54c608155fc6d67e42f917ba7dccdbad0ce37c1623d71885b16ede9a9649f",
    "backtest/prereg_factor_v1.py": "959255e58c863385aeab2eca2c2e017f98d05c1589b64f52e7d2041779583b23",
    "backtest/prereg_signal_cond_v1.py": "04816712f2d0e76fbea7b89bdfb7905a93c52cb819953daf6e6f8f36a6c6707d",
    "backtest/factor_lab.py": "12eed0facf12d21e884d1ddb10ff28c108922c7c54742dd3d7d5ef8e87eb5c64",
    "signals/quant/ear.py": "55ef100ca2698c6843feb3dd5fc6a965a81cc9353f5cb549c4bcbf38958d3e21",
    "signals/chan/chan_signal.py": "735acfdd0328a5bda286b3a1ae848dd1f862cab057c943df4a593f34ac7d7db4",
    "signals/chan/stroke.py": "12de029b09653951633acb0fb51bb789cdc7ab74851bbad41b6de87d6158b568",
    "signals/chan/pivot.py": "af914cb8158432e480e2a748413b766431f3cdd2534ae67a1c438e1d52aef1ae",
    "signals/chan/fractal.py": "c8b672570333e5d1d3f7e120769f517236bb1376e5c08eef93c5ee97ed388abe",
    "signals/quant/trend.py": "b99345c0d869ebdee2eda68ce433730e2af02144c2e364d946afffd0be686d29",
    "signals/quant/momentum.py": "cfee7d44755d9e6370e306e27fa2215482b8c5d0a988fdd863d129ac82984507",
    "signals/quant/relative.py": "36f85d8e3f71c4c3c1e98f78c1a260f23ee68f7cbfa6dbf4e2878b054916e7e1",
    "signals/quant/volume.py": "4a24700f45cdc2710ab7e95ac9d8ff907ce7621fcefe017e486fd4daada24a28",
}
SIG_START, SIG_END = pd.Timestamp("2005-01-03"), pd.Timestamp("2020-11-30")
LABEL_END = pd.Timestamp("2020-12-31")
SPLIT = pd.Timestamp("2013-01-01")
MIN_BARS = 200
CASES = ("UNH", "MSFT", "NVDA", "META")


def check_frozen() -> None:
    for rel, sha in FROZEN.items():
        got = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if got != sha:
            raise SystemExit(f"冻结输入不符：{rel} {got[:12]}… ≠ {sha[:12]}…")
    from signals.chan.chan_signal import STROKE_EXTREME_FIRST
    assert STROKE_EXTREME_FIRST is True


def _init(p):
    global _P
    _P = p


# ── H1：F4′ ─────────────────────────────────────────────────

def _f4_fn(bench: str, lookback: int = 252):
    from backtest.prereg_factor_v1 import EST_AMEND1, make_resid_mom
    return make_resid_mom([_P[bench]["Close"].astype(float).pct_change()], lookback, EST_AMEND1)


def _h1_one(tk: str) -> pd.DataFrame:
    from backtest.factor_lab import REFERENCE_FACTORS, build_factor_panel
    fns = {**REFERENCE_FACTORS, "f4": _f4_fn("QQQ"), "f4_126": _f4_fn("QQQ", 126), "f4_spy": _f4_fn("SPY")}
    return build_factor_panel({tk: _P[tk]}, fns)


def h1_cases(p: pd.DataFrame, prices: dict) -> dict:
    pct = p.groupby("date")["f4"].rank(pct=True)
    p = p.assign(_pct=pct)
    fwd = {}
    for b in ("QQQ", "SPY"):
        c = prices[b]["Close"].astype(float)
        fwd[b] = (c.shift(-10) / c - 1).rename("b")
    out = {}
    for tk in CASES:
        s = p[(p["ticker"] == tk) & p["f4"].notna()]
        if s.empty:
            out[tk] = {"n_days": 0}
            continue
        band = pd.cut(s["_pct"], [0, 1 / 3, 2 / 3, 1], labels=["low", "mid", "high"])
        row = {"n_days": len(s), "first": str(s["date"].min().date()),
               "mean_pct": float(s["_pct"].mean()), "share_q5": float((s["_pct"] > 0.8).mean()),
               "share_q1": float((s["_pct"] <= 0.2).mean()),
               "ts_spearman_f4_fwd10": float(s[["f4", "fwd10"]].corr(method="spearman").iloc[0, 1])}
        for b, f in fwd.items():
            ex = s["fwd10"].to_numpy() - f.reindex(s["date"]).to_numpy()
            row[f"fwd10_ex_{b}"] = {k: float(np.nanmean(ex[band == k])) for k in ("low", "mid", "high")}
        out[tk] = row
    return out


def run_h1(prices: dict) -> dict:
    from backtest.factor_lab import (MIN_NAMES_PER_DATE, REFERENCE_FACTORS, assert_asof_consistent,
                                     daily_ic, evaluate_factor, ir_stats)
    from backtest.prereg_factor_v1 import finite_asof_check, ic_block, long_short
    from signals.quant.ear import residual_ic
    uni = [t for t in _universe() if t in prices]
    cache = OUT / "h1_panel.pkl"
    if cache.exists():
        p = pd.read_pickle(cache)
    else:
        with Pool(10, initializer=_init, initargs=(prices,)) as pool:
            frames = pool.map(_h1_one, uni)
        p = pd.concat(frames, ignore_index=True)
        p = p[(p["date"] >= SIG_START) & (p["date"] <= SIG_END)].sort_values(["date", "ticker"]).reset_index(drop=True)
        p.to_pickle(cache)
    refs = list(REFERENCE_FACTORS)

    _init(prices)
    f4 = _f4_fn("QQQ")
    frames = {t: prices[t] for t in uni}
    g0_lab = assert_asof_consistent(frames, f4, n_samples=120, seed=7)
    g0_fin = finite_asof_check(frames, lambda _: f4)
    g0 = {"factor_lab_points": g0_lab, **g0_fin, "pass": g0_fin["max_abs_diff"] == 0.0}

    ev = evaluate_factor(p, "f4", refs)
    st = ev["ic_stats"]
    g1 = {"ic": st, "checks": ev["checks"], "lab_verdict": ev["verdict"],
          "q_means": [float(x) for x in ev["quantile"]["q_means"]], "mono_rho": ev["quantile"]["mono_rho"],
          "year_sign": ev["year_sign"], "by_year": {int(k): float(v) for k, v in ev["by_year"]["mean"].items()},
          "max_corr": ev["max_corr"], "max_corr_with": ev["max_corr_with"],
          "pass": bool(st["ic_mean"] > 0 and st["t_stat"] >= 3.0 and ev["verdict"] == "PASS")}

    ric = ir_stats(residual_ic(p, "f4", refs, "fwd10", MIN_NAMES_PER_DATE), 10)
    g2 = {"resid_ic": ric, "pass": bool(ric["t_stat"] >= 2.0 and ric["ic_mean"] > 0)}

    close = pd.DataFrame({t: prices[t]["Close"].astype(float) for t in uni}).sort_index()
    g3 = long_short(p, "f4", close)
    g3["pass"] = bool(g3["net_sharpe_ci95"][0] > 0)

    ic = daily_ic(p, "f4", "fwd10")
    a, b = float(ic[ic.index < SPLIT].mean()), float(ic[ic.index >= SPLIT].mean())
    g4 = {"ic_2005_2012": a, "ic_2013_2020": b, "pass": bool(a > 0 and b > 0)}

    fe = p.assign(fwd10_fe=p["fwd10"] - p.groupby("ticker")["fwd10"].transform("mean"))
    s5 = ir_stats(daily_ic(fe, "f4", "fwd10_fe"), 10)
    g5 = {"ic": s5, "pass": bool(s5["ic_mean"] > 0 and s5["t_stat"] >= 2.0)}

    se = st["ic_std"] / np.sqrt(max(st["n_days"] / 10, 1.0))
    ci = [st["ic_mean"] - 1.96 * se, st["ic_mean"] + 1.96 * se]
    if all(g["pass"] for g in (g0, g1, g2, g3, g4, g5)):
        verdict = "留出支持"
    elif st["t_stat"] <= -3.0:
        verdict = "方向证伪"
    elif -0.02 < ci[0] and ci[1] < 0.02:
        verdict = "测得准的零效应"
    else:
        verdict = "弃权"

    crisis = p[(p["date"] >= "2008-01-01") & (p["date"] <= "2009-12-31")]
    ex = {"fwd5": ic_block(p, "f4", 5), "fwd20": ic_block(p, "f4", 20), "L126": ic_block(p, "f4_126"),
          "spy_fwd10": ic_block(p, "f4_spy"), "crisis_2008_09": ic_block(crisis, "f4"),
          "coverage": float(p["f4"].notna().mean())}
    return {"rows": len(p), "n_tickers": int(p["ticker"].nunique()),
            "window": [str(p["date"].min().date()), str(p["date"].max().date())],
            "G0": g0, "G1": g1, "G2": g2, "G3": g3, "G4": g4, "G5": g5, "ic_ci95": ci, "verdict": verdict,
            "exploratory": ex, "cases": h1_cases(p, prices)}


# ── H2：R13 ─────────────────────────────────────────────────

def _universe() -> list:
    from backtest.factor_lab import VALIDATION_UNIVERSE
    return list(VALIDATION_UNIVERSE)


def _peers(tk: str, prices: dict) -> list:
    from backtest.prereg_factor_v1 import TICKER_ETF
    return [t for t in _universe() if TICKER_ETF[t] == TICKER_ETF[tk] and t in prices]


def _events_one(tk: str):
    from signals.chan.chan_signal import extract_chan_events
    return tk, extract_chan_events(_P[tk])


def _score_one(args):
    from backtest.prereg_signal_cond_v1 import q_score
    tk, pos, peers = args
    try:
        return q_score(tk, pos, _P, peers)
    except Exception as exc:          # 剔除并报告，不当中性 0
        return {"error": repr(exc)}


def h2_panel(prices: dict) -> pd.DataFrame:
    from backtest.prereg_factor_v1 import EST_AMEND1, make_resid_mom
    cache = OUT / "h2_panel.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    U = [t for t in _universe() if t in prices]
    with Pool(10, initializer=_init, initargs=(prices,)) as pool:
        events = dict(pool.map(_events_one, U))
    qqq, spy = (prices[b]["Close"].astype(float) for b in ("QQQ", "SPY"))
    qret = qqq.pct_change()
    rows, jobs = [], []
    for tk in U:
        df = prices[tk]
        idx = df.index
        c = df["Close"].astype(float)
        peers = _peers(tk, prices)
        f4 = make_resid_mom([qret], 252, EST_AMEND1)(df)

        def ex(pos, h, b):
            if pos + 1 + h >= len(idx):
                return np.nan
            s, e = idx[pos + 1], idx[pos + 1 + h]
            return (c[e] / c[s] - 1) - (b[e] / b[s] - 1)

        for e in events[tk]:
            if not e.signal_type.startswith("b") or not (SIG_START <= e.date <= SIG_END):
                continue
            pos = idx.get_loc(e.date)
            sub = df.iloc[: pos + 1]
            r = sub["Close"].astype(float).pct_change().tail(60)
            m = qret.reindex(r.index)
            ok = r.notna() & m.notna()
            y60 = ex(pos, 60, qqq)
            if pos + 61 < len(idx) and idx[pos + 61] > LABEL_END:
                y60 = np.nan
            rows.append({"ticker": tk, "date": e.date, "type": e.signal_type, "bars": pos + 1,
                         "y10": ex(pos, 10, qqq), "y20": ex(pos, 20, qqq), "y60": y60, "y20_spy": ex(pos, 20, spy),
                         "atr_pct20": float(((sub["High"] - sub["Low"]) / sub["Close"]).tail(20).mean()),
                         "beta60": float(np.polyfit(m[ok], r[ok], 1)[0]) if ok.sum() >= 40 else np.nan,
                         "f4p": float(f4.iloc[pos]), "last_label_date": idx[pos + 21] if pos + 21 < len(idx) else pd.NaT})
            jobs.append((tk, pos, peers))
    with Pool(10, initializer=_init, initargs=(prices,)) as pool:
        scores = pool.map(_score_one, jobs, chunksize=20)
    panel = pd.DataFrame([{**r, **s} for r, s in zip(rows, scores)])
    panel.to_pickle(cache)
    return panel


def h2_g0(d: pd.DataFrame, prices: dict) -> dict:
    from backtest.prereg_signal_cond_v1 import SEED, q_score
    rng = np.random.default_rng(SEED)
    pick = d.iloc[rng.choice(len(d), size=min(30, len(d)), replace=False)]
    maxdiff = 0.0
    for _, r in pick.iterrows():
        cut = {k: v.loc[: r["date"]] for k, v in prices.items()}
        q2 = q_score(r["ticker"], len(cut[r["ticker"]]) - 1, cut, _peers(r["ticker"], prices))["Q"]
        maxdiff = max(maxdiff, abs(q2 - r["Q"]))
    lab_ok = bool(d["last_label_date"].notna().all() and (d["last_label_date"] <= LABEL_END).all())
    return {"n": len(pick), "max_abs_diff": maxdiff, "labels_within_prices": lab_ok,
            "max_label_date": str(d["last_label_date"].max().date()), "pass": maxdiff == 0.0 and lab_ok}


def h2_cases(d: pd.DataFrame) -> dict:
    from backtest.prereg_signal_cond_v1 import spearman
    out = {}
    for tk in CASES:
        s = d[d["ticker"] == tk]
        row = {"n": len(s), "by_type": s["type"].value_counts().to_dict(),
               "rho_Q_y20": spearman(s["Q"], s["y20"]), "rho_Q_y20_spy": spearman(s["Q"], s["y20_spy"]),
               "mean_y20": float(s["y20"].mean()), "mean_y20_spy": float(s["y20_spy"].mean())}
        band = pd.qcut(s["Q"].rank(method="first"), 3, labels=["low", "mid", "high"])
        row["tercile"] = s.groupby(band, observed=True)[["y20", "y20_spy"]].mean().round(5).to_dict()
        out[tk] = row
    return out


def run_h2(prices: dict) -> dict:
    from backtest.prereg_signal_cond_v1 import month_boot, partial_spearman, spearman, summarize, tercile_spread
    panel = h2_panel(prices)
    bad = panel[panel["error"].notna()] if "error" in panel else panel.iloc[:0]
    short = panel[panel["bars"] < MIN_BARS]
    d = panel[(panel["bars"] >= MIN_BARS) & panel["Q"].notna() & panel["y20"].notna()].reset_index(drop=True)
    d["month"] = d["date"].dt.to_period("M")
    d["year"] = d["date"].dt.year
    res = {"n_raw": len(panel), "n_used": len(d), "n_error": len(bad), "n_short_history": len(short),
           "errors_sample": bad["error"].astype(str).str[:100].value_counts().head(5).to_dict() if len(bad) else {},
           "dropped_names": sorted(set(bad["ticker"]) | set(short["ticker"])),
           "by_type": d["type"].value_counts().to_dict(), "n_months": int(d["month"].nunique())}
    res["G0"] = h2_g0(d, prices)

    rank_q = lambda x: spearman(x["Q"], x["y20"])
    rho = rank_q(d)
    res["G1"] = summarize(rho, month_boot(d, rank_q))
    res["G1"]["pass"] = bool(rho > 0 and res["G1"]["z"] >= 3.0)

    yr = {int(y): spearman(g["Q"], g["y20"]) for y, g in d.groupby("year")}
    same = float(np.mean([np.sign(v) == np.sign(rho) for v in yr.values()]))
    early, late = rank_q(d[d["date"] < SPLIT]), rank_q(d[d["date"] >= SPLIT])
    res["G2"] = {"by_year": yr, "same_sign_frac": same, "rho_2005_2012": early, "rho_2013_2020": late,
                 "pass": bool(same >= 0.60 and early > 0 and late > 0)}

    g3a = summarize(tercile_spread(d), month_boot(d, tercile_spread))
    loo = {tk: rank_q(d[d["ticker"] != tk]) for tk in d["ticker"].unique()}
    top = min(loo, key=loo.get)
    dd = d[d["ticker"] != top].reset_index(drop=True)
    g3b = summarize(loo[top], month_boot(dd, rank_q))
    res["G3"] = {"tercile_spread": g3a, "drop_top_contributor": top, "drop_top": g3b,
                 "pass": bool(g3a["ci95"][0] > 0 and g3b["est"] > 0 and g3b["z"] >= 2.0)}

    dc = d.dropna(subset=["atr_pct20", "beta60"]).reset_index(drop=True)
    pf = lambda x: partial_spearman(x["Q"].to_numpy(), x["y20"].to_numpy(), x[["atr_pct20", "beta60"]].to_numpy())
    res["G4"] = summarize(pf(dc), month_boot(dc, pf))
    res["G4"].update(n=len(dc), **{"pass": bool(res["G4"]["est"] > 0 and res["G4"]["z"] >= 2.0)})

    fe = d.assign(y20_fe=d["y20"] - d.groupby("ticker")["y20"].transform("mean"))
    rank_fe = lambda x: spearman(x["Q"], x["y20_fe"])
    res["G5"] = summarize(rank_fe(fe), month_boot(fe, rank_fe))
    res["G5"]["pass"] = bool(res["G5"]["est"] > 0 and res["G5"]["z"] >= 2.0)

    if all(res[k]["pass"] for k in ("G0", "G1", "G2", "G3", "G4", "G5")):
        res["verdict"] = "留出支持"
    elif res["G1"]["z"] <= -3.0:
        res["verdict"] = "方向证伪"
    elif -0.05 < res["G1"]["ci95"][0] and res["G1"]["ci95"][1] < 0.05:
        res["verdict"] = "测得准的零效应"
    else:
        res["verdict"] = "弃权"

    ex = {}
    for col in ("trend", "mom", "rel", "vol", "f4p", "atr_pct20", "beta60"):
        s = d[[col, "y20"]].dropna()
        ex[f"rho_{col}"] = {"rho": spearman(s[col], s["y20"]), "n": len(s)}
    for typ, g in d.groupby("type"):
        ex[f"rho_Q_{typ}"] = {"rho": spearman(g["Q"], g["y20"]), "n": len(g)}
    for h in ("y10", "y60", "y20_spy"):
        s = d[["Q", h]].dropna()
        ex[f"rho_Q_{h}"] = {"rho": spearman(s["Q"], s[h]), "n": len(s)}
    ex["tercile_mean_y20"] = d.groupby(pd.qcut(d["Q"].rank(method="first"), 3, labels=["low", "mid", "high"]),
                                       observed=True)[["y20", "y20_spy"]].mean().round(5).to_dict()
    res["exploratory"] = ex
    res["cases"] = h2_cases(d)
    return res


def main() -> None:
    check_frozen()
    prices = pickle.loads((OUT / "prices.pkl").read_bytes())
    res = {"H2": run_h2(prices), "H1": run_h1(prices)}
    (OUT / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print("results written", {k: v["verdict"] for k, v in res.items()})


if __name__ == "__main__":
    main()
