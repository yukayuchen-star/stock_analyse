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
from config.stocks_ashare import (
    PSEUDO_B2_SCORE, PSEUDO_B2_BREAK_K, PSEUDO_B2_DRYUP_WIN,
    PSEUDO_B2_POS_X, PSEUDO_B2_PUSH_PCT,
)

# 三买中枢新鲜度阈值（交易日）：三买须是突破后的"及时回踩"，
# 中枢末笔距今超过此值即视为旧中枢残影，门控丢弃（仅作用于实盘选股）。
STALE_PIVOT_TD = 25


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


# ── 类二买（lb2）：上涨中枢震荡缩量收口突破（右侧买点）──────────────
# 必要条件（缠论.md「中枢震荡变盘前元信息」）：极度缩量 + BOLL收口 + 快速突破ZG，
# 且现价不追高（≤ZG×(1+X)）。条件全部用 ≤当前 bar 数据，无前视。

def _pb2_arrays(df: pd.DataFrame):
    """预计算类二买条件数组（一次 O(n)，供选股与回测逐 bar O(1) 调用）。"""
    v = df["Volume"].astype(float) if "Volume" in df.columns else None
    if v is not None:
        below_all = ((v < v.rolling(5).mean()) &
                     (v < v.rolling(10).mean()) &
                     (v < v.rolling(20).mean())).to_numpy()
    else:
        below_all = np.zeros(len(df), dtype=bool)
    if {"boll_upper", "boll_lower", "boll_mid"}.issubset(df.columns):
        bw = (df["boll_upper"] - df["boll_lower"]) / df["boll_mid"]
        squeeze = (bw <= bw.rolling(60).quantile(0.20)).to_numpy()
    else:
        squeeze = np.zeros(len(df), dtype=bool)
    close = df["Close"].to_numpy(dtype=float)
    ret   = df["Close"].pct_change().to_numpy()
    return close, below_all, squeeze, ret


def _pb2_trigger(close, below_all, squeeze, ret, idx: int,
                 zg: float, board: str) -> bool:
    """idx 处是否触发类二买（新鲜突破ZG + 缩量 + 收口 + 快速拉升 + 不追高）。"""
    if zg is None or zg <= 0 or idx < 25 or idx >= len(close):
        return False
    K   = PSEUDO_B2_BREAK_K
    X   = PSEUDO_B2_POS_X.get(board, 0.20)
    push = PSEUDO_B2_PUSH_PCT.get(board, 0.03)
    win = PSEUDO_B2_DRYUP_WIN
    price = close[idx]
    if price <= zg:                       return False   # 当前未在中枢上沿之上
    if idx - K < 0 or close[idx - K] > zg: return False   # K 根前已在上方 → 非新鲜突破
    if price > zg * (1 + X):              return False   # 追高护栏
    lo = max(0, idx - win)
    if not below_all[lo:idx + 1].any():   return False   # 极度缩量
    if not squeeze[lo:idx + 1].any():     return False   # BOLL收口（突破前）
    seg = ret[max(0, idx - K + 1):idx + 1]
    if seg.size == 0 or np.nanmax(seg) < push: return False  # 快速拉升中阳
    return True


def _detect_pseudo_b2(df: pd.DataFrame, pivot, board: str) -> tuple[str, float]:
    """实盘选股：当前 bar 是否成立类二买。返回 ("lb2", score) 或 ("none", 0.0)。"""
    if pivot is None:
        return "none", 0.0
    close, below_all, squeeze, ret = _pb2_arrays(df)
    if _pb2_trigger(close, below_all, squeeze, ret, len(close) - 1, pivot.zg, board):
        return "lb2", PSEUDO_B2_SCORE
    return "none", 0.0


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
            # 一买严格门控（一买须在下跌趋势末端，价格处于相对低位）：
            #  (1) 周线非向下 且 KDJ/RSI 底背离确认，二者缺一即弃；
            #  (2) 上涨中继护栏：周线向上且现价已在中枢 ZG 之上 → 实为上涨回调
            #      而非下跌末端一买（_detect_buy 的 b1 兜底分支无价格位置约束），弃。
            zg_now = latest_pivot.zg if latest_pivot else None
            if weekly == "down" or not bdiv:
                gate_note = "b1被门控(周线向下/无底背离)"
                buy_type, raw_score, diverge = "none", 0.0, False
            elif weekly == "up" and zg_now is not None and float(close.iloc[-1]) > zg_now:
                gate_note = "b1被门控(上涨中继:周线up且现价>中枢ZG，非下跌末端一买)"
                buy_type, raw_score, diverge = "none", 0.0, False
        if buy_type == "b2" and weekly == "down":
            gate_note = "b2被门控(周线向下)"
            buy_type, raw_score = "none", 0.0
        # b3（突破中枢）即便周线偏弱也允许，但弱周线下削分；
        # 须中枢新鲜——三买是突破后的"及时回踩"，旧中枢残影/价格已远走不算
        if buy_type == "b3" and latest_pivot is not None:
            pivot_end = max(s.end_date for s in latest_pivot.strokes)
            pivot_floor = (df.index[-STALE_PIVOT_TD]
                           if len(df) >= STALE_PIVOT_TD else df.index[0])
            if pivot_end < pivot_floor:
                gap = int((df.index > pivot_end).sum())
                gate_note = f"b3被门控(中枢陈旧:末笔距今{gap}TD>{STALE_PIVOT_TD})"
                buy_type, raw_score = "none", 0.0

        # ── 右侧路径：左侧买卖点(含被门控掉的 b1/b3)用尽后，检测类二买 ──
        # 中枢震荡缩量收口突破。须上涨语境(周线非向下) + 中枢新鲜；不追高。
        # 「上涨中继 b1」被门控后正好落到这里被重判为 lb2（同一结构的正确归类）。
        if (buy_type == "none" and sell_type == "none"
                and weekly != "down" and latest_pivot is not None):
            lb2_pend  = max(s.end_date for s in latest_pivot.strokes)
            lb2_floor = (df.index[-STALE_PIVOT_TD]
                         if len(df) >= STALE_PIVOT_TD else df.index[0])
            if lb2_pend >= lb2_floor:
                lb2_type, lb2_score = _detect_pseudo_b2(df, latest_pivot, board)
                if lb2_type == "lb2":
                    buy_type, raw_score, diverge = "lb2", lb2_score, False
                    gate_note = (gate_note + " | " if gate_note else "") + "重判为类二买(lb2)"

        # ── 评分修正 ──────────────────────────────────────────
        score = raw_score
        if buy_type == "b1" or sell_type == "s1":
            score *= _trend_weight(trend_type)
        if weekly == "down" and score > 0:
            score *= 0.5
        if buy_type == "b3":
            score *= (0.85 + 0.30 * strength)         # 力度强→加分，弱→削分
        if buy_type == "lb2" and weekly == "neutral":
            score *= 0.85                              # 周线up维持，neutral削分
        score = float(np.clip(score, -1.0, 1.0))

        # confidence：结构完整度 + 指标确认
        confidence = min(len(strokes) / 20.0, 1.0)
        if buy_type == "b1" and bdiv:
            confidence = min(confidence + 0.10, 1.0)
        if buy_type == "b3" and (strength > 0.6 or squeeze):
            confidence = min(confidence + 0.10, 1.0)
        if buy_type == "lb2" and (squeeze or
                                  (latest_pivot is not None
                                   and len(latest_pivot.strokes) in (3, 5))):
            confidence = min(confidence + 0.10, 1.0)   # 收口/简单中枢→加分

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

    # 注：类二买(lb2)为 Watch-only（回测仅 42% 胜率，不交易）→ 不进回测事件流。
    return events
