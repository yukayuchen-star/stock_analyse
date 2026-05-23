"""
缠论信号层主模块（P4）

策略：日K线级别缠论择时
  - 分型→笔→中枢 结构识别（fractal/stroke/pivot）
  - MACD柱面积背驰判断 b1/s1
  - 中枢价格位置判断 b2/b3/s2/s3
  - 周线SMA20过滤（日线数据重采样），多头信号在周线下跌时×0.5

得分约定（与 QuantSignalResult 同一空间 -1~1）：
  B1=+0.50  B2=+0.75  B3=+0.65
  S1=-0.50  S2=-0.65  S3=-0.70

参考：HKUDS/Vibe-Trading（包含关系+笔去噪）+ 缠论.md（买卖点）
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from signals.chan.fractal import process_bars, detect_fractals
from signals.chan.stroke  import build_strokes, Stroke
from signals.chan.pivot   import find_latest_pivot, Pivot


# ── 结果数据类 ────────────────────────────────────────────────

@dataclass
class ChanSignalResult:
    """
    缠论择时引擎输出。

    score(-1~1)：正多负空，与 QuantSignalResult.score 同一尺度。
    confidence(0~1)：结构完整度（笔/中枢数量越多越高）。
    """
    ticker: str
    timestamp: pd.Timestamp

    # 结构统计
    stroke_count: int = 0
    pivot_count:  int = 0           # 最近中枢包含的笔数

    # 信号
    buy_point_type:  Optional[str] = None   # "b1"|"b2"|"b3"
    sell_point_type: Optional[str] = None   # "s1"|"s2"|"s3"
    divergence:      bool = False

    # 中枢
    current_pivot: Optional[Dict] = None   # {"ZD":, "ZG":, "mid":, "strokes":}

    # 级别
    last_stroke_direction: str = "unknown"  # "up"|"down"
    weekly_trend:          str = "neutral"  # "up"|"down"|"neutral"
    level_resonance:       int = 0          # 0-2（日线+周线共振数）

    # 综合
    score:      float = 0.0
    confidence: float = 0.0
    reasoning:  str   = ""


def placeholder_chan_signal(ticker: str) -> ChanSignalResult:
    return ChanSignalResult(
        ticker=ticker,
        timestamp=pd.Timestamp.now(),
        score=0.0,
        confidence=0.0,
        reasoning="[缠论模块 P4 待实现]",
    )


# ── MACD 工具 ─────────────────────────────────────────────────

def _macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.Series:
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    return macd - macd.ewm(span=sig, adjust=False).mean()


def _stroke_area(stroke: Stroke, df: pd.DataFrame, hist: pd.Series) -> float:
    """笔内 MACD 柱绝对值之和（背驰对比用）。"""
    mask = (df.index >= stroke.start_date) & (df.index <= stroke.end_date)
    return float(hist[mask].abs().sum())


# ── 周线过滤 ──────────────────────────────────────────────────

def _weekly_trend(df: pd.DataFrame) -> str:
    """用日线收盘重采样到周线，计算 SMA20W 判断趋势。"""
    try:
        wclose = df["Close"].resample("W").last().dropna()
        if len(wclose) < 21:
            return "neutral"
        sma20 = float(wclose.rolling(20).mean().iloc[-1])
        latest = float(wclose.iloc[-1])
        if pd.isna(sma20):
            return "neutral"
        if latest > sma20 * 1.02:
            return "up"
        if latest < sma20 * 0.98:
            return "down"
        return "neutral"
    except Exception:
        return "neutral"


# ── 买卖点检测 ────────────────────────────────────────────────

def _detect_buy(
    strokes: List[Stroke],
    pivot:   Optional[Pivot],
    hist:    pd.Series,
    df:      pd.DataFrame,
    close:   pd.Series,
) -> tuple[str, float, bool]:
    """
    返回 (buy_type, raw_score, divergence)。
    优先级：B3 > B2 > B1。
    """
    last  = strokes[-1]
    price = float(close.iloc[-1])

    # ── B3：前一笔（上涨）突破ZG，末笔（下跌）回踩仍在ZG附近 ──
    # 约束：末笔低点在 ZG 的 20% 以内，防止旧中枢产生虚假信号
    if pivot and last.direction == "down" and len(strokes) >= 2:
        prev = strokes[-2]
        if (prev.direction == "up" and
                prev.high > pivot.zg and
                last.low >= pivot.zg * 0.99 and
                last.low <= pivot.zg * 1.20 and
                price >= pivot.zg * 0.99):
            return "b3", 0.65, False

    # ── B2：价格在中枢内或接近ZD回调，MACD近期曾正值 ──────
    # 约束：价格必须在 ZG 以下（≤ZG×1.05），防止价格远超中枢的假二买
    if pivot and last.direction == "down":
        recent_max = float(hist.iloc[-15:].max()) if len(hist) >= 15 else 0.0
        if (last.low >= pivot.zd * 0.99 and
                price >= pivot.zd and
                price <= pivot.zg * 1.05 and   # 价格在中枢内或刚刚突破，不是远超
                recent_max > 0):
            return "b2", 0.75, False

    # ── B1：下跌笔MACD面积背驰（当前面积 < 前一次 × 0.8）─────
    down_strokes = [s for s in strokes if s.direction == "down"]
    if len(down_strokes) >= 2 and last.direction == "down":
        curr_area = _stroke_area(last,             df, hist)
        prev_area = _stroke_area(down_strokes[-2], df, hist)
        if prev_area > 1e-6 and curr_area < prev_area * 0.8:
            return "b1", 0.50, True

    return "none", 0.0, False


def _detect_sell(
    strokes: List[Stroke],
    pivot:   Optional[Pivot],
    hist:    pd.Series,
    df:      pd.DataFrame,
    close:   pd.Series,
) -> tuple[str, float, bool]:
    """返回 (sell_type, raw_score, divergence)。score 已为负数。"""
    last  = strokes[-1]
    price = float(close.iloc[-1])

    # ── S3：价格跌破中枢ZD ─────────────────────────────────
    if pivot and last.direction == "down":
        if price < pivot.zd * 1.01 and last.low < pivot.zd:
            return "s3", -0.70, False

    # ── S2：反弹上升笔未过ZG，当前价格在中枢中轴下方 ──────
    if pivot and last.direction == "down" and len(strokes) >= 2:
        prev_up = strokes[-2]
        if prev_up.direction == "up" and prev_up.high <= pivot.zg * 1.02:
            if price <= pivot.mid:
                return "s2", -0.65, False

    # ── S1：上升笔MACD面积背驰 ────────────────────────────
    up_strokes = [s for s in strokes if s.direction == "up"]
    if len(up_strokes) >= 2 and last.direction == "up":
        curr_area = _stroke_area(last,          df, hist)
        prev_area = _stroke_area(up_strokes[-2], df, hist)
        if prev_area > 1e-6 and curr_area < prev_area * 0.8:
            return "s1", -0.50, True

    return "none", 0.0, False


# ── 主函数 ────────────────────────────────────────────────────

def compute_chan_signal(
    ticker: str,
    prices: Dict[str, pd.DataFrame],
) -> ChanSignalResult:
    """
    为单只股票计算缠论信号。

    Args:
        ticker: 股票代码
        prices: 全池价格字典 {ticker: DataFrame}（需含 High/Low/Close 列）
    """
    df = prices.get(ticker)
    if df is None or df.empty:
        logger.warning(f"[Chan] 无价格数据: {ticker}")
        return ChanSignalResult(ticker=ticker, timestamp=pd.Timestamp.now(),
                                reasoning="无价格数据")

    # 200 raw bars → ~150 processed bars，至少能形成稳定笔/中枢结构
    # 推荐 550+ TD（price_history_days=800），可达 ~413 processed bars
    if len(df) < 200:
        return ChanSignalResult(ticker=ticker, timestamp=pd.Timestamp.now(),
                                reasoning=f"数据不足({len(df)}根，需>=200，建议550+)")

    try:
        # ── 1-3. 结构识别 ─────────────────────────────────────
        pbars    = process_bars(df)
        fractals = detect_fractals(pbars)
        strokes  = build_strokes(fractals)

        if len(strokes) < 3:
            return ChanSignalResult(
                ticker=ticker, timestamp=pd.Timestamp.now(),
                stroke_count=len(strokes),
                reasoning=f"笔不足({len(strokes)}根，需>=3) 分型={len(fractals)}",
            )

        # ── 4. 中枢 ───────────────────────────────────────────
        latest_pivot = find_latest_pivot(strokes, lookback=12)

        # ── 5. MACD & 周线 ────────────────────────────────────
        close   = df["Close"]
        hist    = _macd_hist(close)
        weekly  = _weekly_trend(df)

        # ── 6. 买卖点（仅最后一笔在近15个交易日内时生效）─────
        last = strokes[-1]
        cutoff   = df.index[-1] - pd.Timedelta(days=15)
        is_fresh = last.end_date >= cutoff

        buy_type  = sell_type = "none"
        raw_score = 0.0
        diverge   = False

        if is_fresh:
            buy_type, raw_score, diverge = _detect_buy(
                strokes, latest_pivot, hist, df, close)
            if buy_type == "none":
                sell_type, raw_score, diverge = _detect_sell(
                    strokes, latest_pivot, hist, df, close)

        # ── 7. 周线过滤 & 共振 ────────────────────────────────
        score = raw_score
        if weekly == "down" and score > 0:
            score *= 0.5

        resonance = 0
        if score > 0 and weekly == "up":
            resonance = 2
        elif score > 0:
            resonance = 1

        score      = float(np.clip(score, -1.0, 1.0))
        confidence = min(len(strokes) / 20.0, 1.0)

        # ── 8. 描述 ───────────────────────────────────────────
        pstr  = (f"ZD={latest_pivot.zd:.1f} ZG={latest_pivot.zg:.1f}"
                 if latest_pivot else "无中枢")
        point = (buy_type if buy_type != "none"
                 else sell_type if sell_type != "none"
                 else "neutral")
        reasoning = (
            f"笔={len(strokes)} {pstr} 周线={weekly} "
            f"末笔={last.direction}{'✓' if is_fresh else '×'} "
            f"→ {point} div={diverge} res={resonance} score={score:+.2f}"
        )
        logger.debug(f"[Chan] {ticker}: {reasoning}")

        return ChanSignalResult(
            ticker=ticker,
            timestamp=pd.Timestamp.now(),
            stroke_count=len(strokes),
            pivot_count=len(latest_pivot.strokes) if latest_pivot else 0,
            buy_point_type=buy_type   if buy_type  != "none" else None,
            sell_point_type=sell_type if sell_type != "none" else None,
            divergence=diverge,
            current_pivot={
                "ZD":      latest_pivot.zd,
                "ZG":      latest_pivot.zg,
                "mid":     latest_pivot.mid,
                "strokes": len(latest_pivot.strokes),
            } if latest_pivot else None,
            last_stroke_direction=last.direction,
            weekly_trend=weekly,
            level_resonance=resonance,
            score=score,
            confidence=confidence,
            reasoning=reasoning,
        )

    except Exception as e:
        logger.warning(f"[Chan] {ticker} 计算异常: {e}")
        return ChanSignalResult(
            ticker=ticker, timestamp=pd.Timestamp.now(),
            stroke_count=0,
            reasoning=f"计算异常: {e}",
        )
