from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from loguru import logger

from data.base  import with_retry
from data.cache import SQLiteCache


_OHLC = ("Open", "High", "Low", "Close")


def drop_incomplete_bars(df: pd.DataFrame, ticker: str = "") -> pd.DataFrame:
    """丢弃 OHLC 不全的 K 线行（Yahoo 的未完成尾行占位）。

    **为什么必须在数据源层过滤**（2026-08-29 实测坐实）：Yahoo 会在当日收盘数据尚未
    落地时先返回一行**只有 Volume、OHLC 全为 NaN 的占位行**。它不报错、不缺列，
    下游谁都察觉不到，但会**静默改写缠论的末笔**——实测同一份数据去掉该行后
    MSFT 从 `buy_point=None(+0.00)` 变为 **b3(+0.75)**、META 从 `None` 变为 **s3(−0.70)**。
    这类缺陷会同时打中所有共用本缓存的管线（战术 + 核心），所以**两侧对账全绿也发现不了**，
    只能在入口处堵住。它还会经 `Signal(price=nan)` → `if s.price > 0` 过滤 →
    `_snapshot` 的成本价兜底，把 paper 组合的市值冻结在成本上。

    **判据 = OHLC 必须齐全**（不只看 Close）：缠论的分型/包含关系吃的是 High/Low，
    一根只有 Close 的残行同样会算出垃圾结构。宁可少一根真实交易日，
    也不要让一根残行悄悄改写末笔——前者可见（本函数会告警），后者不可见。

    实测基线（2026-08-29，46 只 × 800/1825 天双窗口共 22,434 行）：命中 22 行，
    **全部是 OHLC 四项皆 NaN 的尾行占位，内部缺口 0 行** → 本过滤当时不丢弃任何真实交易日。
    """
    if df is None or df.empty:
        return df
    cols = [c for c in _OHLC if c in df.columns]
    if not cols:
        return df
    bad = df[cols].isna().any(axis=1)
    if not bad.any():
        return df

    kept = df[~bad]
    # 内部缺口（被丢弃行之后仍有有效行）与纯尾行占位是两回事：前者意味着历史被改写，
    # 属于必须有人看一眼的异常；后者是每天都会遇到的正常现象。故分级告警。
    last_ok = kept.index[-1] if not kept.empty else None
    interior = int(bad.loc[:last_ok].sum()) if last_ok is not None else int(bad.sum())
    dates = ", ".join(str(d)[:10] for d in df.index[bad][-3:])
    if interior:
        logger.warning(f"[{ticker}] 丢弃 OHLC 不全的 K 线 {int(bad.sum())} 根"
                       f"（其中 **{interior} 根位于序列内部**，历史被改写，请核查）: {dates}")
    else:
        logger.debug(f"[{ticker}] 丢弃未完成尾行占位 K {int(bad.sum())} 根: {dates}")
    return kept


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
            # 缓存命中也要过滤：占位行可能是**上一次运行**存进去的（TTL 24h），
            # 只在下载路径过滤会让已污染的缓存条目继续毒害整整一天。
            return drop_incomplete_bars(cached, ticker)

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
        df = drop_incomplete_bars(df, ticker)   # 先净化再入缓存，避免污染次日
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

    # ── 下次财报日 ────────────────────────────────────────
    TTL_EARNINGS = 24   # calendar 是公司确认前会漂移的**估计日**，按日刷新

    def get_next_earnings(self, ticker: str) -> str | None:
        """yfinance calendar 的下次财报日（YYYY-MM-DD）；ETF / 取不到返回 None。

        「无财报日」也写缓存（空串）：否则 ETF 与失败名每轮都会重新发一次请求。
        但**异常不写缓存**——网络抖动不应把一只有财报的票标成「无」整整一天。
        """
        key = self.cache.make_key("yf_next_earnings", ticker)
        cached = self.cache.get(key)
        if cached is not None:
            v = cached.iloc[0]["next_earnings"]
            # 缓存经 JSON 往返，空串可能回来成 NaN/None —— str(nan) 是真值 "nan"
            return str(v)[:10] if pd.notna(v) and str(v) else None
        try:
            cal = yf.Ticker(ticker).calendar
            ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
            v = str(ed[0])[:10] if ed else ""
        except Exception as e:
            logger.warning(f"yfinance calendar error [{ticker}]: {e}")
            return None
        self.cache.set(key, pd.DataFrame([{"next_earnings": v}]), ttl_hours=self.TTL_EARNINGS)
        return v or None

    TTL_EARNINGS_HIST = 24 * 7   # 已发生的财报日是历史事实，一周刷新一次足够

    def get_earnings_history(self, ticker: str, limit: int = 28) -> list[str]:
        """已发生的财报日（美东本地日期 YYYY-MM-DD，升序）；取不到返回 []。

        只收 `Reported EPS` 非空的行 —— 未来的估计日也在同一张表里，不能混进「已发生」。
        缓存/异常策略同 `get_next_earnings`：空结果也缓存，异常不缓存。
        """
        key = self.cache.make_key("yf_earnings_hist", ticker, str(limit))
        cached = self.cache.get(key)
        if cached is not None:
            return sorted(str(d)[:10] for d in cached["date"] if pd.notna(d) and str(d))
        try:
            df = yf.Ticker(ticker).get_earnings_dates(limit=limit)
        except Exception as e:
            logger.warning(f"yfinance earnings_dates error [{ticker}]: {e}")
            return []
        dates: list[str] = []
        if df is not None and not df.empty and "Reported EPS" in df.columns:
            dates = sorted({str(ix)[:10] for ix in df.index[df["Reported EPS"].notna()]})
        self.cache.set(key, pd.DataFrame({"date": dates or [""]}), ttl_hours=self.TTL_EARNINGS_HIST)
        return dates

    TTL_REVISIONS = 24

    def get_eps_revisions(self, ticker: str) -> dict:
        """分析师 EPS 一致预期的快照：`eps_trend`（当前 vs 30/90 天前）+ `eps_revisions`（30 天上/下修家数）。

        只有**当日快照**，Yahoo 不给历史 —— 所以这些数只能向前逐日记录，不能回测。
        返回 {period: {current,d30,d90,up30,down30}}，period ∈ {0q, 0y}；取不到返回 {}。
        """
        key = self.cache.make_key("yf_eps_revisions", ticker)
        cached = self.cache.get(key)
        if cached is not None:
            return {r["period"]: {k: (None if pd.isna(r[k]) else float(r[k]))
                                  for k in ("current", "d30", "d90", "up30", "down30")}
                    for _, r in cached.iterrows() if r["period"]}
        try:
            t = yf.Ticker(ticker)
            trend, rev = t.eps_trend, t.eps_revisions
        except Exception as e:
            logger.warning(f"yfinance eps_trend error [{ticker}]: {e}")
            return {}
        rows = []
        for p in ("0q", "0y"):
            tr = trend.loc[p] if trend is not None and not trend.empty and p in trend.index else {}
            rv = rev.loc[p] if rev is not None and not rev.empty and p in rev.index else {}
            if len(tr) == 0 and len(rv) == 0:
                continue
            rows.append({"period": p, "current": tr.get("current"), "d30": tr.get("30daysAgo"),
                         "d90": tr.get("90daysAgo"), "up30": rv.get("upLast30days"),
                         "down30": rv.get("downLast30days")})
        df = pd.DataFrame(rows or [{"period": "", "current": None, "d30": None, "d90": None,
                                    "up30": None, "down30": None}])
        self.cache.set(key, df, ttl_hours=self.TTL_REVISIONS)
        return {r["period"]: {k: (None if pd.isna(r[k]) else float(r[k]))
                              for k in ("current", "d30", "d90", "up30", "down30")}
                for _, r in df.iterrows() if r["period"]}

    # ── 宏观（不支持）────────────────────────────────────

    def get_macro(self, series_id: str) -> pd.DataFrame:
        return pd.DataFrame()
