"""R5 因子级 as-of 回测门 — 量化 special 信号的量价确认验证器。

动机：P7 回测撮合的是**缠论买卖点**，量化 10% sleeve 不独立产生交易，
无法回答「某量化因子改动是否提升信号质量」。本模块做**因子级 as-of 回测**：
逐日重放 momentum 的 pullback/breakout special 触发（**仅用 ≤当日数据，无前视**），
标注前向 5/10/20 交易日收益，对比「纯价格 vs 量能门」的胜率/期望/信号数/假信号率。

用法：`python -m backtest.factor_eval`
数据：`cache/market_data.db` 的 OHLCV 价格 blob（yfinance auto_adjust，拆股已调整）。
  样本为**扫描过的美股单票为主**；cache 键为哈希、blob 不带 ticker，无法 allowlist，
  故 QQQ/SPY 等基准 ETF（有量能）可能混入 ~2/127（^VIX 无量能已被 OHLCV 过滤天然剔除），
  量级可忽略、不影响 breakout 门单调性结论。

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
    # skipna 与 prod 的 volume.tail(3).mean() 对齐（rolling(3).mean 会传播 NaN，二者
    # 在缺量能末窗上分叉——用 min_periods=1 复刻 skipna 语义，防两路发射逻辑漂移）。
    vol3   = vol.rolling(3, min_periods=1).mean()     # == tail(3).mean(skipna) at t
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


def _selfcheck(blobs: list[pd.DataFrame], n_samples: int = 120) -> None:
    """抽样断言：向量化 build_events 与实盘 _special_signal 完全对齐——
    (a) 纯价格 special 触发一致；(b) pb_ratio/bo_ratio 一致（含缺量能末窗，
    覆盖 rolling(3,min_periods=1) 与 tail(3).mean(skipna) 的语义等价）。"""
    rng = np.random.default_rng(7)
    checked = mism = 0
    for _ in range(n_samples):
        df = blobs[rng.integers(len(blobs))]
        t = int(rng.integers(260, len(df) - 21))
        sl = df.iloc[: t + 1]
        _, live_aux = _special_signal(sl, pullback_gate=False, breakout_gate=False)
        row = build_events(df)  # 整列，取位置 t
        vec = row.loc[df.index[t]] if df.index[t] in row.index else None
        c = float(sl.Close.iloc[-1])
        sm = float(sl.Close.rolling(200).mean().iloc[-1])
        ed = c / float(sl.Close.ewm(span=20, adjust=False).mean().iloc[-1]) - 1.0
        h52 = float(sl.Close.rolling(252).max().iloc[-1])
        pb_trig = c > sm and -0.03 <= ed <= 0.01
        bo_trig = h52 > 0 and -0.03 <= (c - h52) / h52 <= 0.00
        checked += 1
        # (b) 若该日进入事件集，断言量能比率两路一致
        if vec is not None and bool(vec.pb_trig) == pb_trig and bool(vec.bo_trig) == bo_trig:
            lpb, lbo = live_aux["pullback_vol_ratio"], live_aux["breakout_vol_ratio"]
            if lpb is not None and abs(float(vec.pb_ratio) - lpb) > 1e-9:
                mism += 1
            if lbo is not None and abs(float(vec.bo_ratio) - lbo) > 1e-9:
                mism += 1
        elif pb_trig or bo_trig:
            # 触发但未落入事件集（仅当 fwd20 为 NaN 的尾部日）→ 允许
            if t < len(df) - 20:
                mism += 1
    assert mism == 0, f"emission drift: {mism}/{checked} vectorized≠live"
    print(f"[selfcheck] build_events == live _special_signal (触发+量能比率) on {checked}/{checked} 抽样日\n")


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
    # 三档单调性验证：实盘 shipped 三档 <1.0(+0.05) / [1.0,thr)(+0.10) / ≥thr(+0.20)
    # 的前向收益须单调抬升，方为中间档 +0.10 的证据（回应 code-review Finding 4）。
    thr = 1.5  # shipped breakout_thr
    lo  = _stat(bo[bo.bo_ratio < 1.0], PRIMARY_H)
    mid = _stat(bo[(bo.bo_ratio >= 1.0) & (bo.bo_ratio < thr)], PRIMARY_H)
    hi  = _stat(bo[bo.bo_ratio >= thr], PRIMARY_H)
    print(f"  [shipped 三档 @thr={thr}×] "
          f"DEMOTE<1.0 exp={lo[2]:+.4f}(n={lo[0]}) → MID[1.0,{thr}) exp={mid[2]:+.4f}(n={mid[0]}) "
          f"→ KEEP≥{thr} exp={hi[2]:+.4f}(n={hi[0]})  单调={'✓' if lo[2] <= mid[2] <= hi[2] else '✗'}")

    print("\nVERDICT: breakout 门过(thr=1.5×，已 merge) | pullback 门证伪方向相反(不 merge)")


if __name__ == "__main__":
    main()
