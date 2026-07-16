from datetime import date, timedelta

import pandas as pd
from loguru import logger

from config.settings import settings
from config.stocks import STOCK_POOL, BENCHMARKS
from data.cache import SQLiteCache
from data.yfinance_source import YFinanceSource
from data.fred_source import FREDSource, FRED_SERIES
from data.finnhub_source import FinnhubSource
from data.alpha_vantage_source import AlphaVantageSource
from utils.time_utils import today_str


class DataPipeline:
    """统一数据编排：各源初始化、获取、缓存透明化。"""

    def __init__(self) -> None:
        self.cache   = SQLiteCache(settings.cache_dir)
        self.yf      = YFinanceSource(self.cache)
        self.fred    = FREDSource(self.cache)
        self.finnhub = FinnhubSource(self.cache)
        self.av      = AlphaVantageSource(self.cache)

    # ── 价格 ──────────────────────────────────────────────

    def get_price(self, ticker: str) -> pd.DataFrame:
        end   = today_str()
        start = (date.today() - timedelta(days=settings.price_history_days)).strftime("%Y-%m-%d")
        return self.yf.get_price(ticker, start, end)

    def get_backtest_price(self, ticker: str) -> pd.DataFrame:
        """获取 backtest_history_days 窗口的价格（P7 回测专用）。"""
        end   = today_str()
        start = (date.today() - timedelta(days=settings.backtest_history_days)).strftime("%Y-%m-%d")
        return self.yf.get_price(ticker, start, end)

    def get_all_prices(self, stock_pool: list[str] | None = None) -> dict[str, pd.DataFrame]:
        """获取全部股票 + 基准的日线数据。stock_pool 为 None 时使用配置默认值。"""
        pool = stock_pool if stock_pool is not None else STOCK_POOL
        if not pool:
            logger.warning("get_all_prices: stock_pool 为空，仅返回基准价格数据")
        result: dict[str, pd.DataFrame] = {}
        for ticker in pool + BENCHMARKS:
            df = self.get_price(ticker)
            if not df.empty:
                result[ticker] = df
            else:
                logger.warning(f"No price data: {ticker}")
        return result

    # ── 新闻 ──────────────────────────────────────────────

    def get_news(self, ticker: str, days: int = 7) -> pd.DataFrame:
        """Finnhub 优先，fallback 到 yfinance。"""
        df = self.finnhub.get_news(ticker, days)
        if df.empty:
            logger.debug(f"Finnhub news empty for {ticker}, fallback to yfinance")
            df = self.yf.get_news(ticker, days)
        return df

    # ── 宏观 ──────────────────────────────────────────────

    def get_macro(self) -> dict[str, pd.DataFrame]:
        """获取全部 FRED 宏观序列。"""
        return self.fred.get_all()

    def get_macro_snapshot(self) -> tuple[dict[str, float], list[str]]:
        """
        返回 (各序列最新值快照, 降级项列表)（R3.2）。
        降级项：序列缺失 → FRED_MISSING:<sid>；数据点过期 → FRED_STALE:<sid>(<n>d)。
        """
        result: dict[str, float] = {}
        degraded: list[str] = []
        for sid in FRED_SERIES:
            dated = self.fred.get_latest_dated(sid)
            if dated is None:
                degraded.append(f"FRED_MISSING:{sid}")
                continue
            val, data_date = dated
            result[sid] = val
            stale = self.fred.staleness(sid, data_date)
            if stale:
                degraded.append(stale)
        if degraded:
            logger.warning(f"[Pipeline] 宏观数据降级 {len(degraded)} 项: {', '.join(degraded)}")
        return result, degraded

    # ── 财报 ──────────────────────────────────────────────

    def get_earnings(self, ticker: str) -> pd.DataFrame:
        return self.av.get_income_quarterly(ticker)

    # ── 基本面 info ────────────────────────────────────────

    def get_fundamentals(self, stock_pool: list[str] | None = None) -> dict[str, dict]:
        """yfinance info 关键财务指标，7 天缓存。stock_pool 为 None 时使用配置默认值。"""
        pool = stock_pool if stock_pool is not None else STOCK_POOL
        result: dict[str, dict] = {}
        for ticker in pool:
            info = self.yf.get_info(ticker)
            if info:
                result[ticker] = info
            else:
                logger.warning(f"No fundamental info: {ticker}")
        return result

    # ── 全量拉取（main.py 入口）──────────────────────────

    def fetch_all(self, stock_pool: list[str] | None = None) -> dict:
        """
        P1 入口：一次性拉取全部数据，返回结构化字典供信号层消费。
        stock_pool 为 None 时使用配置默认值 STOCK_POOL。
        {
            "prices":   dict[ticker, DataFrame],
            "news":     dict[ticker, DataFrame],
            "macro":    dict[series_id, DataFrame],
            "snapshot": dict[series_id, float],
        }
        """
        pool = stock_pool if stock_pool is not None else STOCK_POOL
        logger.info("── 数据层：开始拉取 ──")

        logger.info(f"  价格数据 ({len(pool + BENCHMARKS)} 只)…")
        prices = self.get_all_prices(pool)

        logger.info("  新闻数据…")
        news = {}
        for ticker in pool:
            news[ticker] = self.get_news(ticker)

        logger.info("  宏观数据 (FRED)…")
        macro    = self.get_macro()
        snapshot, macro_degraded = self.get_macro_snapshot()

        logger.info("  基本面数据 (yfinance info)…")
        fundamentals = self.get_fundamentals(pool)

        logger.info(f"── 数据层完成：{len(prices)} 只价格 / {len(macro)} 个宏观序列 / {len(fundamentals)} 只基本面 ──")
        return {"prices": prices, "news": news, "macro": macro, "snapshot": snapshot,
                "macro_degraded": macro_degraded, "fundamentals": fundamentals}
