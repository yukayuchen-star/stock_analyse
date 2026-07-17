"""Alpha Vantage 财报数据源。

保留原因（R4.4 注记）：实盘主流程（main.py 每日选股）不调用本源——基本面走
yfinance info 快照；本源经 pipeline.get_earnings 提供**季度财报时间序列**，是未来
实现「财报+2月延迟」PIT 回测基本面的唯一入口，删除即断路，故保留不删。
"""
import pandas as pd
from loguru import logger

from data.cache import SQLiteCache
from config.settings import settings

TTL_EARNINGS = 24 * 90  # 财报季度数据缓存 90 天


class AlphaVantageSource:
    """基本面数据（财报）备用源，500次/天。主力用于季度财报，大量缓存。"""

    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache
        self._fd = None

    def _get_client(self):
        if self._fd is None:
            from alpha_vantage.fundamentaldata import FundamentalData
            self._fd = FundamentalData(
                key=settings.alpha_vantage.key.get_secret_value(),
                output_format="pandas",
            )
        return self._fd

    def is_available(self) -> bool:
        key = settings.alpha_vantage.key.get_secret_value()
        return bool(key)

    # ── 财报 ──────────────────────────────────────────────

    def get_income_quarterly(self, ticker: str) -> pd.DataFrame:
        """季度利润表，90 天缓存。返回含 fiscalDateEnding 列的 DataFrame。"""
        key = self.cache.make_key("av_income_q", ticker)
        cached = self.cache.get(key)
        if cached is not None:
            logger.debug(f"Cache hit: AV income {ticker}")
            return cached

        try:
            df, _ = self._get_client().get_income_statement_quarterly(ticker)
            if df.empty:
                return df
            if "fiscalDateEnding" in df.columns:
                df["fiscalDateEnding"] = pd.to_datetime(df["fiscalDateEnding"])
                df = df.sort_values("fiscalDateEnding", ascending=False)
            self.cache.set(key, df, ttl_hours=TTL_EARNINGS)
            logger.info(f"Fetched AV income quarterly: {ticker} ({len(df)} quarters)")
            return df
        except Exception as e:
            logger.error(f"Alpha Vantage income error [{ticker}]: {e}")
            return pd.DataFrame()

    def get_balance_quarterly(self, ticker: str) -> pd.DataFrame:
        """季度资产负债表，90 天缓存。"""
        key = self.cache.make_key("av_balance_q", ticker)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        try:
            df, _ = self._get_client().get_balance_sheet_quarterly(ticker)
            self.cache.set(key, df, ttl_hours=TTL_EARNINGS)
            return df
        except Exception as e:
            logger.error(f"Alpha Vantage balance error [{ticker}]: {e}")
            return pd.DataFrame()

    # ── DataSource Protocol 填充 ──────────────────────────

    def get_price(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        return pd.DataFrame()  # 用 yfinance

    def get_news(self, ticker: str, days: int = 7) -> pd.DataFrame:
        return pd.DataFrame()  # 用 Finnhub

    def get_macro(self, series_id: str) -> pd.DataFrame:
        return pd.DataFrame()  # 用 FRED
