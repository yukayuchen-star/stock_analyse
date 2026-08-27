"""
R9.2 核心持仓基本面数据层：SEC EDGAR 优先 + yfinance 回退链。

设计（PRD R9.2）：
- 主路径 = SEC 官方 JSON API（data.sec.gov submissions / companyfacts，非爬虫）；
- ⚠️ 2026-08-18 实测本机网络 sec.gov TLS 握手即被重置（与 R6.3 FTD 同因，SEC 对部分
  海外 IP 段 reset，网络级硬约束）→ 运行时主力是回退链：
  yfinance `Ticker.sec_filings`（EDGAR 元数据镜像，含 8-K/10-K/10-Q + edgarUrl）
  + `income_stmt` / `quarterly_income_stmt`（5 年年报 / 5 季季报，含 Diluted EPS）。
- EDGAR 直连保留：探测一次（短超时），可达即用官方源（未来换网/VPN 自动升级）。
- 缓存：SQLiteCache（DataFrame 存储）；filings 24h、财务 72h。
- 降级如实标注（degraded 列表），绝不静默。
"""
from __future__ import annotations

import json

import pandas as pd
import requests
import yfinance as yf
from loguru import logger

from data.cache import SQLiteCache

# SEC 公平访问要求：声明 User-Agent（app 名 + 联系邮箱）、≤10 req/s（本用量远低于限）
EDGAR_USER_AGENT = "stock_analyse research fmbbs92fgr@privaterelay.appleid.com"
_EDGAR_TIMEOUT   = 8   # 秒；本网络不可达时快速失败进回退链

TTL_FILINGS = 24        # 小时
TTL_FIN     = 72        # 财务报表季频更新，3 天足够

# 关注的报表科目（yfinance income_stmt 行名）
# Operating Income / Pretax Income / Tax Provision 供营业利润口径正常化 EPS 使用
# （剥离一次性非经营损益——GOOGL/AMZN 曾有半数 TTM EPS 来自投资重估，见 signals.valuation）
_FIN_ROWS = ["Total Revenue", "Gross Profit", "Operating Income", "Pretax Income",
             "Tax Provision", "Net Income", "Diluted EPS"]
_FIN_SCHEMA = "|".join(_FIN_ROWS)   # 进缓存 key，科目集一变即自动失效
# EDGAR companyfacts us-gaap tag → 统一行名
_XBRL_TAGS = {
    "Revenues": "Total Revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "Total Revenue",
    "GrossProfit": "Gross Profit",
    "OperatingIncomeLoss": "Operating Income",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
        "Pretax Income",
    "IncomeTaxExpenseBenefit": "Tax Provision",
    "NetIncomeLoss": "Net Income",
    "EarningsPerShareDiluted": "Diluted EPS",
}
_FORMS = {"8-K", "10-K", "10-Q"}   # 只关注材料事件 + 定期报告
# Yahoo 怪癖：双类股基本面/filings 挂在另一 class 下（GOOGL→GOOG），空结果时按别名重试
_YF_ALIAS = {"GOOGL": "GOOG"}


class EdgarSource:
    """核心持仓 filings + 财务序列数据源（EDGAR 优先、yfinance 回退）。"""

    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache
        self.degraded: list[str] = []
        self._edgar_ok: bool | None = None   # 每进程探测一次
        self._mirror_noted = False           # 镜像降级标每进程只写一次

    # ── EDGAR 可达性 ──────────────────────────────────────

    def edgar_reachable(self) -> bool:
        if self._edgar_ok is None:
            try:
                r = requests.get(
                    "https://www.sec.gov/files/company_tickers.json",
                    headers={"User-Agent": EDGAR_USER_AGENT}, timeout=_EDGAR_TIMEOUT)
                self._edgar_ok = r.status_code == 200
            except Exception:
                self._edgar_ok = False
            if not self._edgar_ok:
                msg = "EDGAR_UNREACHABLE(sec.gov TLS reset,回退 yfinance 镜像)"
                self.degraded.append(msg)
                logger.warning(f"[EDGAR] {msg}")
        return self._edgar_ok

    def _cik(self, ticker: str) -> str | None:
        """ticker→CIK（10 位零填充）。仅 EDGAR 可达时调用。"""
        key = self.cache.make_key("edgar_cik_map", "all")
        df = self.cache.get(key)
        if df is None:
            r = requests.get("https://www.sec.gov/files/company_tickers.json",
                             headers={"User-Agent": EDGAR_USER_AGENT}, timeout=_EDGAR_TIMEOUT)
            rows = [{"ticker": v["ticker"], "cik": v["cik_str"]} for v in r.json().values()]
            df = pd.DataFrame(rows)
            self.cache.set(key, df, ttl_hours=24 * 30)
        hit = df[df["ticker"] == ticker.upper()]
        return f"{int(hit['cik'].iloc[0]):010d}" if not hit.empty else None

    # ── Filings（8-K / 10-K / 10-Q 元数据）────────────────

    def _note_mirror(self, df: pd.DataFrame) -> None:
        """镜像降级标**从数据本身判定**，不看探测结果。

        历史 bug（2026-08-27 修）：原先只在 `edgar_reachable()` 探测失败时写
        `EDGAR_UNREACHABLE`，而 filings 命中缓存时该探测根本不会被调用 →
        **同一天的第二次运行降级清单会比第一次少一条**，报告因此漏掉 skill 要求
        必须标注的「未读原文」。改为看返回行的 `source`：只要有非 edgar 行就记一条，
        与缓存状态无关。
        """
        if self._mirror_noted or df.empty or "source" not in df.columns:
            return
        if (df["source"] != "edgar").any():
            self.degraded.append(
                "FILINGS_MIRROR(filings 来自 yfinance 镜像而非 EDGAR 原文 → 未读原文口径)")
            self._mirror_noted = True

    def get_filings(self, ticker: str, limit: int = 15) -> pd.DataFrame:
        """最近 filings：columns = [date, form, title, url, source]，新→旧。"""
        key = self.cache.make_key("edgar_filings", ticker)
        cached = self.cache.get(key)
        if cached is not None:
            self._note_mirror(cached)
            return cached.head(limit)

        df = pd.DataFrame()
        if self.edgar_reachable():
            try:
                df = self._filings_edgar(ticker)
            except Exception as e:
                logger.warning(f"[EDGAR] filings 官方源失败 {ticker}: {e}")
        if df.empty:
            df = self._filings_yf(ticker)
        if df.empty and ticker in _YF_ALIAS:
            df = self._filings_yf(_YF_ALIAS[ticker])
        if not df.empty:
            df = df.sort_values("date", ascending=False).reset_index(drop=True)
            self.cache.set(key, df, ttl_hours=TTL_FILINGS)
            self._note_mirror(df)
        else:
            self.degraded.append(f"FILINGS_MISSING:{ticker}")
        return df.head(limit)

    def _filings_edgar(self, ticker: str) -> pd.DataFrame:
        cik = self._cik(ticker)
        if cik is None:
            return pd.DataFrame()
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers={"User-Agent": EDGAR_USER_AGENT}, timeout=_EDGAR_TIMEOUT)
        recent = r.json()["filings"]["recent"]
        rows = []
        for form, date, acc, doc in zip(recent["form"], recent["filingDate"],
                                        recent["accessionNumber"], recent["primaryDocument"]):
            if form not in _FORMS:
                continue
            acc_nodash = acc.replace("-", "")
            rows.append({
                "date": date, "form": form, "title": form,
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{doc}",
                "source": "edgar",
            })
        return pd.DataFrame(rows)

    @staticmethod
    def _filings_yf(ticker: str) -> pd.DataFrame:
        try:
            fil = yf.Ticker(ticker).sec_filings or []
        except Exception as e:
            logger.warning(f"[EDGAR] yfinance sec_filings 失败 {ticker}: {e}")
            return pd.DataFrame()
        rows = [{"date": str(f.get("date", "")), "form": f.get("type", ""),
                 "title": f.get("title", ""), "url": f.get("edgarUrl", ""),
                 "source": "yfinance"}
                for f in fil if f.get("type") in _FORMS]
        # 顺序由 get_filings 统一排序（yfinance 返回顺序无文档保证）
        return pd.DataFrame(rows)

    # ── 财务序列（年/季，含 Diluted EPS）───────────────────

    def get_financials(self, ticker: str) -> dict[str, pd.DataFrame]:
        """{'annual': df, 'quarterly': df}，df: index=科目行、columns=期末日期（新→旧）。

        主口径 yfinance 报表（本网络 EDGAR 不可达）；EDGAR 可达时用 companyfacts
        XBRL（更长历史 + 真 PIT filing 日期）。空缺科目行如实缺席，不填零。
        """
        out: dict[str, pd.DataFrame] = {}
        for freq, suffix in (("annual", "a"), ("quarterly", "q")):
            # 科目集进 key：改 _FIN_ROWS 后旧缓存自动失效，否则 TTL_FIN(72h) 内会一直
            # 喂回缺少新科目的旧 df（新增 Operating Income 时就踩过这个坑）
            key = self.cache.make_key(f"edgar_fin_{suffix}", ticker, _FIN_SCHEMA)
            cached = self.cache.get(key)
            if cached is not None:
                out[freq] = cached
                continue
            df = pd.DataFrame()
            if self.edgar_reachable():
                try:
                    df = self._fin_edgar(ticker, freq)
                except Exception as e:
                    logger.warning(f"[EDGAR] companyfacts 失败 {ticker}: {e}")
            if df.empty:
                df = self._fin_yf(ticker, freq)
            if df.empty and ticker in _YF_ALIAS:
                df = self._fin_yf(_YF_ALIAS[ticker], freq)
            if not df.empty:
                self.cache.set(key, df, ttl_hours=TTL_FIN)
            else:
                self.degraded.append(f"FINANCIALS_MISSING:{ticker}:{freq}")
            out[freq] = df
        return out

    @staticmethod
    def _fin_yf(ticker: str, freq: str) -> pd.DataFrame:
        try:
            t = yf.Ticker(ticker)
            stmt = t.income_stmt if freq == "annual" else t.quarterly_income_stmt
        except Exception as e:
            logger.warning(f"[EDGAR] yfinance 报表失败 {ticker}/{freq}: {e}")
            return pd.DataFrame()
        if stmt is None or stmt.empty:
            return pd.DataFrame()
        rows = [r for r in _FIN_ROWS if r in stmt.index]
        df = stmt.loc[rows].copy()
        df.columns = [pd.Timestamp(c).strftime("%Y-%m-%d") for c in df.columns]
        return df

    def _fin_edgar(self, ticker: str, freq: str) -> pd.DataFrame:
        cik = self._cik(ticker)
        if cik is None:
            return pd.DataFrame()
        r = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                         headers={"User-Agent": EDGAR_USER_AGENT}, timeout=_EDGAR_TIMEOUT)
        gaap = r.json().get("facts", {}).get("us-gaap", {})
        want_annual = freq == "annual"
        series: dict[str, dict[str, float]] = {}
        for tag, row_name in _XBRL_TAGS.items():
            for unit_vals in gaap.get(tag, {}).get("units", {}).values():
                for v in unit_vals:
                    end, start = v.get("end"), v.get("start")
                    if not end or not start:
                        continue
                    # ⚠️ 必须按**期间长度**筛：一份 10-K 同时含 FY 12 个月与 Q4 3 个月两组事实，
                    # 二者 form/fp/end 全同——不筛则后写覆盖，年度值可能落成 Q4 值（约 1/4），
                    # 进而使 TTM EPS 偏小、P/E 带虚高 ~4 倍，误触发「极端高估 → trim」。
                    days = (pd.Timestamp(end) - pd.Timestamp(start)).days
                    if want_annual:
                        if not (v.get("fp") == "FY" and v.get("form") == "10-K"
                                and 330 <= days <= 400):
                            continue
                    else:
                        if not (v.get("form") in ("10-Q", "10-K") and 80 <= days <= 100):
                            continue
                    # 同名行的多个 tag（如两种 Revenue 口径）按 _XBRL_TAGS 顺序取优先，不覆盖
                    series.setdefault(row_name, {}).setdefault(end, float(v["val"]))
        if not series:
            return pd.DataFrame()
        df = pd.DataFrame(series).T
        df = df[sorted(df.columns, reverse=True)]
        return df

    # ── 新鲜度（R3.2 纪律）────────────────────────────────

    @staticmethod
    def staleness(ticker: str, latest_filing_date: str | None,
                  limit_days: int = 120) -> str | None:
        """最近一条 filing 距今超限（默认 120 天≈一个季报周期+余量）→ STALE 标记。"""
        if not latest_filing_date:
            return f"EDGAR_STALE:{ticker}(no filings)"
        age = (pd.Timestamp.now().normalize() - pd.Timestamp(latest_filing_date)).days
        if age > limit_days:
            return f"EDGAR_STALE:{ticker}({age}d>{limit_days}d)"
        return None
