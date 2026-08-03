"""R6.3 FINRA 做空数据源 — 程序化自取日度做空量（**非爬虫**，同 FRED/yfinance 类）。

来源：FINRA RegSHO 日度**全市场**文件（一份含全 ticker，公开、无需鉴权）：
  `https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt`
  管道分隔，表头 `Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market` + 尾行(记录数)。
  **Short Volume Ratio (SVR) = ShortVolume / TotalVolume**（做空流量占比，是**流量**代理≠做空兴趣）。

设计（与既有 `FinnhubSource`/`FREDSource` 同构）：共享 `SQLiteCache` + `with_retry` + TTL + 新鲜度 +
本地文件回退。直接 GET 日期参数化的已发布数据文件，**非 HTML 抓取 / 跨页遍历 → 不是批量爬虫**。

⚠️ 范围（2026-08-02 用户决策「仅 FINRA，FTD 全缓」）：SEC FTD（sec.gov）本环境 TLS 层不可达
（curl+python 均 SSL 复位）→ **逼空风险因子（需 FTD）本期不做**，此模块仅 FINRA 做空量。
过 R6.1 门（`signals/quant/short_sentiment`）才并入实盘打分；不过则如实记录不 merge。
"""
from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd
import requests
from loguru import logger

from data.base import with_retry
from data.cache import SQLiteCache

_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d}.txt"
_UA = "stock-analyse-research (contact: local user)"   # 礼貌标识，非鉴权
_TTL_DAILY = 24 * 7          # 单日历史文件不变 → 长 TTL
_TTL_HIST = 24 * 30          # 聚合宽表 → 30 天 TTL
_STALE_DAYS = 5              # 日频：>5 日历日无新数据视为陈旧（周末+假日容差）
# 本地文件回退目录（离线 / 端点故障时放 CNMSshvol{YYYYMMDD}.txt）；None=不启用
FINRA_LOCAL_DIR: Optional[str] = None


class FinraShortSource:
    """FINRA 日度做空量源：程序化自取 + 缓存 + 本地回退。"""

    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache

    # ── 单日 ─────────────────────────────────────────────
    def _fetch_daily(self, date_str: str) -> pd.DataFrame:
        """取单个交易日全市场做空量 → [Symbol, ShortVolume, TotalVolume, SVR]。非交易日/缺失返回空。"""
        d = date_str.replace("-", "")
        # 本地回退优先（离线）
        if FINRA_LOCAL_DIR:
            p = Path(FINRA_LOCAL_DIR) / f"CNMSshvol{d}.txt"
            if p.exists():
                return self._parse(p.read_text())
        try:
            def _get() -> str:
                r = requests.get(_URL.format(d=d), timeout=30, headers={"User-Agent": _UA})
                if r.status_code != 200:   # 非交易日/未发布 → 空（不抛，避免 with_retry 无谓重试）
                    return ""
                return r.text
            txt = with_retry(_get, label=f"finra:{date_str}")
        except Exception as e:
            logger.debug(f"[FINRA] {date_str} fetch 失败: {e}")
            return pd.DataFrame()
        return self._parse(txt) if txt else pd.DataFrame()

    @staticmethod
    def _parse(txt: str) -> pd.DataFrame:
        """解析管道分隔文本；尾行(记录数)经数值强转天然剔除。"""
        try:
            df = pd.read_csv(StringIO(txt), sep="|")
        except Exception:
            return pd.DataFrame()
        need = {"Symbol", "ShortVolume", "TotalVolume"}
        if not need.issubset(df.columns):
            return pd.DataFrame()
        df = df[["Symbol", "ShortVolume", "TotalVolume"]].copy()
        df["ShortVolume"] = pd.to_numeric(df["ShortVolume"], errors="coerce")
        df["TotalVolume"] = pd.to_numeric(df["TotalVolume"], errors="coerce")
        df = df.dropna(subset=["Symbol", "ShortVolume", "TotalVolume"])
        df = df[df["TotalVolume"] > 0]
        if df.empty:
            return pd.DataFrame()
        df["SVR"] = df["ShortVolume"] / df["TotalVolume"]
        return df.reset_index(drop=True)

    def get_daily(self, date_str: str) -> pd.DataFrame:
        """单日做空量（per-date 缓存；供实盘取近日）。"""
        key = SQLiteCache.make_key("finra_shvol", date_str)
        cached = self.cache.get(key)
        if cached is not None and not cached.empty:
            return cached
        df = self._fetch_daily(date_str)
        if not df.empty:
            self.cache.set(key, df, ttl_hours=_TTL_DAILY)
        return df

    # ── 历史宽表（验证用）─────────────────────────────────
    def get_short_history(
        self, tickers: Iterable[str], start: str, end: str, checkpoint_every: int = 60,
    ) -> pd.DataFrame:
        """区间内逐交易日取 SVR，装配成宽表 [date × ticker]。**缓存聚合宽表**（不缓存全市场原始文件，防 DB 膨胀）。

        **可断点续传**：每 checkpoint_every 个命中日把部分宽表写回同一缓存键；再次调用时从已抓日
        续跑（长下载被会话中断也不重来）。缓存键含 start/end/tickers，参数一致才复用。
        """
        tickers = sorted(set(tickers))
        key = SQLiteCache.make_key("finra_svr_hist", start, end, "|".join(tickers))
        all_days = list(pd.bdate_range(start, end))

        cached = self.cache.get(key)
        recs: dict = {}
        have: set = set()
        if cached is not None and not cached.empty:
            recs = {d: cached.loc[d].dropna().to_dict() for d in cached.index}
            have = set(cached.index)
            if have.issuperset(all_days):     # 全部已抓 → 直接返回
                return cached

        tset = set(tickers)
        since_ckpt = 0
        for dt in all_days:
            if dt in have:                    # 断点续传：跳过已抓日
                continue
            df = self._fetch_daily(dt.strftime("%Y-%m-%d"))
            if df.empty:
                continue
            sub = df[df["Symbol"].isin(tset)]
            if sub.empty:
                continue
            recs[dt] = dict(zip(sub["Symbol"], sub["SVR"]))
            since_ckpt += 1
            if since_ckpt >= checkpoint_every:   # 检查点：部分宽表写回，抗中断
                self.cache.set(key, pd.DataFrame.from_dict(recs, orient="index").sort_index(),
                               ttl_hours=_TTL_HIST)
                since_ckpt = 0
        if not recs:
            logger.warning(f"[FINRA] {start}~{end} 无可用做空数据")
            return pd.DataFrame()
        wide = pd.DataFrame.from_dict(recs, orient="index").sort_index()
        logger.info(f"[FINRA] SVR 宽表 {wide.shape[0]} 日 × {wide.shape[1]} 票")
        self.cache.set(key, wide, ttl_hours=_TTL_HIST)
        return wide

    # ── 实盘新鲜度（供 R3.2 降级区块）──────────────────────
    def staleness(self, data_date: pd.Timestamp) -> Optional[str]:
        age = (pd.Timestamp.now().normalize() - pd.Timestamp(data_date).normalize()).days
        if age > _STALE_DAYS:
            return f"FINRA_STALE:({age}d>{_STALE_DAYS}d)"
        return None
