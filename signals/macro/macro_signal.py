from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict

import numpy as np
import pandas as pd
from loguru import logger

from signals.macro.regime          import classify_vix, VIXRegime
from signals.macro.sector_strength import compute_all_bucket_ir


# ── 宏观得分权重 ──────────────────────────────────────────────
W_VIX    = 0.50   # VIX 制度（主导因子）
W_YIELD  = 0.30   # 10Y-2Y 利差（利率环境）
W_BUCKET = 0.20   # 桶相对 QQQ 的 IR（行业强度）

# 利差正常化锚点（±1.5% spread → ±1 score）
YIELD_NORM = 1.5


@dataclass
class MacroSignalResult:
    """
    宏观风险门控输出。

    score (-1~1) = 0.50×vix_score + 0.30×yield_score + 0.20×bucket_avg_score
    position_limit 由 VIX 制度直接决定，优先于 score。
    """
    timestamp: pd.Timestamp

    # VIX
    vix_level:      float = 0.0
    vix_regime:     str   = "unknown"   # "calm"|"neutral"|"tense"|"panic"
    position_limit: float = 0.7         # VIX 四档仓位上限
    vix_score:      float = 0.0         # -1~1

    # 利率曲线
    dgs10:        float = 0.0
    dgs2:         float = 0.0
    yield_spread: float = 0.0           # 10Y - 2Y（%）
    yield_score:  float = 0.0           # -1~1

    # 桶强度（vs QQQ）
    bucket_ir:     Dict[str, float] = field(default_factory=dict)   # 年化 IR
    bucket_scores: Dict[str, float] = field(default_factory=dict)   # -1~1

    # 综合
    score:     float = 0.0   # -1~1
    reasoning: str   = ""


# ── 主函数 ────────────────────────────────────────────────────

def compute_macro_signal(
    snapshot: Dict[str, float],
    prices:   Dict[str, pd.DataFrame],
    buckets:  Dict[str, list],
) -> MacroSignalResult:
    """
    计算宏观信号。

    Args:
        snapshot: FRED 最新值快照 {series_id: float}，含 VIXCLS / DGS10 / DGS2
        prices:   全股票池价格字典（含 QQQ），用于桶强度计算
        buckets:  BUCKETS 配置 {bucket_name: [ticker, ...]}
    """
    # ── 1. VIX 制度 ──────────────────────────────────────────
    vix = snapshot.get("VIXCLS")
    if vix is None:
        logger.warning("[Macro] VIXCLS 缺失，使用默认 VIX=20")
        vix = 20.0

    regime: VIXRegime = classify_vix(vix)
    logger.debug(f"[Macro] VIX={vix:.1f} → {regime.regime} pos_limit={regime.position_limit:.0%}")

    # ── 2. 利率曲线 ──────────────────────────────────────────
    dgs10 = snapshot.get("DGS10")
    dgs2  = snapshot.get("DGS2")

    if dgs10 is None or dgs2 is None:
        logger.warning("[Macro] 国债收益率数据缺失，yield_score=0")
        spread      = 0.0
        yield_score = 0.0
    else:
        spread      = float(dgs10 - dgs2)
        yield_score = float(np.clip(spread / YIELD_NORM, -1.0, 1.0))
        logger.debug(f"[Macro] 10Y={dgs10:.2f}% 2Y={dgs2:.2f}% spread={spread:+.2f}% yield_score={yield_score:+.2f}")

    # ── 3. 桶强度 vs QQQ ─────────────────────────────────────
    bucket_ir, bucket_scores = compute_all_bucket_ir(buckets, prices, lookback=60)
    bucket_avg = float(np.mean(list(bucket_scores.values()))) if bucket_scores else 0.0

    # ── 4. 综合得分 ──────────────────────────────────────────
    score = float(np.clip(
        W_VIX    * regime.score
        + W_YIELD  * yield_score
        + W_BUCKET * bucket_avg,
        -1.0, 1.0,
    ))

    # ── 5. 描述 ──────────────────────────────────────────────
    bucket_str = " ".join(f"{k}={v:+.2f}" for k, v in bucket_scores.items())
    reasoning  = (
        f"VIX={vix:.1f}({regime.regime}) vix_score={regime.score:+.2f} | "
        f"10Y-2Y={spread:+.2f}% yield_score={yield_score:+.2f} | "
        f"buckets=[{bucket_str}] avg={bucket_avg:+.2f} "
        f"→ macro_score={score:+.3f} pos_limit={regime.position_limit:.0%}"
    )
    logger.info(f"[Macro] {reasoning}")

    return MacroSignalResult(
        timestamp      = pd.Timestamp.now(),
        vix_level      = round(vix, 2),
        vix_regime     = regime.regime,
        position_limit = regime.position_limit,
        vix_score      = regime.score,
        dgs10          = round(dgs10 or 0.0, 4),
        dgs2           = round(dgs2  or 0.0, 4),
        yield_spread   = round(spread, 4),
        yield_score    = round(yield_score, 4),
        bucket_ir      = bucket_ir,
        bucket_scores  = bucket_scores,
        score          = round(score, 4),
        reasoning      = reasoning,
    )


def placeholder_macro_signal() -> MacroSignalResult:
    return MacroSignalResult(
        timestamp   = pd.Timestamp.now(),
        vix_regime  = "unknown",
        position_limit = 0.7,
        reasoning   = "[宏观信号模块 P3 待实现]",
    )
