"""预注册因子验证 v1 —— P1：F4 残差动量（口径见仓库根 `PREREG-factor-v1.md`，运行前核对其 sha256）。

价格一次性钉死到 `output/prereg_factor_v1/prices.pkl`（auto_adjust 每次下载重算，不钉死则不可复现）；
数值结果落 `output/prereg_factor_v1/p1_f4.json`，RESULTS.md 据此撰写。

用法：`python -m backtest.prereg_factor_v1`（P1 F4′；`--v1` 原口径；`--p2` 跑 F3）
"""
from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path
from typing import Dict, Sequence

import numpy as np
import pandas as pd

from backtest.factor_lab import (
    FWD, MIN_NAMES_PER_DATE, PRIMARY_H, REFERENCE_FACTORS, VALIDATION_UNIVERSE,
    assert_asof_consistent, build_factor_panel, daily_ic, evaluate_factor, ir_stats,
    quantile_profile,
)
from signals.quant.ear import pooled_adj_r2, residual_ic

# 变更 1 之后的文件 hash（v1 原 hash 84840215…7cab5，见 RESULTS.md）
PREREG_SHA = "0083a432696ffbd385845025543b501951d0578c6d4b80ca2fb13eadc1d5563c"
EST_AMEND1 = 756   # 变更 1：α/β 估计窗 36 个月，严格长于残差求和窗
OUT = Path("output/prereg_factor_v1")
PRICE_START = "2019-01-02"
SIGNAL_START = "2022-01-03"
SUB_START = "2025-01-02"
SKIP = 21
SECTOR_ETF = {
    "XLK": "AAPL ADBE ADI AMAT AMD ARM AVGO CRM CRWD CSCO DELL FTNT IBM INTC INTU KLAC LRCX "
           "MRVL MSFT MU NOW NVDA ORCL PANW QCOM SNDK TXN",
    "XLF": "AXP BAC BLK C GS JPM MA MS SCHW V WFC",
    "XLV": "ABBV ABT AMGN DHR JNJ LLY MRK PFE TMO UNH",
    "XLI": "BA CAT DE GE HON LMT UNP VRT",
    "XLY": "AMZN HD LOW MCD NKE SBUX TSLA",
    "XLC": "CMCSA DIS GOOGL META NFLX VZ",
    "XLP": "COST KO PEP PG TGT WMT",
    "XLE": "COP CVX XOM",
}
TICKER_ETF = {t: etf for etf, names in SECTOR_ETF.items() for t in names.split()}
CASES = ("UNH", "MSFT", "NVDA", "META")
COST_ROUNDTRIP = 0.0010
REBAL = 10
BOOT_N, BOOT_BLOCK = 5000, 6


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_pinned_prices() -> Dict[str, pd.DataFrame]:
    """首跑下载并钉死；之后只读钉死文件。"""
    path = OUT / "prices.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    from data.pipeline import DataPipeline
    pl = DataPipeline()
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    out = {}
    for tk in [*VALIDATION_UNIVERSE, "QQQ", "SPY", *SECTOR_ETF]:
        df = pl.yf.get_price(tk, PRICE_START, end)
        if df is None or df.empty:
            print(f"  skip {tk}: no data")
            continue
        out[tk] = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(out))
    return out


def make_resid_mom(factors: Sequence[pd.Series], lookback: int, est: int | None = None):
    """F4/F4b：t 处用 [t−E+1, t] 的 OLS 估 α/β（v1 口径 E=L），残差在 [t−L+1, t−21] 上求和 ÷ 标准差。

    E=L 时带截距残差在估计窗内零和 ⇒ 求和窗的和 ≡ −最近 21 天残差和（变更 1 的起因），故变更 1 取 E=756。
    """
    est = est or lookback
    def fn(df: pd.DataFrame) -> pd.Series:
        r = df["Close"].astype(float).pct_change().to_numpy()
        X = np.column_stack([f.reindex(df.index).to_numpy() for f in factors])
        out = np.full(len(r), np.nan)
        need = int(np.ceil(0.9 * est))
        for t in range(est, len(r)):
            lo = t - est + 1
            y, x = r[lo:t + 1], X[lo:t + 1]
            ok = np.isfinite(y) & np.isfinite(x).all(axis=1)
            if ok.sum() < need:
                continue
            A = np.column_stack([np.ones(ok.sum()), x[ok]])
            coef, *_ = np.linalg.lstsq(A, y[ok], rcond=None)
            eps = y - np.column_stack([np.ones(len(y)), x]) @ coef
            e = eps[est - lookback: est - SKIP]
            e = e[np.isfinite(e)]
            sd = e.std(ddof=1)
            if len(e) > 2 and sd > 0:
                out[t] = e.sum() / sd
        return pd.Series(out, index=df.index)
    return fn


def finite_asof_check(prices, fn_for, n_tk=12, n_pts=10, seed=7) -> dict:
    """补充 as-of 检查：只在因子值有限的点上抽样（factor_lab 版会把暖机期 NaN 点算作通过）。"""
    rng = np.random.default_rng(seed)
    tks = list(rng.choice(sorted(prices), size=n_tk, replace=False))
    checked, maxdiff = 0, 0.0
    for tk in tks:
        df = prices[tk]
        fn = fn_for(tk)
        full = fn(df)
        idx = np.flatnonzero(np.isfinite(full.to_numpy()))
        for t in rng.choice(idx, size=min(n_pts, len(idx)), replace=False):
            b = float(fn(df.iloc[: t + 1]).iloc[-1])
            maxdiff = max(maxdiff, abs(float(full.iloc[t]) - b))
            checked += 1
    return {"checked": checked, "max_abs_diff": maxdiff, "tickers": tks}


def ic_block(panel, col, h=PRIMARY_H) -> dict:
    s = ir_stats(daily_ic(panel, col, f"fwd{h}"), h)
    return {k: (float(v) if isinstance(v, (float, np.floating)) else v) for k, v in s.items()}


def long_short(panel, col, close: pd.DataFrame) -> dict:
    """G3：每 10 TD 调仓，t 收盘出信号、t+1 收盘成交，Q5 多 / Q1 空等权，按换手扣 0.10% 往返。"""
    dates = close.index
    sig_dates = sorted(panel["date"].unique())
    reb = sig_dates[::REBAL]
    prev_w = pd.Series(dtype=float)
    rows = []
    for d in reb:
        i = dates.get_loc(d)
        if i + 1 + REBAL >= len(dates):
            break
        g = panel.loc[panel["date"] == d, ["ticker", col]].dropna()
        ret = (close.iloc[i + 1 + REBAL] / close.iloc[i + 1] - 1.0).reindex(g["ticker"])
        g = g.assign(ret=ret.to_numpy()).dropna()
        if len(g) < MIN_NAMES_PER_DATE or g[col].nunique() < 5:
            continue
        q = pd.qcut(g[col], 5, labels=False, duplicates="drop")
        lo, hi = g[q == q.min()], g[q == q.max()]
        w = pd.concat([pd.Series(1 / len(hi), index=hi["ticker"]),
                       pd.Series(-1 / len(lo), index=lo["ticker"])])
        turn = float(w.subtract(prev_w, fill_value=0).abs().sum())
        gross = float(hi["ret"].mean() - lo["ret"].mean())
        rows.append({"date": d, "gross": gross, "turnover": turn,
                     "net": gross - turn * COST_ROUNDTRIP / 2})
        prev_w = w
    df = pd.DataFrame(rows)
    ann = np.sqrt(252 / REBAL)

    def sharpe(x):
        return float(x.mean() / x.std(ddof=1) * ann) if x.std(ddof=1) > 0 else float("nan")

    net = df["net"].to_numpy()
    rng = np.random.default_rng(11)
    n = len(net)
    boots = []
    for _ in range(BOOT_N):
        starts = rng.integers(0, n, size=int(np.ceil(n / BOOT_BLOCK)))
        idx = np.concatenate([(s + np.arange(BOOT_BLOCK)) % n for s in starts])[:n]
        boots.append(sharpe(pd.Series(net[idx])))
    eq = (1 + df["net"]).cumprod()
    return {"periods": n, "start": str(df["date"].iloc[0].date()), "end": str(df["date"].iloc[-1].date()),
            "gross_sharpe": sharpe(df["gross"]), "net_sharpe": sharpe(df["net"]),
            "net_sharpe_ci95": [float(np.nanpercentile(boots, 2.5)), float(np.nanpercentile(boots, 97.5))],
            "mean_gross_per_period": float(df["gross"].mean()), "mean_net_per_period": float(df["net"].mean()),
            "annual_turnover": float(df["turnover"].iloc[1:].mean() * 252 / REBAL),
            "max_drawdown": float((eq / eq.cummax() - 1).min())}


def walk_forward(panel, col, full_sign, alt_col=None) -> dict:
    """G4①：2024-01 起每 6 个月一个测试窗，边界前后各 embargo 10 TD。"""
    ic = daily_ic(panel, col, f"fwd{PRIMARY_H}")
    ic_alt = daily_ic(panel, alt_col, f"fwd{PRIMARY_H}") if alt_col else None
    bounds = list(pd.date_range("2024-01-01", ic.index.max() + pd.offsets.MonthBegin(1), freq="6MS"))
    wins = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = ic[(ic.index >= a) & (ic.index < b)]
        seg = seg.iloc[REBAL:-REBAL] if len(seg) > 2 * REBAL else seg.iloc[0:0]
        if seg.empty:
            continue
        row = {"window": f"{a.date()}~{(b - pd.Timedelta(days=1)).date()}", "n": len(seg),
               "ic": float(seg.mean()), "same_sign": bool(np.sign(seg.mean()) == full_sign)}
        if ic_alt is not None:
            tr = (ic.index >= a - pd.DateOffset(months=24)) & (ic.index < a)
            row["train_ic_primary"] = float(ic[tr].mean())
            row["train_ic_alt"] = float(ic_alt[(ic_alt.index >= a - pd.DateOffset(months=24))
                                               & (ic_alt.index < a)].mean())
        wins.append(row)
    frac = float(np.mean([w["same_sign"] for w in wins])) if wins else float("nan")
    return {"windows": wins, "same_sign_frac": frac}


def main(amend1: bool = True) -> None:
    sha = _sha(Path("PREREG-factor-v1.md"))
    assert sha == PREREG_SHA, f"预注册文件 hash 不符：{sha}"
    print(f"[prereg] sha256 OK {sha[:12]}…")

    prices = load_pinned_prices()
    price_sha = _sha(OUT / "prices.pkl")
    qqq = prices["QQQ"]["Close"].astype(float).pct_change()
    etf_r = {e: prices[e]["Close"].astype(float).pct_change() for e in SECTOR_ETF}
    uni = {t: prices[t] for t in VALIDATION_UNIVERSE if t in prices}
    last_bar = str(max(df.index.max() for df in uni.values()).date())
    print(f"[data] {len(uni)}/78 只 + QQQ/SPY/8 ETF，prices.pkl sha256 {price_sha[:12]}…，末根 {last_bar}")

    est = EST_AMEND1 if amend1 else None
    tag = "p1b" if amend1 else "p1"
    print(f"[mode] {'变更 1：估计窗 756（探索性）' if amend1 else 'v1 冻结口径'}")
    f4 = make_resid_mom([qqq], 252, est)
    f4_126 = make_resid_mom([qqq], 126, est)

    # G0
    g0_lab = assert_asof_consistent(uni, f4, n_samples=120, seed=7)
    g0_fin = finite_asof_check(uni, lambda tk: f4)
    print(f"[G0] factor_lab {g0_lab} 点通过；有限点 {g0_fin['checked']} 个 max|diff|={g0_fin['max_abs_diff']:.2e}")

    frames = []
    for tk, df in uni.items():
        fns = {**REFERENCE_FACTORS, "f4": f4, "f4_126": f4_126,
               "f4b": make_resid_mom([qqq, etf_r[TICKER_ETF[tk]]], 252, est)}
        frames.append(build_factor_panel({tk: df}, fns))
    panel = pd.concat(frames, ignore_index=True)
    panel = panel[panel["date"] >= SIGNAL_START].sort_values(["date", "ticker"]).reset_index(drop=True)
    panel.to_pickle(OUT / f"{tag}_panel.pkl")
    refs = list(REFERENCE_FACTORS)
    sig_start, sig_end = str(panel["date"].min().date()), str(panel["date"].max().date())
    print(f"[panel] rows={len(panel)} 信号日 {sig_start} → {sig_end}，f4 覆盖 {panel['f4'].notna().mean():.1%}")

    # G1
    ev = evaluate_factor(panel, "f4", refs)
    g1 = {"verdict": ev["verdict"], "checks": ev["checks"], "ic": ev["ic_stats"],
          "q_means": [float(x) for x in ev["quantile"]["q_means"]], "hi_lo": ev["quantile"]["hi_lo"],
          "mono_rho": ev["quantile"]["mono_rho"], "year_sign": ev["year_sign"],
          "by_year": {int(k): float(v) for k, v in ev["by_year"]["mean"].items()},
          "max_corr": ev["max_corr"], "max_corr_with": ev["max_corr_with"],
          "corr_with_refs": {r: float(panel[["f4", r]].corr().iloc[0, 1]) for r in refs}}
    print(f"[G1] {g1['verdict']} {g1['checks']} IC={g1['ic']['ic_mean']:+.4f} t={g1['ic']['t_stat']:+.2f}")

    # G2
    ric = residual_ic(panel, "f4", refs, f"fwd{PRIMARY_H}", MIN_NAMES_PER_DATE)
    g2 = {"resid_ic": ir_stats(ric, PRIMARY_H), "adj_r2": pooled_adj_r2(panel, "f4", refs)}
    g2["pass"] = bool(abs(g2["resid_ic"]["t_stat"]) >= 2.0
                      and np.sign(g2["resid_ic"]["ic_mean"]) == np.sign(g1["ic"]["ic_mean"]))
    print(f"[G2] resid IC={g2['resid_ic']['ic_mean']:+.4f} t={g2['resid_ic']['t_stat']:+.2f} "
          f"adjR2={g2['adj_r2']:.3f} pass={g2['pass']}")

    # G3（方向按全样本 IC 符号：负 IC 则多 Q1 空 Q5）
    close = pd.DataFrame({t: df["Close"].astype(float) for t, df in uni.items()}).sort_index()
    sgn = np.sign(g1["ic"]["ic_mean"]) or 1.0
    p3 = panel.assign(_s=panel["f4"] * sgn)
    g3 = long_short(p3, "_s", close)
    g3["direction"] = "long Q5/short Q1" if sgn > 0 else "long Q1/short Q5"
    g3["pass"] = bool(g3["net_sharpe_ci95"][0] > 0)
    print(f"[G3] net Sharpe={g3['net_sharpe']:+.2f} CI={g3['net_sharpe_ci95']} pass={g3['pass']}")

    # G4
    wf = walk_forward(panel, "f4", sgn, alt_col="f4_126")
    drop = panel[~panel["ticker"].isin(CASES)]
    d4 = ic_block(drop, "f4")
    g4 = {"walk_forward": wf, "drop_cases": d4,
          "pass": bool(wf["same_sign_frac"] >= 0.6 and np.sign(d4["ic_mean"]) == sgn
                       and abs(d4["t_stat"]) >= 2.0)}
    print(f"[G4] WF 同号 {wf['same_sign_frac']:.2f}；剔四只 IC={d4['ic_mean']:+.4f} t={d4['t_stat']:+.2f} pass={g4['pass']}")

    robust = {"f4_fwd5": ic_block(panel, "f4", 5), "f4_fwd20": ic_block(panel, "f4", 20),
              "f4_126_fwd10": ic_block(panel, "f4_126"), "f4b_fwd10": ic_block(panel, "f4b"),
              "sub_2025_f4_fwd10": ic_block(panel[panel["date"] >= SUB_START], "f4"),
              "corr_f4_f4b": float(panel[["f4", "f4b"]].corr().iloc[0, 1]),
              "corr_f4_f4_126": float(panel[["f4", "f4_126"]].corr().iloc[0, 1])}
    for k, v in robust.items():
        if isinstance(v, dict):
            print(f"   {k}: IC={v['ic_mean']:+.4f} t={v['t_stat']:+.2f} n={v['n_days']}")

    # 个案：每只的 F4 横截面分位分布、最新值、时间序列 corr(F4, fwd10)
    pct = panel.groupby("date")["f4"].rank(pct=True)
    cases = {}
    for tk in CASES:
        m = panel["ticker"] == tk
        sub = panel[m]
        last = sub.dropna(subset=["f4"]).iloc[-1]
        cases[tk] = {"n_days": int(sub["f4"].notna().sum()),
                     "mean_pct_rank": float(pct[m].mean()),
                     "share_top_quintile": float((pct[m] > 0.8).mean()),
                     "share_bottom_quintile": float((pct[m] <= 0.2).mean()),
                     "last_date": str(last["date"].date()), "last_f4": float(last["f4"]),
                     "last_pct_rank": float(pct[m].iloc[-1]),
                     "ts_corr_f4_fwd10": float(sub[["f4", "fwd10"]].corr(method="spearman").iloc[0, 1])}

    verdict = "PASS" if (g0_fin["max_abs_diff"] == 0 and g1["verdict"] == "PASS" and g2["pass"]
                         and g3["pass"] and g4["pass"]) else "REJECT"
    res = {"prereg_sha256": sha, "prices_sha256": price_sha, "last_bar": last_bar,
           "signal_window": [sig_start, sig_end], "n_tickers": int(panel["ticker"].nunique()),
           "rows": int(len(panel)), "G0": {"factor_lab_points": g0_lab, **g0_fin}, "G1": g1,
           "G2": g2, "G3": g3, "G4": g4, "robustness": robust, "cases": cases, "verdict": verdict}
    (OUT / f"{tag}_f4.json").write_text(json.dumps(res, indent=1, default=str, ensure_ascii=False))
    print(f"\n[verdict] F4 = {verdict}  → {OUT / f'{tag}_f4.json'}")


# ── P2：F3 跳空回补率（PREREG §3.2，口径未变更）────────────────────────
F3_GRID = [(0.02, 20), (0.02, 60), (0.04, 20), (0.04, 60)]   # (G, HOLD)，首项为主规格


def load_pinned_earnings(tickers) -> Dict[str, list]:
    """财报日与 R11 同源（`get_earnings_history`），首跑钉死到 earnings_dates.json。"""
    path = OUT / "earnings_dates.json"
    if path.exists():
        return json.loads(path.read_text())
    from data.pipeline import DataPipeline
    pl = DataPipeline()
    out = {tk: pl.yf.get_earnings_history(tk) for tk in tickers}
    path.write_text(json.dumps(out, indent=0, sort_keys=True))
    return out


def f3_events(df: pd.DataFrame, event_dates, G: float):
    """逐事件 (i0, k, g, fill, value)；只用 ≤ d0+1 的数据。"""
    o, c, idx = df["Open"].astype(float).to_numpy(), df["Close"].astype(float).to_numpy(), df.index
    evs = []
    for d in pd.DatetimeIndex(pd.to_datetime(list(event_dates))).sort_values():
        i0 = int(idx.searchsorted(d))
        if i0 < 1 or i0 + 1 >= len(idx):
            continue
        gaps = [o[j] / c[j - 1] - 1.0 for j in (i0, i0 + 1)]
        if not all(np.isfinite(gaps)):
            continue
        k = i0 if abs(gaps[0]) >= abs(gaps[1]) else i0 + 1
        g = gaps[k - i0]
        if abs(g) < G:
            continue
        fill = float(np.clip((o[k] - c[k]) / (o[k] - c[k - 1]), 0.0, 1.0))
        evs.append((i0, k, g, fill, float(np.sign(g) * (1.0 - fill))))
    return evs


def make_f3_fn(event_dates, G: float, hold: int):
    def fn(df: pd.DataFrame) -> pd.Series:
        out = np.full(len(df), np.nan)
        for i0, _k, _g, _fill, v in f3_events(df, event_dates, G):
            out[i0 + 2: i0 + 2 + hold] = v      # 后一事件覆盖前一事件
        return pd.Series(out, index=df.index)
    return fn


def main_p2() -> None:
    from signals.quant.ear import make_ear_fn

    sha = _sha(Path("PREREG-factor-v1.md"))
    assert sha == PREREG_SHA, f"预注册文件 hash 不符：{sha}"
    prices = load_pinned_prices()
    uni = {t: prices[t] for t in VALIDATION_UNIVERSE if t in prices}
    spy = prices["SPY"]["Close"].astype(float)
    dates = load_pinned_earnings(sorted(uni))
    ed_sha = _sha(OUT / "earnings_dates.json")
    n_ev = sum(len(v) for v in dates.values())
    no_dates = sorted(t for t, v in dates.items() if not v)
    print(f"[prereg] sha OK；财报日 {n_ev} 个事件，最早 {min(v[0] for v in dates.values() if v)}，"
          f"无日期 {no_dates}；earnings_dates.json sha256 {ed_sha[:12]}…")

    G0_, H0 = F3_GRID[0]
    rng = np.random.default_rng(7)
    dated = sorted(t for t in uni if dates[t])
    lab_pts = 0
    for tk in rng.choice(dated, size=12, replace=False):
        lab_pts += assert_asof_consistent({tk: uni[tk]}, make_f3_fn(dates[tk], G0_, H0), n_samples=10)
    g0_fin = finite_asof_check({t: uni[t] for t in dated},
                               lambda tk: make_f3_fn(dates[tk], G0_, H0))
    print(f"[G0] factor_lab {lab_pts} 点；有限点 {g0_fin['checked']} 个 max|diff|={g0_fin['max_abs_diff']:.2e}")

    cols = [f"f3_g{int(G * 100)}_h{h}" for G, h in F3_GRID]
    col = cols[0]
    frames = []
    for tk in dated:
        fns = {**REFERENCE_FACTORS, "ear": make_ear_fn(dates[tk], spy),
               **{c: make_f3_fn(dates[tk], G, h) for c, (G, h) in zip(cols, F3_GRID)}}
        frames.append(build_factor_panel({tk: uni[tk]}, fns))
    panel = pd.concat(frames, ignore_index=True)
    panel = panel[panel["date"] >= SIGNAL_START].sort_values(["date", "ticker"]).reset_index(drop=True)
    panel.to_pickle(OUT / "p2_panel.pkl")
    refs = list(REFERENCE_FACTORS)
    names_per_day = panel.groupby("date")[col].count()
    sig_start, sig_end = str(panel["date"].min().date()), str(panel["date"].max().date())
    print(f"[panel] rows={len(panel)} 信号日 {sig_start} → {sig_end}；{col} 每日名字数 中位 "
          f"{names_per_day.median():.0f}，≥8 的日 {(names_per_day >= MIN_NAMES_PER_DATE).sum()}")

    ev = evaluate_factor(panel, col, [*refs, "ear"])
    g1 = {"verdict": ev["verdict"], "checks": ev["checks"], "ic": ev["ic_stats"],
          "q_means": [float(x) for x in ev["quantile"].get("q_means", [])],
          "mono_rho": ev["quantile"].get("mono_rho"), "year_sign": ev["year_sign"],
          "by_year": {int(k): float(v) for k, v in ev["by_year"]["mean"].items()},
          "max_corr": ev["max_corr"], "max_corr_with": ev["max_corr_with"],
          "corr_with_refs": {r: float(panel[[col, r]].corr().iloc[0, 1]) for r in [*refs, "ear"]}}
    abstain = g1["ic"]["n_days"] < 250
    print(f"[G1] {g1['verdict']} {g1['checks']} IC={g1['ic']['ic_mean']:+.4f} t={g1['ic']['t_stat']:+.2f} "
          f"n_days={g1['ic']['n_days']}{' → 弃权' if abstain else ''}")

    ric = residual_ic(panel, col, [*refs, "ear"], f"fwd{PRIMARY_H}", MIN_NAMES_PER_DATE)
    g2 = {"resid_ic": ir_stats(ric, PRIMARY_H), "adj_r2": pooled_adj_r2(panel, col, [*refs, "ear"])}
    g2["pass"] = bool(abs(g2["resid_ic"]["t_stat"]) >= 2.0
                      and np.sign(g2["resid_ic"]["ic_mean"]) == np.sign(g1["ic"]["ic_mean"]))
    print(f"[G2] resid IC={g2['resid_ic']['ic_mean']:+.4f} t={g2['resid_ic']['t_stat']:+.2f} "
          f"adjR2={g2['adj_r2']:.3f} pass={g2['pass']}")

    close = pd.DataFrame({t: df["Close"].astype(float) for t, df in uni.items()}).sort_index()
    sgn = np.sign(g1["ic"]["ic_mean"]) or 1.0
    g3 = long_short(panel.assign(_s=panel[col] * sgn), "_s", close)
    g3["direction"] = "long Q5/short Q1" if sgn > 0 else "long Q1/short Q5"
    g3["pass"] = bool(g3["net_sharpe_ci95"][0] > 0)
    print(f"[G3] net Sharpe={g3['net_sharpe']:+.2f} CI={g3['net_sharpe_ci95']} pass={g3['pass']}")

    wf = walk_forward(panel, col, sgn)
    d4 = ic_block(panel[~panel["ticker"].isin(CASES)], col)
    g4 = {"walk_forward": wf, "drop_cases": d4,
          "pass": bool(wf["same_sign_frac"] >= 0.6 and np.sign(d4["ic_mean"]) == sgn
                       and abs(d4["t_stat"]) >= 2.0)}
    print(f"[G4] WF 同号 {wf['same_sign_frac']:.2f}；剔四只 IC={d4['ic_mean']:+.4f} t={d4['t_stat']:+.2f} pass={g4['pass']}")

    robust = {f"{col}_fwd5": ic_block(panel, col, 5), f"{col}_fwd20": ic_block(panel, col, 20),
              **{f"{c}_fwd10": ic_block(panel, c) for c in cols[1:]},
              "ear_fwd10": ic_block(panel, "ear"),
              "sub_2025": ic_block(panel[panel["date"] >= SUB_START], col)}
    for k, v in robust.items():
        print(f"   {k}: IC={v['ic_mean']:+.4f} t={v['t_stat']:+.2f} n={v['n_days']}")

    # 个案事件表：每个 |g|≥2% 事件的 g / fill / F3 与 d0+2 起的 10 日收益
    cases = {}
    for tk in CASES:
        df = uni[tk]
        c = df["Close"].astype(float).to_numpy()
        rows = []
        for i0, k, g, fill, v in f3_events(df, dates[tk], G0_):
            if df.index[i0] < pd.Timestamp(SIGNAL_START):
                continue
            # 与面板同口径：生效首日 t=d0+2 的 fwd10 = C[d0+12]/C[d0+2]
            fwd = c[i0 + 12] / c[i0 + 2] - 1.0 if i0 + 12 < len(c) else None
            rows.append({"d0": str(df.index[i0].date()), "react": str(df.index[k].date()),
                         "gap": round(g, 4), "fill": round(fill, 3), "f3": round(v, 3),
                         "fwd10_from_d0p2": None if fwd is None else round(float(fwd), 4)})
        cases[tk] = rows

    verdict = ("ABSTAIN" if abstain else
               "PASS" if (g0_fin["max_abs_diff"] == 0 and g1["verdict"] == "PASS" and g2["pass"]
                          and g3["pass"] and g4["pass"]) else "REJECT")
    res = {"prereg_sha256": sha, "prices_sha256": _sha(OUT / "prices.pkl"), "earnings_sha256": ed_sha,
           "n_events": n_ev, "no_dates": no_dates, "signal_window": [sig_start, sig_end],
           "names_per_day_median": float(names_per_day.median()),
           "G0": {"factor_lab_points": lab_pts, **g0_fin}, "G1": g1, "G2": g2, "G3": g3, "G4": g4,
           "robustness": robust, "cases": cases, "verdict": verdict}
    (OUT / "p2_f3.json").write_text(json.dumps(res, indent=1, default=str, ensure_ascii=False))
    print(f"\n[verdict] F3 = {verdict}  → {OUT / 'p2_f3.json'}")


if __name__ == "__main__":
    import sys
    if "--p2" in sys.argv:
        main_p2()
    else:
        main(amend1="--v1" not in sys.argv)
