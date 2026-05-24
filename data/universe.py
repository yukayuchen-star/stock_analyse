"""
Universe 构建器 — S&P 500 ∪ Nasdaq Top30 by market cap

来源：
  - S&P 500 成分：Wikipedia 表格（稳定，每日缓存到 cache/universe_<date>.json）
  - Nasdaq Top30：Nasdaq-100 成分（同样来自 Wikipedia）再按 yfinance marketCap 排序取前 30

每日缓存避免重复抓取；force_refresh=True 可强制刷新。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from io import StringIO
from pathlib import Path
from typing import List

import pandas as pd
import requests
import yfinance as yf
from loguru import logger

from utils.time_utils import today_str


_CACHE_DIR = Path("cache") / "universe"
_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NDX_URL   = "https://en.wikipedia.org/wiki/Nasdaq-100"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def _normalize(symbol: str) -> str:
    """Wikipedia 用 BRK.B / BF.B 风格，yfinance 用 BRK-B / BF-B。"""
    return symbol.strip().upper().replace(".", "-")


def _fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
    r.raise_for_status()
    return r.text


def _fetch_sp500() -> List[str]:
    tables = pd.read_html(StringIO(_fetch_html(_SP500_URL)))
    df = tables[0]
    return [_normalize(s) for s in df["Symbol"].tolist()]


def _fetch_ndx100() -> List[str]:
    tables = pd.read_html(StringIO(_fetch_html(_NDX_URL)))
    # Nasdaq-100 页面有多张表，找含 Ticker / Symbol 列的成分表
    for t in tables:
        cols = [c for c in t.columns if isinstance(c, str)]
        for col in cols:
            if col.lower() in ("ticker", "symbol"):
                return [_normalize(s) for s in t[col].tolist()]
    raise RuntimeError("Nasdaq-100 成分表未找到")


def _fetch_marketcap(tickers: List[str], max_workers: int = 10) -> dict[str, float]:
    """并行取 marketCap，失败的股票静默忽略（10 路 ≈ 串行 10×）。"""
    def _one(tk: str) -> tuple[str, float]:
        try:
            mcap = float(getattr(yf.Ticker(tk).fast_info, "market_cap", 0) or 0)
            return tk, mcap
        except Exception:
            return tk, 0.0

    caps: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed(ex.submit(_one, t) for t in tickers):
            tk, mcap = fut.result()
            if mcap > 0:
                caps[tk] = mcap
    return caps


def get_universe(
    nasdaq_top: int = 30,
    force_refresh: bool = False,
) -> List[str]:
    """
    返回 S&P 500 ∪ Nasdaq-100 Top{nasdaq_top} by market cap，去重。

    每日缓存到 cache/universe/<YYYY-MM-DD>.json。
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _CACHE_DIR / f"{today_str()}.json"

    if cache_file.exists() and not force_refresh:
        try:
            data = json.loads(cache_file.read_text())
            universe = data.get("tickers", [])
            logger.info(f"[Universe] 缓存命中: {len(universe)} 只 ({cache_file.name})")
            return universe
        except Exception as e:
            logger.warning(f"[Universe] 缓存读取失败 {cache_file}: {e}")

    logger.info("[Universe] 抓取 S&P 500 + Nasdaq-100 成分表 ...")
    sp500 = _fetch_sp500()
    logger.info(f"[Universe]   S&P 500: {len(sp500)} 只")

    ndx = _fetch_ndx100()
    logger.info(f"[Universe]   Nasdaq-100: {len(ndx)} 只")

    logger.info(f"[Universe] 取 Nasdaq Top {nasdaq_top} by market cap ...")
    caps = _fetch_marketcap(ndx)
    ndx_top = [t for t, _ in sorted(caps.items(), key=lambda x: x[1], reverse=True)[:nasdaq_top]]
    logger.info(f"[Universe]   Nasdaq Top{nasdaq_top}: {ndx_top[:10]}...")

    universe = sorted(set(sp500) | set(ndx_top))
    logger.info(f"[Universe] 合并去重后: {len(universe)} 只")

    cache_file.write_text(json.dumps({
        "date":       today_str(),
        "sp500":      sp500,
        "ndx_top":    ndx_top,
        "tickers":    universe,
    }, indent=2))
    logger.info(f"[Universe] 已写入缓存: {cache_file}")

    return universe
