from typing import Dict, List

BUCKETS: Dict[str, List[str]] = {
    "mega_tech": ["GOOGL", "AAPL", "NVDA", "MSFT", "META"],
    "consumer":  ["AMZN", "TSLA"],
    "hardware":  ["SNDK", "VRT"],
}

STOCK_POOL: List[str] = [t for tickers in BUCKETS.values() for t in tickers]

BENCHMARKS: List[str] = ["QQQ", "SPY", "^VIX", "^TNX"]

# ── 模拟组合（paper-trading，从启用日起按策略信号前向模拟）──────
PORTFOLIO_INITIAL_CAPITAL = 100_000   # 美股初始资金 $10万
PORTFOLIO_LOT_SIZE        = 1         # 美股按股交易，无整手限制
# ── R9 双 Sleeve 架构（2026-08-18 用户确认）─────────────────
# Core 70%：长持核心仓（基本面+估值+择时，advisory 手动执行，不进 paper 引擎）；
# Tactical 30%：现有缠论×宏观×量化 swing 引擎（paper 组合语义收敛为仅战术 sleeve）。
CORE_HOLDINGS: List[str] = ["NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", "META", "QQQ"]
CORE_TARGET_FRAC   = 0.70   # 核心仓目标占总资金比例（当前已建 ~20%，持续摊低成本补满）
BASE_FLOOR_FRAC    = 0.70   # 每名底仓下限 = 该比例 × 已建仓位；其上为增强层（可高抛低吸）
CORE_LEDGER_PATH   = "output/core_ledger.json"   # 真金台账（用户维护，skill 成本类结论的锚）

# 总持仓上限：任何时点持仓市值 ≤ 该比例 × 当前权益（其余留现金）。
# 仅约束新买入，不强制卖出因升值漂过上限的赢家。
# R9（2026-08-18 用户确认）：0.60 → 0.30——paper 组合语义收敛为「仅 30% 战术 sleeve」，
# 核心 70% 走 advisory 手动执行不进本引擎；核心名从 tactical 买入候选排除（main.py）。
MAX_PORTFOLIO_EXPOSURE    = 0.30
# 分批建仓颗粒度：每个「确认买点」只买该票目标仓位的这一比例，累加至目标为止。
# 1/3=需三次确认买点才把一只票加满目标（防一次性顶满）。事件驱动，无时间要求：
# 无买点则不动，新买点由「结构止损位移」判定（同一买点滞留不重复加）。用户定 2026-08-11。
PORTFOLIO_TRANCHE_FRACTION = 1.0 / 3.0
