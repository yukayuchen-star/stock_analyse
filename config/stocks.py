from typing import Dict, List

BUCKETS: Dict[str, List[str]] = {
    "mega_tech": ["GOOGL", "AAPL", "NVDA", "MSFT", "META"],
    "consumer":  ["AMZN", "TSLA"],
    "hardware":  ["SNDK", "VRT"],
}

STOCK_POOL: List[str] = [t for tickers in BUCKETS.values() for t in tickers]

BENCHMARKS: List[str] = ["QQQ", "SPY", "^VIX", "^TNX"]
