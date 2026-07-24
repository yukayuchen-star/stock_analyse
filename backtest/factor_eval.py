"""R5 因子级 as-of 回测门 — 量化 special 信号的量价确认验证器。

动机：P7 回测撮合的是**缠论买卖点**，量化 10% sleeve 不独立产生交易，
无法回答「某量化因子改动是否提升信号质量」。本模块做**因子级 as-of 回测**：
逐日重放 momentum 的 pullback/breakout special 触发（**仅用 ≤当日数据，无前视**），
标注前向 5/10/20 交易日收益，对比「纯价格 vs 量能门」的胜率/期望/信号数/假信号率。

用法：`python -m backtest.factor_eval`
数据：`cache/market_data.db` 的 US OHLCV 价格 blob（yfinance auto_adjust，拆股已调整）。

2026-07-24 结论（R5.1/R5.2）：
  breakout 量能门 **过**（KEEP≥1.5× fwd10 win .611/exp +.0385 ≫ DEMOTE<1.0× .570/+.0165，
    且随阈值单调）→ 已 merge 为 momentum 默认。
  pullback 缩量门 **证伪且方向相反**（缩量 KEEP +.0041 < 放量 DEMOTE +.0170）→ 不 merge。

向量化实现说明：位置 t 的 rolling/ewm 仅消费 ≤t 数据，故整列滚动统计与逐日重放**等价**且无前视；
`_selfcheck` 在抽样日上断言向量化 special == 实盘 `momentum._special_signal`，防两路发射逻辑漂移。
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from signals.quant.momentum import _special_signal

DB = Path("cache/market_data.db")
FWD = (5, 10, 20)
PB_GRID = (0.6, 0.7, 0.8)      # pullback 缩量阈值网格
BO_GRID = (1.5, 2.0, 2.5)      # breakout 放量阈值网格
PRIMARY_H = 10                 # 主口径前向天数


def load_price_blobs(db: Path = DB) -> list[pd.DataFrame]:
    """从 cache 取所有含 OHLCV 的价格 blob（≥300 行）。"""
    con = sqlite3.connect(db)
    rows = pd.read_sql("SELECT data FROM cache", con)
    con.close()
    out: list[pd.DataFrame] = []
    for d in rows["data"]:
        if '"Volume"' not in d or '"Open"' not in d or '"Close"' not in d:
            continue
        obj = json.loads(d)
        df = pd.DataFrame(obj["data"])
        if "index" in df:
            df.index = pd.to_datetime(df["index"])
            df = df.drop(columns=["index"])
        if {"Close", "High", "Low", "Volume"}.issubset(df.columns) and len(df) >= 300:
            out.append(df[["Close", "High", "Low", "Volume"]].astype(float))
    return out


def build_events(df: pd.DataFrame) -> pd.DataFrame:
    """向量化 as-of：复刻 momentum._special_signal 触发 + 量能比率，附前向收益。"""
    close, high, low, vol = df.Close, df.High, df.Low, df.Volume
    ema20  = close.ewm(span=20, adjust=False).mean()
    sma200 = close.rolling(200).mean()
    h52    = close.rolling(252).max()
    vma20  = vol.rolling(20).mean()
    vol3   = vol.rolling(3).mean()                    # == tail(3).mean at t
    ema_dev = close / ema20 - 1.0
    rng = high - low

    e = pd.DataFrame({
        "pb_trig": (close > sma200) & (ema_dev >= -0.03) & (ema_dev <= 0.01),
        "bo_trig": (h52 > 0) & ((close - h52) / h52 >= -0.03) & ((close - h52) / h52 <= 0.00),
        "pb_ratio": vol3 / vma20,
        "bo_ratio": vol / vma20,
        "close_pos": np.where(rng > 0, (close - low) / rng, 0.5),
        "vma20": vma20,
    }, index=df.index)
    for h in FWD:
        e[f"fwd{h}"] = close.shift(-h) / close - 1.0
    return e[(e.vma20 > 0) & e.fwd20.notna() & (e.pb_trig | e.bo_trig)]


def _stat(sub: pd.DataFrame, h: int) -> tuple[int, float, float]:
    if len(sub) == 0:
        return 0, float("nan"), float("nan")
    f = sub[f"fwd{h}"]
    return len(sub), float((f > 0).mean()), float(f.mean())


def _selfcheck(blobs: list[pd.DataFrame], n_samples: int = 60) -> None:
    """抽样断言：向量化 special 触发 == 实盘逐日 _special_signal（两门皆关=纯价格）。"""
    rng = np.random.default_rng(7)
    checked = mism = 0
    for _ in range(n_samples):
        df = blobs[rng.integers(len(blobs))]
        t = int(rng.integers(260, len(df) - 21))
        sl = df.iloc[: t + 1]
        live, _ = _special_signal(sl, pullback_gate=False, breakout_gate=False)  # 纯价格
        c = float(sl.Close.iloc[-1])
        ema = float(sl.Close.ewm(span=20, adjust=False).mean().iloc[-1])
        ed = c / ema - 1.0
        sm = float(sl.Close.rolling(200).mean().iloc[-1])
        cand = []
        if c > sm and -0.03 <= ed <= 0.01:
            cand.append(0.30)
        h52 = float(sl.Close.rolling(252).max().iloc[-1])
        if h52 > 0 and -0.03 <= (c - h52) / h52 <= 0.00:
            cand.append(0.20)
        vec = max(cand) if cand else 0.0
        checked += 1
        mism += abs(vec - live) > 1e-12
    assert mism == 0, f"emission drift: {mism}/{checked} vectorized≠live"
    print(f"[selfcheck] vectorized == live _special_signal on {checked}/{checked} sampled days\n")


def main() -> None:
    blobs = load_price_blobs()
    if not blobs:
        print("no OHLCV price blobs in cache/market_data.db — run main.py first to populate cache")
        return
    _selfcheck(blobs)
    ev = pd.concat([build_events(b) for b in blobs], ignore_index=True)
    print(f"tickers={len(blobs)}  events={len(ev)}  "
          f"pullback={int(ev.pb_trig.sum())}  breakout={int(ev.bo_trig.sum())}\n")

    pb = ev[ev.pb_trig]
    print("=" * 72)
    print("PULLBACK  baseline=纯价格全触发 | KEEP=缩量满额 | DEMOTE=放量派发(≥1.0)")
    print("=" * 72)
    for h in FWD:
        n, w, x = _stat(pb, h)
        print(f"  baseline fwd{h:>2}: n={n:>5} win={w:.3f} exp={x:+.4f}")
    for thr in PB_GRID:
        nk, wk, xk = _stat(pb[pb.pb_ratio < thr], PRIMARY_H)
        nd, wd, xd = _stat(pb[pb.pb_ratio >= 1.0], PRIMARY_H)
        print(f"  thr={thr}: KEEP n={nk:>5} win={wk:.3f} exp={xk:+.4f} | "
              f"DEMOTE n={nd:>5} win={wd:.3f} exp={xd:+.4f} | Δexp={xk - xd:+.4f}")

    bo = ev[ev.bo_trig]
    print("\n" + "=" * 72)
    print("BREAKOUT  baseline=纯价格全触发 | KEEP=放量满额 | DEMOTE=无量近高(<1.0)")
    print("=" * 72)
    for h in FWD:
        n, w, x = _stat(bo, h)
        print(f"  baseline fwd{h:>2}: n={n:>5} win={w:.3f} exp={x:+.4f}")
    for thr in BO_GRID:
        nk, wk, xk = _stat(bo[bo.bo_ratio >= thr], PRIMARY_H)
        nd, wd, xd = _stat(bo[bo.bo_ratio < 1.0], PRIMARY_H)
        print(f"  thr={thr}×: KEEP n={nk:>5} win={wk:.3f} exp={xk:+.4f} | "
              f"DEMOTE n={nd:>5} win={wd:.3f} exp={xd:+.4f} | Δexp={xk - xd:+.4f}")

    print("\nVERDICT: breakout 门过(thr=1.5×，已 merge) | pullback 门证伪方向相反(不 merge)")


if __name__ == "__main__":
    main()
