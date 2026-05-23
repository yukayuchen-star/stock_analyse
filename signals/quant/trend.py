from __future__ import annotations

import numpy as np
import pandas as pd


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> tuple[float, float, float]:
    """Wilder ADX。返回 (adx, +DI, -DI)。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    up  = high - high.shift(1)
    dn  = low.shift(1) - low
    plus_dm  = pd.Series(np.where((up > dn) & (up > 0), up,  0.0), index=close.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn,  0.0), index=close.index)

    alpha = 1.0 / period
    atr      = tr.ewm(alpha=alpha, adjust=False).mean().replace(0, np.nan)
    plus_di  = 100 * plus_dm.ewm(alpha=alpha,  adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx  = 100 * (plus_di - minus_di).abs() / di_sum
    adx = dx.ewm(alpha=alpha, adjust=False).mean()

    def _last(s: pd.Series) -> float:
        v = s.iloc[-1]
        return 0.0 if pd.isna(v) else float(v)

    return _last(adx), _last(plus_di), _last(minus_di)


def compute_trend_score(df: pd.DataFrame) -> tuple[float, dict]:
    """
    趋势因子得分 (-1 ~ 1)。

    子分项（各自 -1~+1）及权重：
      MA 位置   35%  — price vs SMA20/60/200
      MA 排列   25%  — 均线多空对齐（金叉/死叉）
      EMA 斜率  25%  — 5日 EMA20 斜率
      ADX       15%  — 趋势强度 × 方向

    需要至少 30 行数据；SMA200 建议 200+ 行。
    """
    close = df["Close"].dropna()
    if len(close) < 30:
        return 0.0, {}

    high = df["High"].reindex(close.index)
    low  = df["Low"].reindex(close.index)
    c    = float(close.iloc[-1])

    # ── MA ───────────────────────────────────────────────
    sma20  = float(close.rolling(20).mean().iloc[-1])
    sma60  = float(close.rolling(60).mean().iloc[-1])  if len(close) >= 60  else None
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

    ema20 = close.ewm(span=20, adjust=False).mean()

    # ── MA 位置得分 ───────────────────────────────────────
    pos = 0.0
    if sma200 is not None:
        pos += 0.40 * (1.0 if c > sma200 else -1.0)
        pos += 0.30 * (1.0 if c > sma60  else -1.0) if sma60 is not None else 0
        pos += 0.30 * (1.0 if c > sma20  else -1.0)
    elif sma60 is not None:
        pos += 0.50 * (1.0 if c > sma60  else -1.0)
        pos += 0.50 * (1.0 if c > sma20  else -1.0)
    else:
        pos = 1.0 if c > sma20 else -1.0

    # ── MA 排列得分 ───────────────────────────────────────
    if sma60 is not None and sma200 is not None:
        if sma20 > sma60 > sma200:
            align = 1.0
        elif sma20 < sma60 < sma200:
            align = -1.0
        else:
            # 部分对齐
            align = 0.5 * (1 if sma20 > sma60 else -1)
    elif sma60 is not None:
        align = 1.0 if sma20 > sma60 else -1.0
    else:
        align = 0.0

    # ── EMA20 斜率（5 日）───────────────────────────────
    n = min(5, len(ema20) - 1)
    base = float(ema20.iloc[-1 - n])
    ema_slope = (float(ema20.iloc[-1]) - base) / base if base > 0 else 0.0
    slope_score = float(np.clip(ema_slope * 50, -1, 1))  # 2% 5日变动 → ±1

    # ── ADX ──────────────────────────────────────────────
    adx_val, plus_di, minus_di = _adx(high, low, close)
    adx_dir      = 1.0 if plus_di >= minus_di else -1.0
    adx_strength = min(adx_val / 30.0, 1.0)   # ADX=30 视为强趋势
    adx_score    = adx_strength * adx_dir

    # ── 合成 ─────────────────────────────────────────────
    score = (
        0.35 * pos
      + 0.25 * align
      + 0.25 * slope_score
      + 0.15 * adx_score
    )

    indicators = {
        "sma20":       sma20,
        "sma60":       sma60,
        "sma200":      sma200,
        "ema20":       float(ema20.iloc[-1]),
        "ema_slope5d": ema_slope,
        "adx":         adx_val,
        "plus_di":     plus_di,
        "minus_di":    minus_di,
    }

    return float(np.clip(score, -1, 1)), indicators
