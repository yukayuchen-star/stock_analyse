"""R13 信号条件化 v1：价格量化分 Q 能否在缠论买点之间做选择（口径见仓库根 `PREREG-signal-conditional-v1.md`）。

价格 / 事件提取 / 量化子分全部冻结，sha 不符即拒绝运行。数值结果落 `output/prereg_signal_cond_v1/results.json`，
RESULTS.md 据此撰写。用法：`python -m backtest.prereg_signal_cond_v1`
"""
from __future__ import annotations

import hashlib
import json
import pickle
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import rankdata

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "prereg_signal_cond_v1"
PRICES = ROOT / "output" / "prereg_factor_v1" / "prices.pkl"

FROZEN = {
    "PREREG-signal-conditional-v1.md": "9dfbacc72e8475c1c8bc226ba61b8e1a90aed15e33ab7daa5a188d76c07c3210",
    "output/prereg_factor_v1/prices.pkl": "6e662ec5660aace1236a6aa1049f0b2ed226606e6cc9072698932e2f8910c985",
    "signals/chan/chan_signal.py": "735acfdd0328a5bda286b3a1ae848dd1f862cab057c943df4a593f34ac7d7db4",
    "signals/chan/stroke.py": "12de029b09653951633acb0fb51bb789cdc7ab74851bbad41b6de87d6158b568",
    "signals/chan/pivot.py": "af914cb8158432e480e2a748413b766431f3cdd2534ae67a1c438e1d52aef1ae",
    "signals/chan/fractal.py": "c8b672570333e5d1d3f7e120769f517236bb1376e5c08eef93c5ee97ed388abe",
    "signals/quant/trend.py": "b99345c0d869ebdee2eda68ce433730e2af02144c2e364d946afffd0be686d29",
    "signals/quant/momentum.py": "cfee7d44755d9e6370e306e27fa2215482b8c5d0a988fdd863d129ac82984507",
    "signals/quant/relative.py": "36f85d8e3f71c4c3c1e98f78c1a260f23ee68f7cbfa6dbf4e2878b054916e7e1",
    "signals/quant/volume.py": "4a24700f45cdc2710ab7e95ac9d8ff907ce7621fcefe017e486fd4daada24a28",
}
EV_START, EV_END = "2022-01-03", "2026-09-25"
W = {"trend": 0.25, "mom": 0.30, "rel": 0.20, "vol": 0.10}
MIN_BARS = 200
SEED, BOOT_N = 20261029, 10_000
G1_Z, G3_Z, G4_Z, YEAR_SIGN_MIN = 3.0, 2.0, 2.0, 0.60


def check_frozen() -> None:
    for rel, sha in FROZEN.items():
        got = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if got != sha:
            raise SystemExit(f"冻结输入不符：{rel} {got[:12]}… ≠ {sha[:12]}…")
    from signals.chan.chan_signal import STROKE_EXTREME_FIRST
    assert STROKE_EXTREME_FIRST is True


# ── 事件与打分 ───────────────────────────────────────────────

def _events(args):
    tk, df = args
    from signals.chan.chan_signal import extract_chan_events
    return tk, extract_chan_events(df)


def q_score(tk: str, pos: int, prices: dict, peers: list) -> dict:
    """as-of：所有输入截到事件日（含）。"""
    from signals.quant.momentum import compute_momentum_score
    from signals.quant.relative import compute_relative_strength_score
    from signals.quant.trend import compute_trend_score
    from signals.quant.volume import compute_volume_score
    df = prices[tk].iloc[: pos + 1]
    d = df.index[-1]
    sub = {k: prices[k].loc[:d] for k in {*peers, "QQQ", "SPY", tk}}
    s = {"trend": compute_trend_score(df)[0], "mom": compute_momentum_score(df)[0],
         "rel": compute_relative_strength_score(tk, sub, peers)[0], "vol": compute_volume_score(df)[0]}
    s["Q"] = sum(W[k] * s[k] for k in W) / sum(W.values())
    return s


def _score_one(args):
    tk, pos, peers = args
    try:
        return q_score(tk, pos, _P, peers)
    except Exception as exc:          # 剔除并报告，不当中性 0
        return {"error": repr(exc)}


def _init(p):
    global _P
    _P = p


# ── 统计 ────────────────────────────────────────────────────

def _rank(a):
    return rankdata(a)


def spearman(x, y) -> float:
    return float(np.corrcoef(_rank(x), _rank(y))[0, 1])


def partial_spearman(x, y, Z) -> float:
    rz = np.column_stack([np.ones(len(x))] + [_rank(z) for z in Z.T])
    def resid(v):
        rv = _rank(v)
        return rv - rz @ np.linalg.lstsq(rz, rv, rcond=None)[0]
    return float(np.corrcoef(resid(x), resid(y))[0, 1])


def month_boot(df: pd.DataFrame, stat, n=BOOT_N, seed=SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    groups = [g.index.to_numpy() for _, g in df.groupby("month")]
    out = np.empty(n)
    for i in range(n):
        pick = rng.integers(0, len(groups), len(groups))
        idx = np.concatenate([groups[j] for j in pick])
        out[i] = stat(df.loc[idx])
    return out


def summarize(point: float, boots: np.ndarray) -> dict:
    se = float(np.nanstd(boots, ddof=1))
    return {"est": point, "se": se, "z": point / se if se > 0 else float("nan"),
            "ci95": [float(np.nanpercentile(boots, 2.5)), float(np.nanpercentile(boots, 97.5))]}


def tercile_spread(d: pd.DataFrame) -> float:
    q = d["Q"].rank(pct=True)
    return float(d.loc[q > 2 / 3, "y20"].mean() - d.loc[q <= 1 / 3, "y20"].mean())


# ── 主流程 ──────────────────────────────────────────────────

def build_panel(prices: dict) -> tuple[pd.DataFrame, dict]:
    from backtest.factor_lab import VALIDATION_UNIVERSE
    from backtest.prereg_factor_v1 import TICKER_ETF, load_pinned_earnings, make_resid_mom
    U = list(VALIDATION_UNIVERSE)
    with Pool(12) as pool:
        events = dict(pool.map(_events, [(t, prices[t]) for t in U]))

    qqq = prices["QQQ"]["Close"].astype(float)
    qret = qqq.pct_change()
    earn = load_pinned_earnings(U)
    rows, jobs = [], []
    for tk in U:
        df = prices[tk]
        peers = [t for t in U if TICKER_ETF[t] == TICKER_ETF[tk]]
        f4 = make_resid_mom([qret], 252, 756)(df)
        idx = df.index
        for e in events[tk]:
            if not e.signal_type.startswith("b") or not (EV_START <= str(e.date.date()) <= EV_END):
                continue
            pos = idx.get_loc(e.date)
            c = df["Close"].astype(float)
            # 标签：t+1 收盘入场
            def ex(h):
                if pos + 1 + h >= len(idx):
                    return np.nan
                a, b = idx[pos + 1], idx[pos + 1 + h]
                return (c[b] / c[a] - 1) - (qqq[b] / qqq[a] - 1)
            sub = df.iloc[: pos + 1]
            rng_ = ((sub["High"] - sub["Low"]) / sub["Close"]).tail(20).mean()
            r = sub["Close"].astype(float).pct_change().tail(60)
            m = qret.reindex(r.index)
            ok = r.notna() & m.notna()
            beta = float(np.polyfit(m[ok], r[ok], 1)[0]) if ok.sum() >= 40 else np.nan
            nxt = sorted(d for d in earn.get(tk, []) if d > str(e.date.date()))
            dte = int(((idx > e.date) & (idx <= nxt[0])).sum()) if nxt and nxt[0] <= str(idx[-1].date()) else np.nan
            rows.append({"ticker": tk, "date": e.date, "type": e.signal_type, "price": e.price,
                         "pos": pos, "bars": pos + 1,
                         "y10": ex(10), "y20": ex(20), "y60": ex(60),
                         "atr_pct20": float(rng_), "beta60": beta, "f4p": float(f4.iloc[pos]),
                         "r_ratio": (e.price - e.stop_loss) / e.price if e.stop_loss else np.nan,
                         "dte": dte, "last_label_pos": pos + 21})
            jobs.append((tk, pos, peers))

    with Pool(12, initializer=_init, initargs=(prices,)) as pool:
        scores = pool.map(_score_one, jobs)
    panel = pd.DataFrame([{**r, **s} for r, s in zip(rows, scores)])
    return panel, events


def g0(panel: pd.DataFrame, prices: dict) -> dict:
    """独立截断（按日期 .loc 而非按位置）重算 30 个事件的 Q；且所有主标签不越过钉死价格末日。"""
    from backtest.factor_lab import VALIDATION_UNIVERSE
    from backtest.prereg_factor_v1 import TICKER_ETF
    rng = np.random.default_rng(SEED)
    ok = panel[panel["Q"].notna()]
    pick = ok.iloc[rng.choice(len(ok), size=min(30, len(ok)), replace=False)]
    maxdiff = 0.0
    for _, r in pick.iterrows():
        cut = {k: v.loc[: r["date"]] for k, v in prices.items()}
        peers = [t for t in VALIDATION_UNIVERSE if TICKER_ETF[t] == TICKER_ETF[r["ticker"]]]
        q2 = q_score(r["ticker"], len(cut[r["ticker"]]) - 1, cut, peers)["Q"]
        maxdiff = max(maxdiff, abs(q2 - r["Q"]))
    lab_ok = bool((panel["last_label_pos"] < panel["ticker"].map(lambda t: len(prices[t]))).all())
    return {"n": len(pick), "max_abs_diff": maxdiff, "labels_within_prices": lab_ok,
            "pass": maxdiff == 0.0 and lab_ok}


def main() -> None:
    check_frozen()
    OUT.mkdir(parents=True, exist_ok=True)
    prices = pickle.loads(PRICES.read_bytes())
    panel, events = build_panel(prices)
    panel.to_pickle(OUT / "panel.pkl")

    bad = panel[panel.get("error").notna()] if "error" in panel else panel.iloc[:0]
    short = panel[panel["bars"] < MIN_BARS]
    d = panel[(panel["bars"] >= MIN_BARS) & panel["Q"].notna() & panel["y20"].notna()].reset_index(drop=True)
    d["month"] = d["date"].dt.to_period("M")
    d["year"] = d["date"].dt.year
    res = {"n_raw": len(panel), "n_used": len(d), "n_error": len(bad),
           "n_short_history": len(short), "dropped_names": sorted(set(bad["ticker"]) | set(short["ticker"])),
           "by_type": d["type"].value_counts().to_dict(), "n_months": int(d["month"].nunique())}

    res["G0"] = g0(d, prices)

    # G1 主效应
    rho = spearman(d["Q"], d["y20"])
    res["G1"] = summarize(rho, month_boot(d, lambda x: spearman(x["Q"], x["y20"])))
    res["G1"]["pass"] = rho > 0 and res["G1"]["z"] >= G1_Z

    # G2 逐年同号
    yr = {int(y): spearman(g["Q"], g["y20"]) for y, g in d.groupby("year")}
    same = np.mean([np.sign(v) == np.sign(rho) for v in yr.values()])
    res["G2"] = {"by_year": yr, "same_sign_frac": float(same), "pass": bool(same >= YEAR_SIGN_MIN)}

    # G3 经济量 + 去掉最大贡献票
    ts = tercile_spread(d)
    g3a = summarize(ts, month_boot(d, tercile_spread))
    loo = {tk: spearman(g["Q"], g["y20"]) for tk, g in
           ((tk, d[d["ticker"] != tk]) for tk in d["ticker"].unique())}
    top = min(loo, key=loo.get)
    dd = d[d["ticker"] != top].reset_index(drop=True)
    g3b = summarize(loo[top], month_boot(dd, lambda x: spearman(x["Q"], x["y20"])))
    res["G3"] = {"tercile_spread": g3a, "drop_top_contributor": top, "drop_top": g3b,
                 "pass": g3a["ci95"][0] > 0 and g3b["est"] > 0 and g3b["z"] >= G3_Z}

    # G4 偏相关：控制 atr_pct20 与 beta60
    dc = d.dropna(subset=["atr_pct20", "beta60"]).reset_index(drop=True)
    pf = lambda x: partial_spearman(x["Q"].to_numpy(), x["y20"].to_numpy(), x[["atr_pct20", "beta60"]].to_numpy())
    g4 = summarize(pf(dc), month_boot(dc, pf))
    g4["n"] = len(dc)
    g4["pass"] = g4["est"] > 0 and g4["z"] >= G4_Z
    res["G4"] = g4

    # 裁决（预注册 §5，写死）
    g = res
    if g["G0"]["pass"] and all(g[k]["pass"] for k in ("G1", "G2", "G3", "G4")):
        verdict = "PASS"
    elif g["G1"]["z"] <= -G1_Z:
        verdict = "反向证据"
    elif -0.05 < g["G1"]["ci95"][0] and g["G1"]["ci95"][1] < 0.05:
        verdict = "测得准的零效应"
    else:
        verdict = "弃权"
    res["verdict"] = verdict

    # 探索性（不裁决）
    ex = {}
    for col in ("trend", "mom", "rel", "vol", "f4p", "atr_pct20", "beta60", "r_ratio", "dte"):
        s = d[[col, "y20"]].dropna()
        ex[f"rho_{col}"] = {"rho": spearman(s[col], s["y20"]), "n": len(s)}
    for typ, gq in d.groupby("type"):
        ex[f"rho_Q_{typ}"] = {"rho": spearman(gq["Q"], gq["y20"]), "n": len(gq)}
    for h in ("y10", "y60"):
        s = d[["Q", h]].dropna()
        ex[f"rho_Q_{h}"] = {"rho": spearman(s["Q"], s[h]), "n": len(s)}
    ex["tercile_mean_y20"] = d.groupby(pd.qcut(d["Q"].rank(method="first"), 3, labels=["low", "mid", "high"]),
                                       observed=True)["y20"].agg(["mean", "median", "count"]).round(5).to_dict()
    ex["trade_level"] = trade_level(d, events, prices)
    res["exploratory"] = ex

    (OUT / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k != "exploratory"}, ensure_ascii=False, indent=1, default=str))
    print("verdict:", verdict)


def trade_level(d: pd.DataFrame, events: dict, prices: dict) -> dict:
    """engine 忠实出场；交易按入场事件的 Q 三分位分组（探索性）。"""
    from backtest.engine import _simulate_trades
    cut = d["Q"].quantile([1 / 3, 2 / 3]).to_numpy()
    qmap = {(r.ticker, r.date): r.Q for r in d.itertuples()}
    rows = []
    for tk, evs in events.items():
        ev = [e for e in evs if e.date >= pd.Timestamp(EV_START)]
        for tr in _simulate_trades(prices[tk], ev):
            q = qmap.get((tk, pd.Timestamp(tr.entry_date)))
            if q is None:
                continue
            rows.append({"bucket": "low" if q <= cut[0] else ("high" if q > cut[1] else "mid"),
                         "pnl": tr.pnl_pct})
    t = pd.DataFrame(rows)
    if t.empty:
        return {}
    return t.groupby("bucket")["pnl"].agg(n="count", mean="mean", win=lambda s: float((s > 0).mean())).round(4).to_dict()


if __name__ == "__main__":
    main()
