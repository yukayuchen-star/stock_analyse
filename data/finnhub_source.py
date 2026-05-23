from datetime import datetime, timedelta

import finnhub
import pandas as pd
from loguru import logger

from data.cache import SQLiteCache
from config.settings import settings


class FinnhubSource:
    """新闻情绪 + 盈利日历（60次/分钟免费）。"""

    TTL_NEWS     = 24   # 新闻缓存 1 天
    TTL_CALENDAR = 24   # 盈利日历缓存 1 天

    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache
        self._client: finnhub.Client | None = None

    def _get_client(self) -> finnhub.Client:
        if self._client is None:
            self._client = finnhub.Client(
                api_key=settings.finnhub.key.get_secret_value()
            )
        return self._client

    def is_available(self) -> bool:
        try:
            self._get_client()
            return True
        except Exception:
            return False

    # ── 新闻 ──────────────────────────────────────────────

    def get_news(self, ticker: str, days: int = 7) -> pd.DataFrame:
        key = self.cache.make_key("finnhub_news", ticker, days)
        cached = self.cache.get(key)
        if cached is not None:
            logger.debug(f"Cache hit: Finnhub news {ticker}")
            return cached

        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)

        try:
            raw = self._get_client().company_news(
                ticker,
                _from=start_dt.strftime("%Y-%m-%d"),
                to=end_dt.strftime("%Y-%m-%d"),
            )
        except Exception as e:
            logger.warning(f"Finnhub news error [{ticker}]: {e}")
            return pd.DataFrame()

        if not raw:
            return pd.DataFrame()

        rows = [{
            "datetime": pd.Timestamp(n.get("datetime", 0), unit="s"),
            "headline": n.get("headline", ""),
            "sentiment": n.get("sentiment", {}).get("score", 0.0) if n.get("sentiment") else 0.0,
            "source":   n.get("source", ""),
            "url":      n.get("url", ""),
        } for n in raw]

        df = pd.DataFrame(rows).sort_values("datetime", ascending=False)
        self.cache.set(key, df, ttl_hours=self.TTL_NEWS)
        logger.debug(f"Fetched Finnhub news: {ticker} ({len(df)} articles)")
        return df

    # ── 盈利日历 ──────────────────────────────────────────

    def get_earnings_calendar(self, ticker: str) -> pd.DataFrame:
        key = self.cache.make_key("finnhub_earnings", ticker)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        try:
            end_dt = datetime.now() + timedelta(days=90)
            start_dt = datetime.now() - timedelta(days=30)
            raw = self._get_client().earnings_calendar(
                symbol=ticker,
                _from=start_dt.strftime("%Y-%m-%d"),
                to=end_dt.strftime("%Y-%m-%d"),
            )
            earnings_list = raw.get("earningsCalendar", [])
            if not earnings_list:
                return pd.DataFrame()
            df = pd.DataFrame(earnings_list)
            self.cache.set(key, df, ttl_hours=self.TTL_CALENDAR)
            return df
        except Exception as e:
            logger.warning(f"Finnhub earnings calendar error [{ticker}]: {e}")
            return pd.DataFrame()

    # ── DataSource Protocol 填充 ──────────────────────────

    def get_price(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        return pd.DataFrame()

    def get_macro(self, series_id: str) -> pd.DataFrame:
        return pd.DataFrame()
