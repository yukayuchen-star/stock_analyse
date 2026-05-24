"""
Universe 筛选器 — 从 S&P500 + Nasdaq Top30 中找加仓候选，从动态池中找减仓候选

策略：
  - add_candidates: 不在当前池、通过预过滤、quant 分数 ≥ ADD_TH 的 Top-N
  - remove_candidates: 在 dynamic_pool（非 core）、quant 分数 ≤ REMOVE_TH 的全部

预过滤（避免对 500 只全跑信号）：
  1. 价格 ≥ $5（剔除低价股）
  2. 日均成交额 ≥ $20M（流动性下限）
  3. 市值 ≥ $5B（剔除小盘）

筛选只是给"建议"，最终由用户在交互编辑器中确认（与 _interactive_pool_editor 配合）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf
from loguru import logger

from signals.quant.factor_engine import compute_quant_signal, QuantSignalResult
from signals.chan.chan_signal     import ChanSignalResult


# ── 阈值常量 ────────────────────────────────────────────────────
ADD_THRESHOLD       = 0.40   # quant 分数 ≥ 此值才进入 add 候选
REMOVE_THRESHOLD    = -0.20  # quant 分数 ≤ 此值进入 remove 候选
TOP_N_ADD           = 5      # add 候选最多保留几只
SCREEN_LOOKBACK_DAYS = 250   # 预筛阶段下载多少日历日的价格

MIN_PRICE           = 5.0
MIN_DOLLAR_VOLUME   = 20_000_000   # 20M 日均成交额
MIN_MARKET_CAP      = 5_000_000_000  # 5B 市值


@dataclass
class ScreeningCandidate:
    ticker:    str
    score:     float
    reasoning: str
    action:    str   # "add" | "remove"


# ── 批量价格抓取 ────────────────────────────────────────────────

def _batch_download_prices(
    tickers: List[str],
    days: int = SCREEN_LOOKBACK_DAYS,
) -> Dict[str, pd.DataFrame]:
    """批量拉取价格，返回 {ticker: df}（剔除空数据）。"""
    if not tickers:
        return {}

    end   = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    logger.info(f"[Screen] 批量下载 {len(tickers)} 只 × {days}d 价格 ...")
    try:
        raw = yf.download(
            tickers, start=start, end=end,
            auto_adjust=True, group_by="ticker", progress=False, threads=True,
        )
    except Exception as e:
        logger.warning(f"[Screen] 批量下载失败: {e}")
        return {}

    out: Dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for t in tickers:
            if t in raw.columns.get_level_values(0):
                sub = raw[t].dropna(how="all")
                if not sub.empty and "Close" in sub.columns:
                    out[t] = sub
    else:
        # 单 ticker 时 yfinance 返回扁平列
        if not raw.empty:
            out[tickers[0]] = raw

    logger.info(f"[Screen] 价格抓取完成: {len(out)} 只有效")
    return out


# ── 预过滤 ──────────────────────────────────────────────────────

def _passes_prefilter(df: pd.DataFrame, market_cap: float | None) -> bool:
    if df is None or df.empty or "Close" not in df.columns:
        return False
    close = float(df["Close"].iloc[-1])
    if close < MIN_PRICE:
        return False
    vol = df["Volume"].tail(20).mean() if "Volume" in df.columns else 0
    if vol * close < MIN_DOLLAR_VOLUME:
        return False
    if market_cap is not None and market_cap < MIN_MARKET_CAP:
        return False
    return True


def _parallel_get_info(
    pipeline,
    tickers: List[str],
    max_workers: int = 10,
) -> Dict[str, dict]:
    """并行批量取 info（pipeline.yf.get_info 内部带 7 天缓存，缓存命中近 0 成本）。"""
    out: Dict[str, dict] = {}
    if not tickers:
        return out
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(pipeline.yf.get_info, t): t for t in tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                out[t] = fut.result() or {}
            except Exception:
                out[t] = {}
    return out


# ── 主入口 ──────────────────────────────────────────────────────

def screen_for_adds(
    pipeline,
    universe: List[str],
    exclude_pool: List[str],
    benchmarks: List[str],
    top_n: int = TOP_N_ADD,
    add_threshold: float = ADD_THRESHOLD,
) -> List[ScreeningCandidate]:
    """
    从 universe 中（剔除 exclude_pool）找 quant 分数最高的 Top-N。

    流程：
      1. 候选 = universe - exclude_pool
      2. 批量下载 250d 价格
      3. 预过滤（价格 / 流动性）
      4. 取 marketCap 过滤小盘
      5. 取 info 跑 quant
      6. 按 score 降序，取 top_n（score ≥ add_threshold）
    """
    candidates = [t for t in universe if t not in exclude_pool]
    if not candidates:
        return []

    # 1. 批量价格（含基准，rel 因子需要）
    fetch_list = candidates + [b for b in benchmarks if b not in candidates]
    prices = _batch_download_prices(fetch_list)

    # 2. 价格 / 成交额预筛
    pre_pass = [t for t in candidates if _passes_prefilter(prices.get(t), None)]
    logger.info(f"[Screen] 价格/成交额过滤: {len(candidates)} → {len(pre_pass)}")

    # 3. 并行批量取 info（首日抓取 + 之后 7 天缓存命中）
    logger.info(f"[Screen] 并行抓取 info {len(pre_pass)} 只 (10路) ...")
    infos = _parallel_get_info(pipeline, pre_pass, max_workers=10)

    # 4. 市值过滤（用 info 自带的 marketCap，不再单独 fast_info 调用）
    survivors = [
        t for t in pre_pass
        if float(infos.get(t, {}).get("marketCap") or 0) >= MIN_MARKET_CAP
    ]
    logger.info(f"[Screen] 市值过滤 (≥${MIN_MARKET_CAP/1e9:.0f}B): {len(pre_pass)} → {len(survivors)}")

    if not survivors:
        return []

    # 5. 跑量化信号（peer 用全 survivors，做横截面）
    scored: List[ScreeningCandidate] = []
    for t in survivors:
        try:
            r = compute_quant_signal(t, prices, survivors, infos.get(t, {}))
        except Exception as e:
            logger.debug(f"[Screen] {t} quant 异常: {e}")
            continue
        if r.score >= add_threshold:
            scored.append(ScreeningCandidate(
                ticker=t, score=r.score, action="add",
                reasoning=r.reasoning,
            ))

    scored.sort(key=lambda c: c.score, reverse=True)
    result = scored[:top_n]
    logger.info(
        f"[Screen] add 候选: {len(scored)} 只达标 (≥{add_threshold:+.2f})，"
        f"取 Top-{top_n}: {[c.ticker for c in result]}"
    )
    return result


def screen_for_removes(
    quant_results: Dict[str, QuantSignalResult],
    chan_results:  Dict[str, ChanSignalResult],
    dynamic_pool:  List[str],
    remove_threshold: float = REMOVE_THRESHOLD,
) -> List[ScreeningCandidate]:
    """
    从 dynamic_pool 中找 quant 分数 ≤ remove_threshold 或 chan 出现卖点的减仓候选。

    core_pool 不参与（永不被自动移除）。
    """
    cands: List[ScreeningCandidate] = []
    for t in dynamic_pool:
        q = quant_results.get(t)
        c = chan_results.get(t)
        if q is None:
            continue

        is_weak  = q.score <= remove_threshold
        has_sell = c is not None and c.sell_point_type is not None and c.score < 0

        if is_weak or has_sell:
            reasoning_parts = []
            if is_weak:
                reasoning_parts.append(f"quant={q.score:+.2f}")
            if has_sell:
                reasoning_parts.append(f"chan={c.sell_point_type}({c.score:+.2f})")
            cands.append(ScreeningCandidate(
                ticker=t,
                score=q.score,
                action="remove",
                reasoning=" ".join(reasoning_parts),
            ))

    cands.sort(key=lambda c: c.score)   # 最弱的排前面
    logger.info(
        f"[Screen] remove 候选 (dynamic 池中): "
        f"{[(c.ticker, round(c.score, 2)) for c in cands]}"
    )
    return cands
