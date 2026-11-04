"""出场与仓位 v1：X1 结构止损上移 · X2 等风险仓位（口径见仓库根 `PREREG-exit-sizing-v1.md`）。

价格、事件提取与回测引擎冻结，sha 不符即拒绝运行。数值结果落 `output/prereg_exit_sizing_v1/results.json`，
RESULTS.md 据此撰写。用法：`python -m backtest.prereg_exit_sizing_v1`
"""
from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import dataclass
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.prereg_vix_gate_v1 import delta_sharpe, month_block_boot, perf, sharpe, summarize

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "prereg_exit_sizing_v1"
PRICES = ROOT / "output" / "prereg_holdout_v1" / "prices.pkl"
CASH = ROOT / "output" / "prereg_vix_gate_v1" / "data.pkl"

FROZEN = {
    "PREREG-exit-sizing-v1.md": "4796c541db8766f07be22bfb6b2ded1216c85baa02c501063a6202dfb1c8920d",
    "output/prereg_holdout_v1/prices.pkl": "45c54c608155fc6d67e42f917ba7dccdbad0ce37c1623d71885b16ede9a9649f",
    "output/prereg_vix_gate_v1/data.pkl": "7f6258930c983f0b3580dc93cf847f37ce7cae0a289f4bab37ba821752a1032c",
    "backtest/prereg_vix_gate_v1.py": "e56aa9c8b63e5a858ad67f8fc34398fb79361b5ad9f4433639d69472035a8b12",
    "backtest/engine.py": "e18b13a1fce79d32f30111e49718e1c1cffd46e2fdab420890573a0186fcec17",
    "signals/chan/chan_signal.py": "735acfdd0328a5bda286b3a1ae848dd1f862cab057c943df4a593f34ac7d7db4",
    "signals/chan/stroke.py": "12de029b09653951633acb0fb51bb789cdc7ab74851bbad41b6de87d6158b568",
    "signals/chan/pivot.py": "af914cb8158432e480e2a748413b766431f3cdd2534ae67a1c438e1d52aef1ae",
    "signals/chan/fractal.py": "c8b672570333e5d1d3f7e120769f517236bb1376e5c08eef93c5ee97ed388abe",
}
SEED = 20261103
SIG_START, SIG_END = pd.Timestamp("2005-01-03"), pd.Timestamp("2020-11-30")
WARMUP = 200
BASE_W = 1 / 76
CLIP = (0.25, 2.0)
MIN_RREF = 100
COST, COST_HIGH = 0.0005, 0.0020
SPLIT = pd.Timestamp("2013-01-01")
EPISODES = {"2007-10~2009-06": ("2007-10-01", "2009-06-30"), "2020-02~2020-06": ("2020-02-01", "2020-06-30")}
R_MAX = 0.15


def check_frozen() -> None:
    for rel, sha in FROZEN.items():
        got = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if got != sha:
            raise SystemExit(f"冻结输入不符：{rel} {got[:12]}… ≠ {sha[:12]}…")


@dataclass
class T:
    ticker: str
    signal_type: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: str
    r: float            # 入场时的止损距离，参考价为事件日收盘
    raised: int         # 止损上移次数

    @property
    def pnl(self) -> float:
        return self.exit_price / self.entry_price - 1


def simulate(ticker: str, df: pd.DataFrame, events: list, raise_stop=False, lag=1, gap=True, tail=True,
             eligible=None) -> list:
    """沿用 `engine._simulate_trades` 的事件循环。止损只扫描上次检查之后的 K：
    引擎每次从入场日重扫，止损不变时等价；止损上移后重扫会把更早的低点误判为触发。"""
    from backtest.engine import _entry_stop
    idx = df.index
    low, opn, close = df["Low"].to_numpy(float), df["Open"].to_numpy(float), df["Close"].to_numpy(float)
    pos_of = {d: i for i, d in enumerate(idx)}
    trades: list = []
    st: dict | None = None

    def scan(upto: int):
        for i in range(st["checked"] + 1, upto + 1):
            if low[i] <= st["stop"]:
                return i, (min(st["stop"], opn[i]) if gap else st["stop"])
        st["checked"] = max(st["checked"], upto)
        return None

    def close_trade(i: int, px: float, why: str):
        trades.append(T(ticker, st["sig"], st["sdate"], idx[st["i"]], st["px"], idx[i], px, why, st["r"], st["raised"]))

    def open_trade(ev, i: int, px: float):
        stop = _entry_stop(ev)
        return {"i": i, "checked": i, "px": px, "stop": stop, "sig": ev.signal_type, "sdate": ev.date,
                "r": (ev.price - stop) / ev.price, "raised": 0}

    for ev in events:
        p = pos_of.get(ev.date)
        if p is None:
            continue
        i = p + lag
        if i >= len(idx):
            continue
        px = float(ev.price) if lag == 0 else float(close[i])
        is_buy = ev.signal_type.startswith("b")
        ok = is_buy and (eligible is None or eligible(ev, p))
        if st is not None:
            hit = scan(i)
            if hit:
                close_trade(hit[0], float(hit[1]), "sl")
                st = open_trade(ev, i, px) if ok and i > hit[0] else None
            elif ev.signal_type.startswith("s") and i > st["i"]:
                close_trade(i, px, "signal")
                st = None
            elif is_buy and raise_stop:
                new = _entry_stop(ev)
                if new > st["stop"]:
                    st["stop"], st["raised"] = new, st["raised"] + 1
        elif ok:
            st = open_trade(ev, i, px)
    if st is not None:
        last = len(idx) - 1
        hit = scan(last) if tail else None
        if hit:
            close_trade(hit[0], float(hit[1]), "sl")
        else:
            close_trade(last, float(close[last]), "eod")
    return trades


def eligibility(df: pd.DataFrame, r_max=None):
    def fn(ev, p):
        if not (SIG_START <= ev.date <= SIG_END) or p < WARMUP:
            return False
        if r_max is None:
            return True
        from backtest.engine import _entry_stop
        return (ev.price - _entry_stop(ev)) / ev.price <= r_max
    return fn


def r_ref_weights(trades: list, clip=CLIP) -> np.ndarray:
    """R_ref = 入场日之前已入场交易的 R 中位数；前 MIN_RREF 笔之前用基线权重。"""
    order = sorted(range(len(trades)), key=lambda k: trades[k].entry_date)
    w = np.full(len(trades), BASE_W)
    seen: list = []
    j = 0
    for k in order:
        d = trades[k].entry_date
        while j < len(order) and trades[order[j]].entry_date < d:
            seen.append(trades[order[j]].r)
            j += 1
        if len(seen) >= MIN_RREF and trades[k].r > 0:
            mult = float(np.median(seen)) / trades[k].r
            w[k] = BASE_W * (np.clip(mult, *clip) if clip else mult)
    return w


class Book:
    def __init__(self, prices: dict, rf: pd.Series, calendar: pd.DatetimeIndex):
        self.pos = {d: i for i, d in enumerate(calendar)}
        self.cal = calendar
        self.rf = rf.reindex(calendar).to_numpy()
        self.close = {t: df["Close"].astype(float) for t, df in prices.items()}

    def _paths(self, tr: T) -> tuple:
        c = self.close[tr.ticker]
        seg = c.loc[tr.entry_date: tr.exit_date].to_numpy(copy=True)
        dates = c.loc[tr.entry_date: tr.exit_date].index
        seg[0], seg[-1] = tr.entry_price, tr.exit_price
        if len(seg) < 2:
            return np.array([], dtype=int), np.array([]), self.pos[tr.entry_date], self.pos[tr.exit_date]
        loc = np.array([self.pos[d] for d in dates[1:]])
        return loc, seg[1:] / seg[:-1] - 1, self.pos[tr.entry_date], self.pos[tr.exit_date]

    def excess(self, trades: list, weights=None, cost=COST) -> pd.Series:
        n = len(self.cal)
        ret, gross, fee = np.zeros(n), np.zeros(n), np.zeros(n)
        weights = np.full(len(trades), BASE_W) if weights is None else weights
        for tr, w in zip(trades, weights):
            loc, r, a, b = self._paths(tr)
            ret[loc] += w * r
            gross[loc] += w
            fee[a] += w * cost
            fee[b] += w * cost
        scale = np.where(gross > 1, 1 / np.where(gross > 0, gross, 1), 1.0)
        return pd.Series(scale * ret - scale * gross * self.rf - fee, index=self.cal)


def summary(trades: list, ex: pd.Series, rf: pd.Series, gross=None) -> dict:
    pnl = np.array([t.pnl for t in trades])
    days = np.array([(t.exit_date - t.entry_date).days for t in trades])
    top = np.sort(pnl)[::-1][:10].sum()
    return {"n": len(trades), "win": float((pnl > 0).mean()), "mean_pnl": float(pnl.mean()),
            "median_pnl": float(np.median(pnl)), "hold_days": float(days.mean()), "sum_pnl": float(pnl.sum()),
            "top10_share_of_sum": float(top / pnl.sum()) if pnl.sum() else None,
            "reasons": pd.Series([t.exit_reason for t in trades]).value_counts().to_dict(),
            "raised_trades": int(sum(t.raised > 0 for t in trades)),
            "sharpe": sharpe(ex.to_numpy()), "perf": perf(ex + rf.reindex(ex.index))}


def boot(df: pd.DataFrame, a: str, b: str = "base") -> dict:
    return summarize(delta_sharpe(df[a].to_numpy(), df[b].to_numpy()), month_block_boot(df, a, b, seed=SEED))


def gates(arm: str, df: pd.DataFrame, by_ticker, high_cost: float) -> dict:
    out = {"G1": boot(df, arm)}
    g1 = out["G1"]
    g1["pass"] = bool(g1["est"] > 0 and g1["z"] >= 3.0)
    d = lambda x: delta_sharpe(x[arm].to_numpy(), x["base"].to_numpy())
    a, b = d(df[df.index < SPLIT]), d(df[df.index >= SPLIT])
    out["G2"] = {"d_2005_2012": a, "d_2013_2020": b, "pass": bool(a > 0 and b > 0)}
    worst_t, worst_d, worst_df = None, np.inf, None
    for t, x in by_ticker():
        v = d(x)
        if v < worst_d:
            worst_t, worst_d, worst_df = t, v, x
    g3 = boot(worst_df, arm)
    g3.update(drop=worst_t, **{"pass": bool(g3["est"] > 0 and g3["z"] >= 2.0)})
    out["G3"] = g3
    ep = {k: d(df[(df.index < s) | (df.index > e)]) for k, (s, e) in EPISODES.items()}
    out["G4"] = {"excl": ep, "pass": all(v > 0 for v in ep.values())}
    out["G5"] = {"d_20bp": high_cost, "pass": bool(high_cost > 0)}
    return out


def verdict(res: dict, g0_pass: bool) -> str:
    g1 = res["G1"]
    if g0_pass and all(res[k]["pass"] for k in ("G1", "G2", "G3", "G4", "G5")):
        return "支持"
    if g1["z"] <= -3.0:
        return "方向证伪"
    if -0.10 < g1["ci95"][0] and g1["ci95"][1] < 0.10:
        return "测得准的零效应"
    return "弃权"


def _init(p):
    global _P
    _P = p


def _ev(t):
    from signals.chan.chan_signal import extract_chan_events
    return t, extract_chan_events(_P[t])


def run_g0(prices: dict, events: dict, base: dict) -> dict:
    from backtest.engine import _simulate_trades
    diffs = 0
    n = 0
    for t, df in prices.items():
        a = _simulate_trades(df, events[t])
        b = simulate(t, df, events[t], raise_stop=False, lag=0, gap=False, tail=False)
        n += len(a)
        same = len(a) == len(b) and all(
            x.entry_date == y.entry_date and x.exit_date == y.exit_date and x.exit_reason == y.exit_reason
            and abs(x.entry_price - y.entry_price) == 0 and abs(x.exit_price - y.exit_price) == 0 for x, y in zip(a, b))
        diffs += not same
    rng = np.random.default_rng(SEED)
    names = sorted(prices)
    mism = 0
    for _ in range(30):
        t = names[rng.integers(len(names))]
        df = prices[t]
        T_ = df.index[rng.integers(WARMUP + 100, len(df) - 1)]
        cut = df.loc[:T_]
        ev = [e for e in events[t] if e.date <= T_]
        full = simulate(t, df, events[t], raise_stop=True, eligible=eligibility(df))
        part = simulate(t, cut, ev, raise_stop=True, eligible=eligibility(cut))
        key = lambda x: (x.entry_date, x.exit_date, x.exit_price, x.exit_reason)
        mism += {key(x) for x in full if x.exit_date < T_} != {key(x) for x in part if x.exit_date < T_}
    trs = [x for v in base.values() for x in v]
    w = r_ref_weights(trs)
    # R_ref 只用入场日之前：把入场日及之后的交易全部改掉，某笔权重不得变化
    probe = sorted(trs, key=lambda x: x.entry_date)[len(trs) // 2]
    alt = [T(x.ticker, x.signal_type, x.signal_date, x.entry_date, x.entry_price, x.exit_date, x.exit_price,
             x.exit_reason, x.r * 3 if (x.entry_date >= probe.entry_date and x is not probe) else x.r, x.raised) for x in trs]
    k = trs.index(probe)
    rref_ok = bool(r_ref_weights(alt)[k] == w[k])
    return {"engine_tickers_diff": int(diffs), "engine_trades": n, "truncation_mismatch": int(mism),
            "rref_as_of": rref_ok, "pass": diffs == 0 and mism == 0 and rref_ok}


def main() -> None:
    check_frozen()
    OUT.mkdir(parents=True, exist_ok=True)
    P = pickle.loads(PRICES.read_bytes())
    C = pickle.loads(CASH.read_bytes())
    prices = {t: P[t] for t in P if t not in ("QQQ", "SPY")}
    cache = OUT / "events.pkl"
    if cache.exists():
        events = pickle.loads(cache.read_bytes())
    else:
        with Pool(10, initializer=_init, initargs=(prices,)) as pool:
            events = dict(pool.map(_ev, sorted(prices)))
        cache.write_bytes(pickle.dumps(events))

    base = {t: simulate(t, prices[t], events[t], eligible=eligibility(prices[t])) for t in prices}
    x1 = {t: simulate(t, prices[t], events[t], raise_stop=True, eligible=eligibility(prices[t])) for t in prices}
    flat = lambda d, skip=None: [x for t, v in d.items() if t != skip for x in v]
    tb, t1 = flat(base), flat(x1)
    start = min(x.entry_date for x in tb)
    cal = P["QQQ"].loc[start: "2021-01-29"].index
    irx = C["IRX"]
    rf = (irx.reindex(cal.union(irx.index)).ffill() / 100 / 252).shift(1).reindex(cal)
    book = Book(prices, rf, cal)

    def frame(tb_, t1_, cost=COST) -> pd.DataFrame:
        return pd.DataFrame({"base": book.excess(tb_, cost=cost), "x1": book.excess(t1_, cost=cost),
                             "x2": book.excess(tb_, r_ref_weights(tb_), cost=cost)}).iloc[1:].dropna()

    df = frame(tb, t1)
    hi = frame(tb, t1, COST_HIGH)
    res: dict = {"window": [str(df.index[0].date()), str(df.index[-1].date())], "n_days": len(df),
                 "G0": run_g0(prices, events, base),
                 "arms": {"base": summary(tb, df["base"], rf), "x1": summary(t1, df["x1"], rf),
                          "x2": {"sharpe": sharpe(df["x2"].to_numpy()), "perf": perf(df["x2"] + rf.reindex(df.index))}}}
    w = r_ref_weights(tb)
    res["arms"]["x2"]["weight_mult"] = {k: float(v) for k, v in pd.Series(w / BASE_W).describe().items()}
    res["arms"]["x2"]["share_clipped_high"] = float((w >= BASE_W * CLIP[1] - 1e-12).mean())
    res["arms"]["x2"]["share_clipped_low"] = float((w <= BASE_W * CLIP[0] + 1e-12).mean())
    res["arms"]["base"]["r"] = {k: float(v) for k, v in pd.Series([x.r for x in tb]).describe().items()}

    def drops():
        for t in prices:
            yield t, frame(flat(base, t), flat(x1, t))

    def d_hi(a):
        return delta_sharpe(hi[a].to_numpy(), hi["base"].to_numpy())

    frames = [(t, x) for t, x in drops()]
    for arm in ("x1", "x2"):
        res[arm.upper()] = gates(arm, df, lambda: frames, d_hi(arm))
        res[arm.upper()]["verdict"] = verdict(res[arm.upper()], res["G0"]["pass"])

    # 探索性
    ex: dict = {}
    fb = {t: simulate(t, prices[t], events[t], eligible=eligibility(prices[t], R_MAX)) for t in prices}
    f1 = {t: simulate(t, prices[t], events[t], raise_stop=True, eligible=eligibility(prices[t], R_MAX)) for t in prices}
    d2 = frame(flat(fb), flat(f1))
    ex["r_max_filter"] = {"n_base": len(flat(fb)), "x1": boot(d2, "x1"), "x2": boot(d2, "x2"),
                          "sharpe_filtered_base": sharpe(d2["base"].to_numpy()),
                          "vs_unfiltered": boot(pd.DataFrame({"f": d2["base"], "base": df["base"]}).dropna(), "f")}
    d3 = df.assign(both=book.excess(t1, r_ref_weights(t1)).reindex(df.index), uncapped=book.excess(tb, r_ref_weights(tb, clip=None)).reindex(df.index))
    ex["x1_x2"] = boot(d3, "both")
    ex["x2_unclipped"] = boot(d3, "uncapped")
    for typ in ("b1", "b2", "b3"):
        a, b = [x for x in tb if x.signal_type == typ], [x for x in t1 if x.signal_type == typ]
        d4 = pd.DataFrame({"base": book.excess(a), "x1": book.excess(b)}).iloc[1:].dropna()
        ex[f"x1_{typ}"] = {**boot(d4, "x1"), "n_base": len(a), "n_x1": len(b)}
    qc = P["QQQ"]["Close"].astype(float).reindex(df.index)
    ex["qqq_sharpe"] = sharpe((qc.pct_change() - rf.reindex(df.index)).dropna().to_numpy())
    res["exploratory"] = ex
    (OUT / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print("results written", {k: res[k]["verdict"] for k in ("X1", "X2")}, "G0", res["G0"]["pass"])


if __name__ == "__main__":
    main()
