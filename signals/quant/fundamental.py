from __future__ import annotations

import numpy as np
import pandas as pd


# 只关心这些数值型基本面字段
_METRICS = [
    "revenueGrowth", "earningsGrowth", "earningsQuarterlyGrowth",
    "returnOnEquity", "grossMargins", "operatingMargins",
    "debtToEquity", "pegRatio", "trailingPegRatio",
    "freeCashflow", "marketCap", "trailingPE", "forwardPE",
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


def compute_fundamental_score(ticker: str, info: dict) -> tuple[float, dict]:
    """
    基本面得分 (-1 ~ 1)。

    指标与归一化基准（适配美股大盘成长股）：
      Revenue Growth  25%  → 20% YoY = +1
      EPS Growth      25%  → 25% YoY = +1
      ROE             20%  → 40%以上 = +1，15% = 0
      Gross Margin    15%  → 65%以上 = +1，30% = 0
      Debt Safety     10%  → D/E=0 = +1，D/E=150 = -1
      PEG             5%   → PEG=0.5 = +1，PEG=3.5 = -1

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
        weights["rev_growth"] = 0.25

    # ── EPS Growth ────────────────────────────────────────
    v = _safe(info, "earningsGrowth") or _safe(info, "earningsQuarterlyGrowth")
    if v is not None:
        scores["eps_growth"] = float(np.clip(v / 0.25, -1, 1))
        weights["eps_growth"] = 0.25

    # ── ROE ───────────────────────────────────────────────
    v = _safe(info, "returnOnEquity")
    if v is not None:
        # 15% → 0, 40% → +1, <0 → 负分
        scores["roe"] = float(np.clip((v - 0.15) / 0.25, -1, 1))
        weights["roe"] = 0.20

    # ── Gross Margin ──────────────────────────────────────
    v = _safe(info, "grossMargins")
    if v is not None:
        # 30% → 0, 65% → +1
        scores["gross_margin"] = float(np.clip((v - 0.30) / 0.35, -1, 1))
        weights["gross_margin"] = 0.15

    # ── Debt Safety ───────────────────────────────────────
    v = _safe(info, "debtToEquity")
    if v is not None:
        # yfinance 返回的 debtToEquity 单位为百分比（如 45 = 45%）
        scores["debt_safety"] = float(np.clip(1.0 - v / 150.0, -1, 1))
        weights["debt_safety"] = 0.10

    # ── PEG ───────────────────────────────────────────────
    v = _safe(info, "pegRatio") or _safe(info, "trailingPegRatio")
    if v is not None and v > 0:
        # PEG 0.5 → +1, PEG 2.0 → 0, PEG 3.5 → -1
        scores["peg"] = float(np.clip((2.0 - v) / 1.5, -1, 1))
        weights["peg"] = 0.05

    if not scores:
        return 0.0, {}

    total_w = sum(weights.values())
    final = sum(scores[k] * weights[k] for k in scores) / total_w

    return float(np.clip(final, -1, 1)), {**scores, "coverage": round(len(scores) / 6, 2)}
