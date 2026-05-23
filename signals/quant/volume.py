from __future__ import annotations

import numpy as np
import pandas as pd


def compute_volume_score(df: pd.DataFrame) -> tuple[float, dict]:
    """
    量价因子得分 (-1 ~ 1)。

    子分项：
      OBV 趋势  60%  — 10 日 OBV 变化 / 近期 OBV 标准差归一化
      VWMA 偏离 40%  — (收盘 - VWMA20) / VWMA20

    量价齐升为正，量价背离（量跌价涨）为负。
    """
    close = df["Close"].dropna()
    if len(close) < 20 or "Volume" not in df.columns:
        return 0.0, {}

    volume = df["Volume"].reindex(close.index).fillna(0)

    # ── OBV ──────────────────────────────────────────────
    direction = np.sign(close.diff().fillna(0))
    obv = (direction * volume).cumsum()

    n_obv = min(10, len(obv) - 1)
    obv_std = float(obv.rolling(20).std().iloc[-1])
    if obv_std > 0 and n_obv > 0:
        obv_chg = float(obv.iloc[-1]) - float(obv.iloc[-1 - n_obv])
        obv_score = float(np.clip(obv_chg / (obv_std * 2), -1, 1))
    else:
        obv_score = 0.0

    # ── VWMA20 偏离 ────────────────────────────────────────
    vol_sum  = volume.rolling(20).sum().replace(0, np.nan)
    vwma     = (close * volume).rolling(20).sum() / vol_sum
    vwma_v   = float(vwma.iloc[-1]) if not pd.isna(vwma.iloc[-1]) else float(close.iloc[-1])
    c        = float(close.iloc[-1])
    vwma_dev = (c - vwma_v) / vwma_v if vwma_v > 0 else 0.0
    vwma_score = float(np.clip(vwma_dev * 15, -1, 1))  # 7% 偏离 → ±1

    # ── 合成 ─────────────────────────────────────────────
    score = 0.60 * obv_score + 0.40 * vwma_score

    indicators = {
        "obv_last":  float(obv.iloc[-1]),
        "obv_score": obv_score,
        "vwma20":    vwma_v,
        "vwma_dev":  vwma_dev,
    }

    return float(np.clip(score, -1, 1)), indicators
