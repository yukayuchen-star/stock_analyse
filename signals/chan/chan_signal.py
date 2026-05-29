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
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from signals.chan.fractal import process_bars, detect_fractals, PBar
from signals.chan.stroke  import build_strokes, Stroke
from signals.chan.pivot   import find_latest_pivot, build_all_pivots, Pivot


# ── 右端稳定性参数（A 定笔确认 + C' 波动率护栏）──────────────────
# A：末笔终点分型需再过 STROKE_CONFIRM_BARS 根处理K线才算"定笔"，否则右端可能重画，不发信号。
STROKE_CONFIRM_BARS    = 2
# C'：近20日均振幅 ≥ HIGH_VOL_PCT 视为高波动名，额外要求 HIGH_VOL_EXTRA_CONFIRM 根定笔确认 + 标记。
HIGH_VOL_PCT           = 0.06
HIGH_VOL_EXTRA_CONFIRM = 2


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

    # 结构性质（缠论：中枢数 ≥2 为趋势，=1 为盘整）
    trend_type:    str = "none"             # "trend"|"consolidation"|"none"
    pivot_total:   int = 0                  # 最近窗口内中枢总数
    fractal_stop:  bool = False             # 末笔分型是否完成停顿确认
    stroke_confirmed: bool = True           # A: 末笔是否"定笔"（终点分型再过 N 根才确认，反右端重画）
    atr_pct:       float = 0.0              # C': 近20日均振幅(High-Low)/Close；高=日线结构噪声大

    # 实战参数（仅在有买/卖信号时填充）
    stop_loss: Optional[float] = None       # 止损价
    r_ratio:   Optional[float] = None       # (entry - stop) / entry，正数

    # 综合
    score:      float = 0.0
    confidence: float = 0.0
    reasoning:  str   = ""


# ── P7 回测用：历史信号事件提取 ──────────────────────────────

@dataclass
class ChanEvent:
    """单个缠论买/卖信号事件（回测专用，无前视偏差）。"""
    date:        pd.Timestamp
    signal_type: str    # "b1"|"b2"|"b3"|"s1"|"s2"|"s3"
    price:       float
    score:       float


def extract_chan_events(df: pd.DataFrame) -> List[ChanEvent]:
    """
    对完整历史 DataFrame 进行一次笔结构分析，
    在每根笔完成时（仅用截至该日的数据）提取信号事件。

    无前视偏差：信号在笔结束日触发，仅依赖该日及之前数据。
    用于 P7 回测引擎，返回按日期升序排列的事件列表。
    """
    if len(df) < 60:
        return []
    try:
        pbars    = process_bars(df)
        fractals = detect_fractals(pbars)
        strokes  = build_strokes(fractals)
    except Exception:
        return []

    events: List[ChanEvent] = []
    STOP_WAIT = 5  # 分型形成后最多等 5 个交易日做停顿确认
    for i in range(3, len(strokes)):
        stroke      = strokes[i]
        sub_strokes = strokes[: i + 1]

        # 找停顿确认日：分型第三根 PBar 之后第一根 close 站住的 raw bar
        end_f      = stroke.end
        idx_third  = end_f.pbar_idx + 1
        if idx_third >= len(pbars):
            continue
        third_date = pbars[idx_third].date
        third_hi   = pbars[idx_third].high
        third_lo   = pbars[idx_third].low

        after = df[(df.index > third_date)].head(STOP_WAIT)
        if after.empty:
            continue
        if end_f.kind == "bottom":
            mask = after["Close"] >= third_hi
        elif end_f.kind == "top":
            mask = after["Close"] <= third_lo
        else:
            continue
        if not mask.any():
            continue
        stop_date = after.index[mask.argmax()]

        sub_df    = df[df.index <= stop_date]
        if len(sub_df) < 30:
            continue
        sub_close = sub_df["Close"]
        sub_hist  = _macd_hist(sub_close)
        pivot     = find_latest_pivot(sub_strokes, lookback=12)
        trend     = _classify_trend(build_all_pivots(sub_strokes[-30:]))

        buy_type, raw_score, _ = _detect_buy(
            sub_strokes, pivot, sub_hist, sub_df, sub_close)
        if buy_type != "none":
            if buy_type == "b1":
                raw_score *= _trend_weight(trend)
            events.append(ChanEvent(stop_date, buy_type,
                                    float(sub_close.iloc[-1]), raw_score))
            continue

        sell_type, raw_score, _ = _detect_sell(
            sub_strokes, pivot, sub_hist, sub_df, sub_close)
        if sell_type != "none":
            if sell_type == "s1":
                raw_score *= _trend_weight(trend)
            events.append(ChanEvent(stop_date, sell_type,
                                    float(sub_close.iloc[-1]), raw_score))

    return events


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


# ── 分型停顿法（缠论原文第四章）─────────────────────────────
# 底分型停顿：底分型后再有一根 K 线，收盘价站住"第三根 K 线"的高点
# 顶分型停顿：顶分型后再有一根 K 线，收盘价跌破"第三根 K 线"的低点
def _fractal_stopped(
    last_stroke: Stroke,
    pbars: List[PBar],
    df: pd.DataFrame,
) -> bool:
    end_fractal = last_stroke.end
    idx_third   = end_fractal.pbar_idx + 1   # 分型的第三根处理K线
    if idx_third >= len(pbars):
        return False
    third_pb = pbars[idx_third]

    # 第三根 PBar 之后的所有原始 K 线：任意一根收盘站住即视为停顿确认
    # 笔结构本身保证未创新低/新高（否则末笔会重画），故只需检查站位
    after_close = df.loc[df.index > third_pb.date, "Close"]
    if after_close.empty:
        return False

    if end_fractal.kind == "bottom":
        return bool((after_close >= third_pb.high).any())
    if end_fractal.kind == "top":
        return bool((after_close <= third_pb.low).any())
    return False


# ── 走势类型分类（缠论原文："中枢数≥2 为趋势，=1 为盘整"）──
def _classify_trend(pivots: List[Pivot]) -> str:
    n = len(pivots)
    if n >= 2:
        return "trend"
    if n == 1:
        return "consolidation"
    return "none"


def _trend_weight(trend_type: str) -> float:
    """趋势背驰更可靠 ×1.15，盘整背驰弱 ×0.85，仅作用于 1 类买卖点。"""
    if trend_type == "trend":
        return 1.15
    if trend_type == "consolidation":
        return 0.85
    return 1.0


# ── 止损价 + R 比率（缠论原文"R=（买入价-止损价）/买入价"）──
_STOP_BUFFER = 0.01  # 1% 缓冲，防止贴边假突破触发

def _calc_stop_and_r(
    point_type: str,
    entry: float,
    last_stroke: Stroke,
    pivot: Optional[Pivot],
) -> tuple[Optional[float], Optional[float]]:
    stop: Optional[float] = None
    if point_type == "b1" or point_type == "b2":
        stop = last_stroke.low * (1 - _STOP_BUFFER)
    elif point_type in ("b3", "lb2") and pivot is not None:
        # lb2(类二买)与 b3 同：突破失败跌回中枢，止损设在中枢上沿 ZG 下方
        stop = pivot.zg * (1 - _STOP_BUFFER)
    elif point_type == "s1" or point_type == "s2":
        stop = last_stroke.high * (1 + _STOP_BUFFER)
    elif point_type == "s3" and pivot is not None:
        stop = pivot.zd * (1 + _STOP_BUFFER)

    if stop is None or entry <= 0:
        return None, None
    # 买点 r = (entry - stop) / entry > 0；卖点 r = (stop - entry) / entry > 0
    r = abs(entry - stop) / entry
    return round(stop, 4), round(r, 4)


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

        # ── 4. 中枢（最近 + 全部）─────────────────────────────
        latest_pivot = find_latest_pivot(strokes, lookback=12)
        all_pivots   = build_all_pivots(strokes[-30:])  # 走势类型只看近 30 笔
        trend_type   = _classify_trend(all_pivots)

        # ── 5. MACD & 周线 ────────────────────────────────────
        close   = df["Close"]
        hist    = _macd_hist(close)
        weekly  = _weekly_trend(df)

        # ── 6. 买卖点（仅最后一笔在近15个交易日内时生效）─────
        last = strokes[-1]
        cutoff   = df.index[-1] - pd.Timedelta(days=15)
        is_fresh = last.end_date >= cutoff

        # 分型停顿确认（缠论第四章核心过滤）
        fractal_stop = _fractal_stopped(last, pbars, df) if is_fresh else False

        # ── C'：波动率护栏（近20日均振幅）──────────────────────
        # 高波动名(如±10%/日)右端笔极不稳，提高定笔门槛并标记，避免隔夜甩动。
        rng = ((df["High"] - df["Low"]) / df["Close"].replace(0, np.nan)).tail(20)
        atr_pct = float(rng.mean()) if rng.notna().any() else 0.0
        high_vol = atr_pct >= HIGH_VOL_PCT

        # ── A：定笔确认（末笔终点分型须再过 confirm_bars 根，反右端重画）──
        # 右端最后一笔在新K线到来时常被重新切分；未"定笔"前不发信号，从源头压制隔夜翻转。
        confirm_bars   = STROKE_CONFIRM_BARS + (HIGH_VOL_EXTRA_CONFIRM if high_vol else 0)
        bars_since_end = (len(pbars) - 1) - last.end.pbar_idx
        stroke_confirmed = bars_since_end >= confirm_bars

        buy_type  = sell_type = "none"
        raw_score = 0.0
        diverge   = False

        if is_fresh and fractal_stop and stroke_confirmed:
            buy_type, raw_score, diverge = _detect_buy(
                strokes, latest_pivot, hist, df, close)
            if buy_type == "none":
                sell_type, raw_score, diverge = _detect_sell(
                    strokes, latest_pivot, hist, df, close)

        # ── 7. 趋势/盘整修正（仅作用于 1 类背驰）& 周线 & 共振 ──
        score = raw_score
        if buy_type == "b1" or sell_type == "s1":
            score *= _trend_weight(trend_type)

        if weekly == "down" and score > 0:
            score *= 0.5

        resonance = 0
        if score > 0 and weekly == "up":
            resonance = 2
        elif score > 0:
            resonance = 1

        score      = float(np.clip(score, -1.0, 1.0))
        confidence = min(len(strokes) / 20.0, 1.0)

        # ── 8. 止损 + R 比率 ──────────────────────────────────
        active_pt = buy_type if buy_type != "none" else sell_type
        stop_loss, r_ratio = (None, None)
        if active_pt != "none":
            stop_loss, r_ratio = _calc_stop_and_r(
                active_pt, float(close.iloc[-1]), last, latest_pivot)

        # ── 9. 描述 ───────────────────────────────────────────
        pstr  = (f"ZD={latest_pivot.zd:.1f} ZG={latest_pivot.zg:.1f}"
                 if latest_pivot else "无中枢")
        point = active_pt if active_pt != "none" else "neutral"
        rstr  = f" stop={stop_loss} R={r_ratio:.3f}" if r_ratio else ""
        confirm_tag = ("定笔✓" if stroke_confirmed
                       else f"未定笔(右端{bars_since_end}/{confirm_bars})")
        vol_tag = f" 高波动{atr_pct:.0%}" if high_vol else ""
        reasoning = (
            f"笔={len(strokes)} {pstr} 周线={weekly} {trend_type} "
            f"末笔={last.direction}{'✓' if is_fresh else '×'}"
            f"{'停顿✓' if fractal_stop else '停顿×'} {confirm_tag}{vol_tag} "
            f"→ {point} div={diverge} res={resonance} score={score:+.2f}{rstr}"
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
            trend_type=trend_type,
            pivot_total=len(all_pivots),
            fractal_stop=fractal_stop,
            stroke_confirmed=stroke_confirmed,
            atr_pct=atr_pct,
            stop_loss=stop_loss,
            r_ratio=r_ratio,
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
