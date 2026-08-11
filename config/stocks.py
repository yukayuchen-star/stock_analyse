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
# 总持仓上限：任何时点持仓市值 ≤ 该比例 × 当前权益（其余留现金）。
# 仅约束新买入，不强制卖出因升值漂过上限的赢家。用户定：严格执行，总仓≤60%、始终留≥40%现金。
MAX_PORTFOLIO_EXPOSURE    = 0.60
# 分批建仓颗粒度：每个「确认买点」只买该票目标仓位的这一比例，累加至目标为止。
# 1/3=需三次确认买点才把一只票加满目标（防一次性顶满）。事件驱动，无时间要求：
# 无买点则不动，新买点由「结构止损位移」判定（同一买点滞留不重复加）。用户定 2026-08-11。
PORTFOLIO_TRANCHE_FRACTION = 1.0 / 3.0
