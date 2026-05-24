from __future__ import annotations

import numpy as np
import pandas as pd


# 只关心这些数值型基本面字段
_METRICS = [
    "revenueGrowth", "earningsGrowth", "earningsQuarterlyGrowth",
    "returnOnEquity", "returnOnAssets", "grossMargins", "operatingMargins",
    "debtToEquity", "pegRatio", "trailingPegRatio",
    "freeCashflow", "operatingCashflow", "totalRevenue", "currentRatio",
    "marketCap", "trailingPE", "forwardPE",
]


def _safe(info: dict, key: str) -> float | None:
    v = info.get(key)
    if v is None:
        return None
    try:
        f = float(v)
        return None if (np.isnan(f) or np.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _quality_subscore(info: dict) -> tuple[float | None, dict]:
    """
    Piotroski 简化 Quality 子分 (-1 ~ 1)。

    三项可获取检验：
      ROA       → (ROA - 5%) / 10%，clip
      OCF/Rev   → 现金流转化率，0.10 = 0，0.30 = +1
      CurrentR  → 流动比率，(CR - 1.0) / 1.0，clip
    """
    parts: list[float] = []
    ind: dict[str, float] = {}

    roa = _safe(info, "returnOnAssets")
    if roa is not None:
        s = float(np.clip((roa - 0.05) / 0.10, -1, 1))
        parts.append(s)
        ind["roa"] = roa

    ocf  = _safe(info, "operatingCashflow")
    rev  = _safe(info, "totalRevenue")
    if ocf is not None and rev is not None and rev > 0:
        ratio = ocf / rev
        s = float(np.clip((ratio - 0.10) / 0.20, -1, 1))
        parts.append(s)
        ind["ocf_rev"] = ratio

    cr = _safe(info, "currentRatio")
    if cr is not None:
        s = float(np.clip((cr - 1.0) / 1.0, -1, 1))
        parts.append(s)
        ind["current_ratio"] = cr

    if not parts:
        return None, {}
    return float(np.mean(parts)), ind


def compute_fundamental_score(ticker: str, info: dict) -> tuple[float, dict]:
    """
    基本面得分 (-1 ~ 1)。

    指标与归一化基准（适配美股大盘成长股）：
      Revenue Growth  22%  → 20% YoY = +1
      EPS Growth      22%  → 25% YoY = +1
      ROE             16%  → 40%以上 = +1，15% = 0
      Gross Margin    13%  → 65%以上 = +1，30% = 0
      Debt Safety      8%  → D/E=0 = +1，D/E=150 = -1
      PEG              4%  → PEG=0.5 = +1，PEG=3.5 = -1
      Quality         15%  → Piotroski 简化：ROA + OCF/Rev + CurrentRatio

    缺失字段自动降权，不影响其他因子。
    """
    if not info:
        return 0.0, {}

    scores: dict[str, float] = {}
    weights: dict[str, float] = {}

    # ── Revenue Growth ────────────────────────────────────
    v = _safe(info, "revenueGrowth")
    if v is not None:
        scores["rev_growth"] = float(np.clip(v / 0.20, -1, 1))
        weights["rev_growth"] = 0.22

    # ── EPS Growth ────────────────────────────────────────
    v = _safe(info, "earningsGrowth") or _safe(info, "earningsQuarterlyGrowth")
    if v is not None:
        scores["eps_growth"] = float(np.clip(v / 0.25, -1, 1))
        weights["eps_growth"] = 0.22

    # ── ROE ───────────────────────────────────────────────
    v = _safe(info, "returnOnEquity")
    if v is not None:
        # 15% → 0, 40% → +1, <0 → 负分
        scores["roe"] = float(np.clip((v - 0.15) / 0.25, -1, 1))
        weights["roe"] = 0.16

    # ── Gross Margin ──────────────────────────────────────
    v = _safe(info, "grossMargins")
    if v is not None:
        # 30% → 0, 65% → +1
        scores["gross_margin"] = float(np.clip((v - 0.30) / 0.35, -1, 1))
        weights["gross_margin"] = 0.13

    # ── Debt Safety ───────────────────────────────────────
    v = _safe(info, "debtToEquity")
    if v is not None:
        # yfinance 返回的 debtToEquity 单位为百分比（如 45 = 45%）
        scores["debt_safety"] = float(np.clip(1.0 - v / 150.0, -1, 1))
        weights["debt_safety"] = 0.08

    # ── PEG ───────────────────────────────────────────────
    v = _safe(info, "pegRatio") or _safe(info, "trailingPegRatio")
    if v is not None and v > 0:
        # PEG 0.5 → +1, PEG 2.0 → 0, PEG 3.5 → -1
        scores["peg"] = float(np.clip((2.0 - v) / 1.5, -1, 1))
        weights["peg"] = 0.04

    # ── Quality（Piotroski 简化）─────────────────────────
    q_score, q_ind = _quality_subscore(info)
    if q_score is not None:
        scores["quality"] = q_score
        weights["quality"] = 0.15

    if not scores:
        return 0.0, {}

    total_w = sum(weights.values())
    final = sum(scores[k] * weights[k] for k in scores) / total_w

    return float(np.clip(final, -1, 1)), {
        **scores,
        **q_ind,
        "coverage": round(len(scores) / 7, 2),
    }
