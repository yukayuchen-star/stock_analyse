from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from loguru import logger

from data.base  import with_retry
from data.cache import SQLiteCache


class YFinanceSource:
    """主数据源：OHLCV、新闻、基本面概览（免费无限制）。"""

    TTL_PRICE = 24      # 日线价格缓存 1 天
    TTL_NEWS  = 24      # 新闻缓存 1 天

    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache

    def is_available(self) -> bool:
        return True

    # ── 价格 ──────────────────────────────────────────────

    def get_price(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        key = self.cache.make_key("yf_price", ticker, start, end)
        cached = self.cache.get(key)
        if cached is not None:
            logger.debug(f"Cache hit: price {ticker}")
            return cached

        try:
            df = with_retry(
                lambda: yf.download(
                    ticker, start=start, end=end,
                    auto_adjust=True, progress=False, multi_level_index=False,
                ),
                label=f"yf.download({ticker})",
            )
        except Exception as e:
            logger.warning(f"yfinance download error [{ticker}]: {e}")
            return pd.DataFrame()

        if df.empty:
            logger.warning(f"yfinance: empty price data for {ticker}")
            return df

        # 兼容 yfinance >= 0.2.38 的 MultiIndex 列
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.index = pd.to_datetime(df.index)
        self.cache.set(key, df, ttl_hours=self.TTL_PRICE)
        logger.debug(f"Fetched price: {ticker} ({len(df)} rows)")
        return df

    # ── 新闻 ──────────────────────────────────────────────

    def get_news(self, ticker: str, days: int = 7) -> pd.DataFrame:
        key = self.cache.make_key("yf_news", ticker, days)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        try:
            t = yf.Ticker(ticker)
            raw = t.news or []
        except Exception as e:
            logger.warning(f"yfinance news error [{ticker}]: {e}")
            return pd.DataFrame()

        rows = []
        for n in raw:
            ts = n.get("providerPublishTime") or n.get("pubDate", 0)
            rows.append({
                "datetime": pd.Timestamp(ts, unit="s") if isinstance(ts, (int, float)) else pd.Timestamp(ts),
                "headline": n.get("title", ""),
                "sentiment": 0.0,
                "source": n.get("publisher", ""),
            })

        df = pd.DataFrame(rows)
        if not df.empty:
            self.cache.set(key, df, ttl_hours=self.TTL_NEWS)
        return df

    # ── 基本面 info ────────────────────────────────────────

    # 仅缓存这些数值型字段，避免 JSON 序列化复杂类型
    _INFO_FIELDS = [
        "revenueGrowth", "earningsGrowth", "earningsQuarterlyGrowth",
        "returnOnEquity", "returnOnAssets", "grossMargins", "operatingMargins",
        "debtToEquity", "pegRatio", "trailingPegRatio",
        "freeCashflow", "operatingCashflow", "totalRevenue", "currentRatio",
        "marketCap", "trailingPE", "forwardPE",
        "trailingEps", "forwardEps",
        "averageVolume", "averageVolume10days",  # 流动性预过滤
    ]
    TTL_INFO = 24 * 7   # 基本面数据缓存 7 天

    def get_info(self, ticker: str) -> dict:
        """获取 yfinance Ticker.info 中的关键财务指标（7 天缓存）。"""
        key = self.cache.make_key("yf_info", ticker)
        cached = self.cache.get(key)
        if cached is not None:
            logger.debug(f"Cache hit: info {ticker}")
            return cached.iloc[0].to_dict()

        try:
            raw = yf.Ticker(ticker).info or {}
            filtered = {k: raw[k] for k in self._INFO_FIELDS if k in raw and raw[k] is not None}
            if not filtered:
                return {}
            df = pd.DataFrame([filtered])
            self.cache.set(key, df, ttl_hours=self.TTL_INFO)
            logger.debug(f"Fetched info: {ticker} ({len(filtered)} fields)")
            return filtered
        except Exception as e:
            logger.warning(f"yfinance info error [{ticker}]: {e}")
            return {}

    # ── 宏观（不支持）────────────────────────────────────

    def get_macro(self, series_id: str) -> pd.DataFrame:
        return pd.DataFrame()
