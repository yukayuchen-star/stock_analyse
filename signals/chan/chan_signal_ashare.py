"""
A 股缠论信号层

复用美股侧忠于缠论的结构引擎（分型→笔→中枢→买卖点），仅做三处 A 股适配：
  1. 背驰用 CSV 预计算的 MACD 柱（`macd` 列 = 2×(dif−dea)）而非重算，
     与用户软件口径一致；面积比值法对缩放因子不敏感，逻辑等价。
  2. 指标确认层：KDJ/RSI 背离辅助背驰判断、CCI/BOLL 确认趋势力度。
     **指标只调整 score/confidence 与门控，绝不独立产生买卖点**（缠论结构为核心）。
  3. 牛短熊长保守门控：以二买/三买为主，一买（左侧背驰抄底）严格门控。

实盘选股用 compute_chan_signal_ashare（带保守门控）；
回测用 extract_chan_events_ashare（不门控，全量发 b1/b2/b3，以便分类型统计胜率）。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from signals.chan.fractal import process_bars, detect_fractals
from signals.chan.stroke  import build_strokes, Stroke
from signals.chan.pivot   import find_latest_pivot, build_all_pivots
from signals.chan.chan_signal import (
    ChanSignalResult, ChanEvent,
    _detect_buy, _detect_sell, _classify_trend, _trend_weight,
    _fractal_stopped, _weekly_trend, _calc_stop_and_r, _macd_hist,
)


# ── MACD 柱：优先用预计算列 ──────────────────────────────────────

def _hist_series(df: pd.DataFrame) -> pd.Series:
    """A 股背驰用 CSV 的 macd 柱列；缺失时回退到重算。"""
    if "macd" in df.columns and df["macd"].notna().any():
        return df["macd"]
    return _macd_hist(df["Close"])


# ── 指标确认层（仅辅助，不造信号）─────────────────────────────────

def _bottom_divergence(df: pd.DataFrame, strokes: List[Stroke]) -> bool:
    """
    底背离辅助：最近两个下跌笔，价格创新低但 KDJ_J / RSI_6 未创新低 → 确认。
    用于一买（底背驰）的二次确认。
    """
    downs = [s for s in strokes if s.direction == "down"]
    if len(downs) < 2:
        return False
    now, prev = downs[-1], downs[-2]
    if now.low >= prev.low:           # 价格未创新低，谈不上底背离
        return False
    try:
        def at(date, col):
            s = df.loc[df.index <= date, col]
            return float(s.iloc[-1]) if not s.empty else np.nan
        j_now, j_prev = at(now.end_date, "kdj_j"), at(prev.end_date, "kdj_j")
        r_now, r_prev = at(now.end_date, "rsi_6"), at(prev.end_date, "rsi_6")
    except Exception:
        return False
    higher_j = np.isfinite(j_now) and np.isfinite(j_prev) and j_now > j_prev
    higher_r = np.isfinite(r_now) and np.isfinite(r_prev) and r_now > r_prev
    return bool(higher_j or higher_r)


def _trend_strength(df: pd.DataFrame) -> float:
    """
    趋势力度 0~1：CCI（动量）+ 收盘在布林带的相对位置。
    用于三买（突破中枢）力度确认。
    """
    last = df.iloc[-1]
    up, mid, lo = last.get("boll_upper"), last.get("boll_mid"), last.get("boll_lower")
    close = float(last["Close"])
    pos = 0.5
    if up is not None and lo is not None and up > lo:
        pos = float(np.clip((close - lo) / (up - lo), 0.0, 1.0))
    cci_raw = last.get("cci", 0.0)
    cci = float(cci_raw) if pd.notna(cci_raw) else 0.0
    cci_s = float(np.clip(cci / 200.0, -1.0, 1.0)) * 0.5 + 0.5   # → 0~1
    return float(0.5 * pos + 0.5 * cci_s)


def _boll_squeeze(df: pd.DataFrame, lookback: int = 60) -> bool:
    """布林带收口：当前带宽处于近 lookback 日最低 20% 分位 → 中阴变盘前兆。"""
    if not {"boll_upper", "boll_lower", "boll_mid"}.issubset(df.columns):
        return False
    bw = ((df["boll_upper"] - df["boll_lower"]) / df["boll_mid"]).dropna()
    if len(bw) < lookback // 2:
        return False
    recent = bw.iloc[-lookback:]
    return bool(bw.iloc[-1] <= recent.quantile(0.20))


# ── 主函数：实盘选股（保守门控）──────────────────────────────────

def compute_chan_signal_ashare(
    code: str,
    df: pd.DataFrame,
    board: str = "main",
) -> ChanSignalResult:
    """
    单只 A 股缠论信号（实盘选股口径，含牛短熊长保守门控）。

    df 需含 High/Low/Close（首字母大写）+ 预计算指标列，DatetimeIndex。
    """
    if df is None or df.empty:
        return ChanSignalResult(ticker=code, timestamp=pd.Timestamp.now(),
                                reasoning="无价格数据")
    if len(df) < 200:
        return ChanSignalResult(ticker=code, timestamp=pd.Timestamp.now(),
                                reasoning=f"数据不足({len(df)}根，需>=200)")

    try:
        pbars    = process_bars(df)
        fractals = detect_fractals(pbars)
        strokes  = build_strokes(fractals)
        if len(strokes) < 3:
            return ChanSignalResult(
                ticker=code, timestamp=pd.Timestamp.now(),
                stroke_count=len(strokes),
                reasoning=f"笔不足({len(strokes)}根) 分型={len(fractals)}")

        latest_pivot = find_latest_pivot(strokes, lookback=12)
        all_pivots   = build_all_pivots(strokes[-30:])
        trend_type   = _classify_trend(all_pivots)

        close  = df["Close"]
        hist   = _hist_series(df)
        weekly = _weekly_trend(df)

        last     = strokes[-1]
        # 新鲜度按交易日计（~12 根，约等于原 15 日历日），避免春节等长假被误判为过期
        fresh_floor = df.index[-12] if len(df) >= 12 else df.index[0]
        is_fresh = last.end_date >= fresh_floor
        fractal_stop = _fractal_stopped(last, pbars, df) if is_fresh else False

        buy_type = sell_type = "none"
        raw_score = 0.0
        diverge   = False
        if is_fresh and fractal_stop:
            buy_type, raw_score, diverge = _detect_buy(
                strokes, latest_pivot, hist, df, close)
            if buy_type == "none":
                sell_type, raw_score, diverge = _detect_sell(
                    strokes, latest_pivot, hist, df, close)

        # ── 指标确认 ──────────────────────────────────────────
        bdiv     = _bottom_divergence(df, strokes)
        strength = _trend_strength(df)
        squeeze  = _boll_squeeze(df)

        # ── 牛短熊长保守门控（仅作用于实盘选股）──────────────
        gate_note = ""
        if buy_type == "b1":
            # 一买严格门控：周线非向下 且 KDJ/RSI 底背离确认，二者缺一即弃
            if weekly == "down" or not bdiv:
                gate_note = "b1被门控(周线向下/无底背离)"
                buy_type, raw_score, diverge = "none", 0.0, False
        if buy_type == "b2" and weekly == "down":
            gate_note = "b2被门控(周线向下)"
            buy_type, raw_score = "none", 0.0
        # b3（突破中枢）即便周线偏弱也允许，但弱周线下削分

        # ── 评分修正 ──────────────────────────────────────────
        score = raw_score
        if buy_type == "b1" or sell_type == "s1":
            score *= _trend_weight(trend_type)
        if weekly == "down" and score > 0:
            score *= 0.5
        if buy_type == "b3":
            score *= (0.85 + 0.30 * strength)         # 力度强→加分，弱→削分
        score = float(np.clip(score, -1.0, 1.0))

        # confidence：结构完整度 + 指标确认
        confidence = min(len(strokes) / 20.0, 1.0)
        if buy_type == "b1" and bdiv:
            confidence = min(confidence + 0.10, 1.0)
        if buy_type == "b3" and (strength > 0.6 or squeeze):
            confidence = min(confidence + 0.10, 1.0)

        resonance = 2 if (score > 0 and weekly == "up") else (1 if score > 0 else 0)

        active_pt = buy_type if buy_type != "none" else sell_type
        stop_loss = r_ratio = None
        if active_pt != "none":
            stop_loss, r_ratio = _calc_stop_and_r(
                active_pt, float(close.iloc[-1]), last, latest_pivot)

        pstr = (f"ZD={latest_pivot.zd:.2f} ZG={latest_pivot.zg:.2f}"
                if latest_pivot else "无中枢")
        point = active_pt if active_pt != "none" else "neutral"
        conf_tag = (f"底背离={'✓' if bdiv else '×'} 力度={strength:.2f}"
                    f" 收口={'✓' if squeeze else '×'}")
        reasoning = (
            f"[{board}] 笔={len(strokes)} {pstr} 周线={weekly} {trend_type} "
            f"末笔={last.direction}{'✓' if is_fresh else '×'}"
            f"{'停顿✓' if fractal_stop else '停顿×'} → {point} "
            f"{conf_tag} res={resonance} score={score:+.2f}"
            + (f" R={r_ratio:.3f}" if r_ratio else "")
            + (f" | {gate_note}" if gate_note else "")
        )
        logger.debug(f"[ChanA] {code}: {reasoning}")

        return ChanSignalResult(
            ticker=code,
            timestamp=pd.Timestamp.now(),
            stroke_count=len(strokes),
            pivot_count=len(latest_pivot.strokes) if latest_pivot else 0,
            buy_point_type=buy_type   if buy_type  != "none" else None,
            sell_point_type=sell_type if sell_type != "none" else None,
            divergence=diverge,
            current_pivot={
                "ZD": latest_pivot.zd, "ZG": latest_pivot.zg,
                "mid": latest_pivot.mid, "strokes": len(latest_pivot.strokes),
            } if latest_pivot else None,
            last_stroke_direction=last.direction,
            weekly_trend=weekly,
            level_resonance=resonance,
            trend_type=trend_type,
            pivot_total=len(all_pivots),
            fractal_stop=fractal_stop,
            stop_loss=stop_loss,
            r_ratio=r_ratio,
            score=score,
            confidence=confidence,
            reasoning=reasoning,
        )

    except Exception as e:
        logger.warning(f"[ChanA] {code} 计算异常: {e}")
        return ChanSignalResult(ticker=code, timestamp=pd.Timestamp.now(),
                                reasoning=f"计算异常: {e}")


# ── 回测用：逐笔历史事件提取（不门控，全量发信号）────────────────

def extract_chan_events_ashare(df: pd.DataFrame) -> List[ChanEvent]:
    """
    与 chan_signal.extract_chan_events 同构，但背驰用预计算 MACD 柱。
    无前视：信号在笔结束后的分型停顿确认日触发，仅依赖 ≤该日数据。
    不做保守门控——回测需要 b1/b2/b3 全量样本以分类型统计胜率。
    """
    if len(df) < 60:
        return []
    try:
        pbars    = process_bars(df)
        fractals = detect_fractals(pbars)
        strokes  = build_strokes(fractals)
    except Exception:
        return []

    full_hist = _hist_series(df)
    events: List[ChanEvent] = []
    STOP_WAIT = 5
    for i in range(3, len(strokes)):
        stroke      = strokes[i]
        sub_strokes = strokes[: i + 1]

        end_f     = stroke.end
        idx_third = end_f.pbar_idx + 1
        if idx_third >= len(pbars):
            continue
        third_date = pbars[idx_third].date
        third_hi   = pbars[idx_third].high
        third_lo   = pbars[idx_third].low

        after = df[df.index > third_date].head(STOP_WAIT)
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

        sub_df = df[df.index <= stop_date]
        if len(sub_df) < 30:
            continue
        sub_close = sub_df["Close"]
        sub_hist  = full_hist.loc[full_hist.index <= stop_date]
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
