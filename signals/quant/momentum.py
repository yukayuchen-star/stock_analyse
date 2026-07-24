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
    """Cutler 口径 RSI（rolling mean），非 Wilder（ewm α=1/14）。

    R4.3 决策记录（2026-07-17，128 只缓存池实测）：两口径 RSI 值 mean|Δ|=6.1点/
    max 17点（Wilder 把极值拉向 50），但经 24%动量×30%量化×10%总分 三层衰减后
    |Δfinal|≤0.0035、80/20 特判带翻转 0 只——决策层差异不显著。切换需重跑 ML
    回测重建全部基线（现基线均为本口径），成本收益不成立 → 保留 Cutler，勿改。
    """
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


# ── Pullback / Breakout special 信号（R5 量价确认）────────────

def _special_signal(
    df: pd.DataFrame,
    *,
    pullback_gate: bool = False,
    breakout_gate: bool = True,
    pullback_thr: float = 0.7,
    breakout_thr: float = 1.5,
) -> tuple[float, dict]:
    """回调/突破 special 信号，含 R5 量价确认门。

    价格触发（与 R5 前一致）：
      Pullback  上升趋势(c>SMA200) 且 EMA20 偏离 ∈[-3%,+1%] → 原始 +0.30
      Breakout  价格在 52 周高点 3% 以内(-3%~0%)          → 原始 +0.20

    R5 量价确认门（每信号独立开关；无量能数据时自动回退该信号为纯价格）：
      breakout_gate（默认 ON，thr=1.5）— 因子级回测实证（127 只/27.9k 事件）：
        无量近高假突破前向显著更差 → breakout_vol_ratio=Volume[-1]/VMA20：
        ≥thr 放量真突破→满额 +0.20；[1.0,thr) 温和放量→ +0.10；<1.0 无量近高→ +0.05
        （三档由回测证实的 bo_ratio 单调性支撑）。KEEP win .611/exp +.0385 ≫ DEMOTE .570/+.0165。
        close_pos 仅诊断暴露，回测实测中性不参与打分。末窗量能缺失(NaN)→该信号回退纯价格。
      pullback_gate（默认 OFF）— A股「缩量回调=健康」经美股回测**证伪且方向相反**：
        缩量 KEEP exp +.0041 < 放量 DEMOTE +.0170（Δ=-.0130）→ 不加门，pullback 维持纯价格。
        反向门（奖励放量回调）疑似有效但属同段样本内过拟合，待 OOS 验证再议（R5.3 记录）。

    仅用 ≤末行数据，无前视；可安全用于 as-of 逐日重放。
    两门皆 OFF（或无量能数据）→ 精确回退 R5 前的纯价格行为。
    """
    close = df["Close"].dropna()
    aux: dict = {
        "special_signal":     0.0,
        "pullback_vol_ratio": None,
        "breakout_vol_ratio": None,
        "close_pos":          None,
    }
    if len(close) < 200:
        return 0.0, aux

    c       = float(close.iloc[-1])
    ema20_v = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema_dev = (c - ema20_v) / ema20_v if ema20_v > 0 else 0.0

    # ── 量能比率（仅 ≤当日数据；诊断值恒计算，是否 gate 由各信号开关决定）──
    pb_ratio = bo_ratio = None
    if "Volume" in df.columns:
        volume = df["Volume"].reindex(close.index).astype(float)
        if len(volume) >= 20:
            vma20 = float(volume.rolling(20).mean().iloc[-1])
            if np.isfinite(vma20) and vma20 > 0:
                # 末窗量能可能缺失(NaN)——须过滤，否则 NaN 比较恒 False 会把
                # 真突破误降为 +0.05，而非 docstring 承诺的"无量能数据→纯价格回退"。
                pb_raw = float(volume.tail(3).mean()) / vma20
                bo_raw = float(volume.iloc[-1]) / vma20
                if np.isfinite(pb_raw):
                    pb_ratio = aux["pullback_vol_ratio"] = pb_raw
                if np.isfinite(bo_raw):
                    bo_ratio = aux["breakout_vol_ratio"] = bo_raw

    candidates: list[float] = []

    # ── Pullback 触发（默认纯价格；gate 已证伪）─────────────
    sma200_v = float(close.rolling(200).mean().iloc[-1])
    if c > sma200_v and -0.03 <= ema_dev <= 0.01:
        if not (pullback_gate and pb_ratio is not None):
            candidates.append(0.30)
        elif pb_ratio < pullback_thr:
            candidates.append(0.30)
        elif pb_ratio < 1.0:
            candidates.append(0.15)
        else:
            candidates.append(-0.05)

    # ── Breakout 触发（默认量能门 ON，thr=1.5）─────────────
    if len(close) >= 252:
        h52 = float(close.rolling(252).max().iloc[-1])
        if h52 > 0 and -0.03 <= (c - h52) / h52 <= 0.00:
            if not (breakout_gate and bo_ratio is not None):
                candidates.append(0.20)
            else:
                # 三档由 bo_ratio 单调映射，回测已证前向收益随 bo_ratio 单调
                # （DEMOTE<1.0 / MID[1.0,thr) / KEEP≥thr 逐级抬升）。
                if bo_ratio >= breakout_thr:
                    bo = 0.20                # 放量=真突破
                elif bo_ratio >= 1.0:
                    bo = 0.10                # 温和放量=中性
                else:
                    bo = 0.05                # 无量近高=假突破预警
                candidates.append(bo)
                # close_pos 仅作诊断暴露（弱收盘信息）——回测实测中性，不参与打分
                if "High" in df.columns and "Low" in df.columns:
                    hi = float(df["High"].reindex(close.index).iloc[-1])
                    lo = float(df["Low"].reindex(close.index).iloc[-1])
                    aux["close_pos"] = (c - lo) / (hi - lo) if hi > lo else 0.5

    special = max(candidates) if candidates else 0.0
    aux["special_signal"] = special
    return special, aux


# ── 主函数 ────────────────────────────────────────────────────

def compute_momentum_score(df: pd.DataFrame) -> tuple[float, dict]:
    """
    动量因子得分 (-1 ~ 1)。

    子指标：
      ROC20   28%  — 20 日价格变化率
      MACD    28%  — 柱值 / ATR 归一化
      RSI14   24%  — 偏离 50 线，±70/30 区间调整
      KAMA    20%  — 5 日 KAMA 斜率

    Pullback/Breakout 附加信号（缠论二买/三买前置识别，R5 加量价确认门）：
      回调买入 +0.3  — 上升趋势中价格触及 EMA20 附近；缩量确认健康、放量降权
      突破信号 +0.2  — 价格接近 52 周高点 3% 以内；放量+强收盘确认、无量降权
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

    # ── Pullback / Breakout 信号（R5 量价确认门）─────────
    ema20_v = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema_dev = (c - ema20_v) / ema20_v if ema20_v > 0 else 0.0
    special, special_aux = _special_signal(df)  # 默认：breakout 量能门 ON、pullback 纯价格

    # 附加信号以非线性方式叠加，避免突破 ±1
    final = base + special * (1.0 - abs(base))

    indicators = {
        "roc20":         roc20,
        "macd_hist":     hist_v,
        "rsi14":         rsi_v,
        "kama_slope5d":  kama_slope,
        "ema20_dev":     ema_dev,
        **special_aux,
    }

    return float(np.clip(final, -1, 1)), indicators
