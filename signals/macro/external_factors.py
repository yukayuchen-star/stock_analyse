"""
外部宏观因子模块

跟踪对美股（尤其科技股）影响最直接的四类宏观异动信号：

  1. 油价 (WTI CL=F)
       油价↑→通胀压力↑→Fed 被迫加息→利率杀估值→科技股承压
  2. 加息预期 (2Y国债 - 现行 Fed Funds Rate)
       利差扩张 = 市场押注更多加息 → 偏空科技
  3. 美元指数 (DXY DX-Y.NYB)
       强美元 → 跨国科技公司海外营收缩水 → 盈利预期下修
  4. 通胀预期 (FRED T10YIE 10年盈亏平衡)
       通胀预期上行趋势 = Fed 需维持鹰派 → 压制估值

另提供异动检测：各指标相对 252 日历史的 Z-score，
|z| ≥ 2.0 时标记为"异动"，输出预警文本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf
from loguru import logger


# ── 常量 ────────────────────────────────────────────────────────

_WINDOW  = 20    # 短期动量窗口（交易日）
_HIST    = 252   # 异动检测历史窗口
_Z_ALERT = 2.0   # 异动 Z-score 门槛

# 各指标正常化分母（调整到 ±1 的参考幅度）
_OIL_NORM    = 0.15   # 油价 20d 涨跌 ±15% → score ±1
_DOLLAR_NORM = 0.05   # DXY 20d 涨跌 ±5%  → score ±1
_INFL_NORM   = 0.50   # 盈亏平衡利率 20d 变化 ±0.5pp → score ±1
_HIKE_NORM   = 1.50   # 2Y-FEDFUNDS 利差 ±1.5pp → score ±1

# 外部资产 ticker
_OIL_TICKER = "CL=F"        # WTI 原油期货
_DXY_TICKER = "DX-Y.NYB"    # 美元指数


# ── 输出数据类 ────────────────────────────────────────────────────

@dataclass
class ExternalFactorsResult:
    """
    外部宏观因子汇总。score 均为 -1~1，正代表对多头有利，负代表偏空。
    """
    # 油价
    oil_price:    float = 0.0
    oil_ret_20d:  float = 0.0   # 20日收益率
    oil_signal:   float = 0.0   # -1~1，油价↑→负

    # 加息预期（2Y - FEDFUNDS）
    fed_funds_rate:  float = 0.0
    dgs2:            float = 0.0
    rate_hike_gap:   float = 0.0   # 2Y - FEDFUNDS（pp），正值=市场押注加息
    rate_hike_signal: float = 0.0  # -1~1，gap 越大越偏空

    # 美元指数
    dxy_level:   float = 0.0
    dxy_ret_20d: float = 0.0
    dollar_signal: float = 0.0  # -1~1，DXY↑→负

    # 通胀预期（10Y 盈亏平衡利率）
    breakeven_10y:   float = 0.0
    breakeven_trend: float = 0.0   # 20日变化（pp）
    inflation_signal: float = 0.0  # -1~1，通胀预期↑→负

    # 异动检测
    anomalies:     List[str] = field(default_factory=list)   # 人类可读预警
    anomaly_score: float = 0.0   # 加总异动严重程度（-1~0：均为偏空压力）

    # 综合外部因子得分（-1~1）
    composite_score: float = 0.0

    reasoning: str = ""


# ── 辅助函数 ──────────────────────────────────────────────────────

def _zscore_latest(series: pd.Series, window: int = _HIST) -> float:
    """计算最新值相对过去 window 日的 Z-score。"""
    if len(series) < window // 2:
        return 0.0
    hist = series.iloc[-window:]
    mu, sigma = hist.mean(), hist.std()
    if sigma < 1e-9:
        return 0.0
    return float((series.iloc[-1] - mu) / sigma)


def _fetch_price_series(ticker: str, period: str = "2y") -> pd.Series:
    """下载收盘价 Series，出错返回空 Series。"""
    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        if df.empty:
            return pd.Series(dtype=float)
        close = df["Close"]
        return close.squeeze() if isinstance(close, pd.DataFrame) else close
    except Exception as exc:
        logger.warning(f"[ExtMacro] {ticker} 下载失败: {exc}")
        return pd.Series(dtype=float)


def _ret_20d(series: pd.Series) -> float:
    """最近 20 个交易日收益率，数据不足返回 0。"""
    if len(series) < _WINDOW + 1:
        return 0.0
    return float(series.iloc[-1] / series.iloc[-_WINDOW - 1] - 1)


# ── 核心计算 ──────────────────────────────────────────────────────

def _oil_signal(snapshot: Dict[str, float]) -> Tuple[float, float, float, Optional[str]]:
    """
    返回：(oil_price, ret_20d, signal, anomaly_text_or_None)
    signal: 油价↑ → 通胀压力 → 科技股空头信号（负）
    """
    series = _fetch_price_series(_OIL_TICKER)
    if series.empty:
        return 0.0, 0.0, 0.0, None

    price  = float(series.iloc[-1])
    ret    = _ret_20d(series)
    signal = float(np.clip(-ret / _OIL_NORM, -1.0, 1.0))

    z = _zscore_latest(series)
    alert = None
    if abs(z) >= _Z_ALERT:
        direction = "飙升" if ret > 0 else "暴跌"
        alert = (
            f"⚠️ 油价异动 WTI={price:.1f} 20d={ret:+.1%} Z={z:.1f}σ"
            f"（{direction}→{'通胀压力↑' if ret > 0 else '通缩信号'}）"
        )
        logger.warning(f"[ExtMacro] {alert}")

    return price, ret, signal, alert


def _rate_hike_signal(
    dgs2: float,
    fed_funds_rate: float,
) -> Tuple[float, float, Optional[str]]:
    """
    2Y国债 - 现行 Fed Funds Rate = 市场隐含加息预期。
    gap 越大 → 市场预期加息越多 → 利率上行压力 → 科技股偏空。
    返回：(rate_hike_gap, signal, anomaly_text_or_None)
    """
    gap    = float(dgs2 - fed_funds_rate)
    signal = float(np.clip(-gap / _HIKE_NORM, -1.0, 1.0))

    alert = None
    if gap > 1.5:
        alert = (
            f"⚠️ 加息预期强烈：2Y={dgs2:.2f}% FEDFUNDS={fed_funds_rate:.2f}%"
            f" gap=+{gap:.2f}pp（市场押注大幅加息）"
        )
        logger.warning(f"[ExtMacro] {alert}")
    elif gap < -0.5:
        alert = (
            f"ℹ️ 降息预期：2Y={dgs2:.2f}% FEDFUNDS={fed_funds_rate:.2f}%"
            f" gap={gap:.2f}pp（市场押注降息）"
        )
        logger.info(f"[ExtMacro] {alert}")

    return gap, signal, alert


def _dollar_signal() -> Tuple[float, float, float, Optional[str]]:
    """
    美元指数 DXY。强美元 → 跨国科技公司营收承压 → 偏空。
    返回：(dxy_level, ret_20d, signal, anomaly_text_or_None)
    """
    series = _fetch_price_series(_DXY_TICKER)
    if series.empty:
        return 0.0, 0.0, 0.0, None

    level  = float(series.iloc[-1])
    ret    = _ret_20d(series)
    signal = float(np.clip(-ret / _DOLLAR_NORM, -1.0, 1.0))

    z = _zscore_latest(series)
    alert = None
    if abs(z) >= _Z_ALERT:
        direction = "走强" if ret > 0 else "走弱"
        alert = (
            f"⚠️ 美元异动 DXY={level:.1f} 20d={ret:+.1%} Z={z:.1f}σ"
            f"（美元{direction}→{'科技股汇兑压力↑' if ret > 0 else '汇兑顺风'}）"
        )
        logger.warning(f"[ExtMacro] {alert}")

    return level, ret, signal, alert


def _inflation_signal(snapshot: Dict[str, float]) -> Tuple[float, float, float, Optional[str]]:
    """
    10Y 通胀盈亏平衡利率（FRED T10YIE）趋势。
    通胀预期↑ → Fed 维持鹰派 → 压制高估值科技 → 偏空。
    返回：(breakeven_10y, trend_20d, signal, anomaly_text_or_None)
    """
    val = snapshot.get("T10YIE")
    if val is None:
        return 0.0, 0.0, 0.0, None

    # 从 FRED 历史序列计算趋势（snapshot 仅有最新值，简化用 DGS10-DGS2 代理趋势）
    # 实际上这里我们只有当前值；趋势用 (T10YIE - 历史均值) 近似
    # 使用 T10YIE 当前值 vs 2.5%（长期通胀目标+风险溢价）作基准
    TARGET_INFL = 2.5   # Fed 2% 目标 + 0.5% 风险溢价
    deviation   = float(val - TARGET_INFL)
    signal      = float(np.clip(-deviation / _INFL_NORM, -1.0, 1.0))

    alert = None
    if val > 3.0:
        alert = (
            f"⚠️ 通胀预期偏高 T10YIE={val:.2f}% > 3.0%"
            f"（市场对通胀担忧持续，Fed 鹰派预期维持）"
        )
        logger.warning(f"[ExtMacro] {alert}")
    elif val < 1.5:
        alert = (
            f"ℹ️ 通胀预期偏低 T10YIE={val:.2f}% < 1.5%"
            f"（通缩风险，Fed 或转鸽，对科技股正面）"
        )

    # trend 用和 TARGET 的偏差代理（正值代表上行压力）
    trend = deviation
    return float(val), trend, signal, alert


# ── 主函数 ────────────────────────────────────────────────────────

def compute_external_factors(snapshot: Dict[str, float]) -> ExternalFactorsResult:
    """
    计算所有外部宏观因子。
    snapshot 需含 FEDFUNDS / DGS2 / T10YIE（来自 FRED）。
    """
    anomalies: List[str] = []

    # 1. 油价
    oil_price, oil_ret, oil_sig, oil_alert = _oil_signal(snapshot)
    if oil_alert:
        anomalies.append(oil_alert)

    # 2. 加息预期
    fedfunds = snapshot.get("FEDFUNDS", 0.0) or 0.0
    dgs2     = snapshot.get("DGS2",     0.0) or 0.0
    hike_gap, hike_sig, hike_alert = _rate_hike_signal(dgs2, fedfunds)
    if hike_alert:
        anomalies.append(hike_alert)

    # 3. 美元
    dxy_level, dxy_ret, dollar_sig, dollar_alert = _dollar_signal()
    if dollar_alert:
        anomalies.append(dollar_alert)

    # 4. 通胀预期
    be10y, be_trend, infl_sig, infl_alert = _inflation_signal(snapshot)
    if infl_alert:
        anomalies.append(infl_alert)

    # 综合外部因子得分（各项等权）
    signals = [oil_sig, hike_sig, dollar_sig, infl_sig]
    valid   = [s for s in signals if s != 0.0]
    composite = float(np.mean(valid)) if valid else 0.0

    # 异动惩罚：每项异动叠加 -0.1（最多 -0.3）
    anomaly_score = max(-0.30, -0.10 * len(anomalies))

    # 汇总 reasoning
    parts = [
        f"Oil={oil_price:.1f}({oil_ret:+.0%} 20d sig={oil_sig:+.2f})",
        f"HikeGap=2Y-FF={hike_gap:+.2f}pp(sig={hike_sig:+.2f})",
        f"DXY={dxy_level:.1f}({dxy_ret:+.1%} 20d sig={dollar_sig:+.2f})",
        f"BE10Y={be10y:.2f}%(sig={infl_sig:+.2f})",
        f"composite={composite:+.2f}",
    ]
    if anomalies:
        parts.append(f"ALERTS({len(anomalies)})")
    reasoning = " | ".join(parts)
    logger.info(f"[ExtMacro] {reasoning}")

    return ExternalFactorsResult(
        oil_price      = round(oil_price,  2),
        oil_ret_20d    = round(oil_ret,    4),
        oil_signal     = round(oil_sig,    4),
        fed_funds_rate = round(fedfunds,   4),
        dgs2           = round(dgs2,       4),
        rate_hike_gap  = round(hike_gap,   4),
        rate_hike_signal = round(hike_sig, 4),
        dxy_level      = round(dxy_level,  2),
        dxy_ret_20d    = round(dxy_ret,    4),
        dollar_signal  = round(dollar_sig, 4),
        breakeven_10y  = round(be10y,      4),
        breakeven_trend= round(be_trend,   4),
        inflation_signal = round(infl_sig, 4),
        anomalies      = anomalies,
        anomaly_score  = round(anomaly_score, 4),
        composite_score= round(composite,  4),
        reasoning      = reasoning,
    )
