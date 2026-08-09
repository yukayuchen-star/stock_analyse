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
