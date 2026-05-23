import pandas as pd
from fredapi import Fred
from loguru import logger

from data.cache import SQLiteCache
from config.settings import settings

# 我们关注的 FRED 序列
FRED_SERIES: dict[str, str] = {
    "FEDFUNDS": "Fed Funds Rate",
    "DGS10":    "10Y Treasury Yield",
    "DGS2":     "2Y Treasury Yield",
    "CPIAUCSL": "CPI (All Urban)",
    "UNRATE":   "Unemployment Rate",
    "T10YIE":   "10Y Breakeven Inflation",
    "VIXCLS":   "VIX (CBOE)",
}

TTL_MACRO = 24  # 宏观数据缓存 1 天


class FREDSource:
    """宏观数据源：美联储经济数据库（免费无限制）。"""

    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache
        self._fred: Fred | None = None

    def _get_client(self) -> Fred:
        if self._fred is None:
            self._fred = Fred(api_key=settings.fred.key.get_secret_value())
        return self._fred

    def is_available(self) -> bool:
        try:
            self._get_client()
            return True
        except Exception:
            return False

    # ── 宏观数据 ──────────────────────────────────────────

    def get_macro(self, series_id: str) -> pd.DataFrame:
        key = self.cache.make_key("fred", series_id)
        cached = self.cache.get(key)
        if cached is not None:
            logger.debug(f"Cache hit: FRED {series_id}")
            return cached

        try:
            s = self._get_client().get_series(series_id)
            df = pd.DataFrame({"value": s})
            df.index = pd.to_datetime(df.index)
            df = df.dropna()
            self.cache.set(key, df, ttl_hours=TTL_MACRO)
            logger.debug(f"Fetched FRED {series_id}: {len(df)} rows, latest={df['value'].iloc[-1]:.4f}")
            return df
        except Exception as e:
            logger.error(f"FRED error [{series_id}]: {e}")
            return pd.DataFrame()

    def get_latest(self, series_id: str) -> float | None:
        """返回最新值（常用快捷方法）。"""
        df = self.get_macro(series_id)
        if df.empty:
            return None
        return float(df["value"].iloc[-1])

    def get_all(self) -> dict[str, pd.DataFrame]:
        """批量获取所有关注序列。"""
        return {sid: self.get_macro(sid) for sid in FRED_SERIES}

    # ── DataSource Protocol 填充 ──────────────────────────

    def get_price(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        return pd.DataFrame()

    def get_news(self, ticker: str, days: int = 7) -> pd.DataFrame:
        return pd.DataFrame()
