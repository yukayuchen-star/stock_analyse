from __future__ import annotations

import numpy as np
import pandas as pd


# ── 辅助计算 ──────────────────────────────────────────────────

def _macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.Series:
    """返回 MACD 柱（histogram = DIF - DEA）。"""
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=sig, adjust=False).mean()
    return dif - dea


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    v = rsi.iloc[-1]
    return 50.0 if pd.isna(v) else float(v)


def _kama(close: pd.Series, period: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    """Kaufman Adaptive Moving Average（递推，无前视）。"""
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    vals  = close.values.astype(float)
    kama  = vals.copy()
    for i in range(period, len(vals)):
        direction  = abs(vals[i] - vals[i - period])
        volatility = np.sum(np.abs(np.diff(vals[i - period: i + 1])))
        er  = direction / volatility if volatility > 0 else 0.0
        sc  = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama[i] = kama[i - 1] + sc * (vals[i] - kama[i - 1])
    return pd.Series(kama, index=close.index)


# ── 主函数 ────────────────────────────────────────────────────

def compute_momentum_score(df: pd.DataFrame) -> tuple[float, dict]:
    """
    动量因子得分 (-1 ~ 1)。

    子指标：
      ROC20   28%  — 20 日价格变化率
      MACD    28%  — 柱值 / ATR 归一化
      RSI14   24%  — 偏离 50 线，±70/30 区间调整
      KAMA    20%  — 5 日 KAMA 斜率

    Pullback/Breakout 附加信号（缠论二买/三买前置识别）：
      回调买入 +0.3  — 上升趋势中价格触及 EMA20 附近（±3%）
      突破信号 +0.2  — 价格接近 52 周高点 3% 以内
    """
    close = df["Close"].dropna()
    if len(close) < 30:
        return 0.0, {}

    c = float(close.iloc[-1])

    # ── ROC20 ────────────────────────────────────────────
    roc20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else 0.0
    roc_score = float(np.clip(roc20 / 0.12, -1, 1))   # 12% 变动 → ±1

    # ── MACD ─────────────────────────────────────────────
    hist    = _macd(close)
    hist_v  = float(hist.iloc[-1])
    atr_std = float(close.rolling(20).std().iloc[-1]) if len(close) >= 20 else 1.0
    macd_score = float(np.clip(hist_v / max(atr_std * 0.3, 1e-9), -1, 1))

    # ── RSI14 ────────────────────────────────────────────
    rsi_v = _rsi(close)
    if rsi_v >= 80:
        rsi_score = 0.5     # 强势超买，动量仍正但略降
    elif rsi_v <= 20:
        rsi_score = -0.5    # 超卖，动量仍负但略升
    else:
        rsi_score = float(np.clip((rsi_v - 50) / 35, -1, 1))

    # ── KAMA 斜率 ────────────────────────────────────────
    kama    = _kama(close)
    n_k     = min(5, len(kama) - 1)
    kama_prev = float(kama.iloc[-1 - n_k])
    kama_slope = (float(kama.iloc[-1]) - kama_prev) / kama_prev if kama_prev > 0 else 0.0
    kama_score = float(np.clip(kama_slope * 30, -1, 1))  # 3% 5日 → ±1

    # ── 基础合成 ─────────────────────────────────────────
    base = (
        0.28 * roc_score
      + 0.28 * macd_score
      + 0.24 * rsi_score
      + 0.20 * kama_score
    )

    # ── Pullback / Breakout 信号 ─────────────────────────
    special = 0.0
    ema20   = close.ewm(span=20, adjust=False).mean()
    ema20_v = float(ema20.iloc[-1])
    ema_dev = (c - ema20_v) / ema20_v if ema20_v > 0 else 0.0

    if len(close) >= 200:
        sma200_v = float(close.rolling(200).mean().iloc[-1])
        in_uptrend = c > sma200_v
        # 回调买入：价格在 EMA20 附近（-3% ~ +1%），处于上升趋势
        if in_uptrend and -0.03 <= ema_dev <= 0.01:
            special = 0.30

    # 突破信号：价格在 52W 高点 3% 以内
    if len(close) >= 252:
        h52 = float(close.rolling(252).max().iloc[-1])
        if h52 > 0 and -0.03 <= (c - h52) / h52 <= 0.00:
            special = max(special, 0.20)

    # 附加信号以非线性方式叠加，避免突破 ±1
    final = base + special * (1.0 - abs(base))

    indicators = {
        "roc20":         roc20,
        "macd_hist":     hist_v,
        "rsi14":         rsi_v,
        "kama_slope5d":  kama_slope,
        "ema20_dev":     ema_dev,
        "special_signal": special,
    }

    return float(np.clip(final, -1, 1)), indicators
