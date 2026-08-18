"""
R9.4/R9.5 核心持仓研究预取器（Python 侧：取数 + 算估值，定性判断交给 skill）。

用法：python core_research.py
产物：output/{date}/core_inputs.json —— 每只核心股的 filings 元数据 / 财务序列摘要 /
      估值带 / 缠论与技术状态 / 财报日历 / 台账派生（成本、进度、底仓下限）。
台账：output/core_ledger.json 不存在时生成空模板（真金成交须用户手动维护）。

诚实边界：advisory 预取，无回测无信号发射；数据缺失如实进 degraded，不臆造。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
from loguru import logger

from config.stocks import (BASE_FLOOR_FRAC, CORE_HOLDINGS, CORE_LEDGER_PATH,
                           CORE_TARGET_FRAC)
from data.cache import SQLiteCache
from data.edgar_source import EdgarSource
from data.yfinance_source import YFinanceSource
from signals.chan.chan_signal import compute_chan_signal
from signals.valuation import _last_close, qqq_valuation, valuation_band

_PRICE_DAYS = 800   # 与实盘管线同窗口（缠论需 ≥200 根，推荐 550+ TD）

_LEDGER_TEMPLATE = {
    "_readme": "真金核心台账：每笔成交手动追加进对应票 fills（layer: base=底仓|enh=增强层）；"
               "total_capital 填总资金美元数（核心目标 = 70% × 此值）；"
               "enhancement_rounds 记高抛低吸配对（status: open|paired|abandoned）。",
    "total_capital": None,
    "positions": {t: {"fills": []} for t in CORE_HOLDINGS},
    "enhancement_rounds": [],
}


def _load_ledger() -> dict:
    path = Path(CORE_LEDGER_PATH)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_LEDGER_TEMPLATE, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        logger.warning(f"[Core] 台账不存在，已生成空模板: {path}（请填入真实成交）")
        return json.loads(json.dumps(_LEDGER_TEMPLATE))
    return json.loads(path.read_text(encoding="utf-8"))


def _ledger_stats(ledger: dict, ticker: str, price: float | None) -> dict:
    fills = ledger.get("positions", {}).get(ticker, {}).get("fills", [])
    shares = sum(f["shares"] for f in fills)
    invested = sum(f["shares"] * f["price"] for f in fills)
    stats = {
        "shares": shares,
        "avg_cost": round(invested / shares, 3) if shares else None,
        "invested": round(invested, 2),
        # 底仓下限（R9.0 双层结构）：高抛卖出不得使持仓跌破此股数
        "base_floor_shares": int(shares * BASE_FLOOR_FRAC),
        "enhancement_shares": shares - int(shares * BASE_FLOOR_FRAC),
    }
    if shares and price:
        stats["market_value"] = round(shares * price, 2)
        stats["unrealized_pct"] = round(price / stats["avg_cost"] - 1, 4)
    open_rounds = [r for r in ledger.get("enhancement_rounds", [])
                   if r.get("ticker") == ticker and r.get("status") == "open"]
    stats["open_enhancement_rounds"] = open_rounds
    return stats


def _fin_summary(fin: dict[str, pd.DataFrame]) -> dict:
    """年/季关键科目 + YoY（skill 判 thesis 用的结构化趋势，非估值口径）。"""
    out: dict = {}
    for freq in ("annual", "quarterly"):
        df = fin.get(freq, pd.DataFrame())
        if df.empty:
            continue
        blk: dict = {}
        for row in df.index:
            vals = df.loc[row].dropna()
            blk[row] = {str(k)[:10]: (round(float(v), 4) if abs(float(v)) < 1e3
                                      else round(float(v)))
                        for k, v in vals.items()}
        # YoY：年度=相邻财年；季度=同比（隔 4 期）
        rev = df.loc["Total Revenue"].dropna() if "Total Revenue" in df.index else pd.Series(dtype=float)
        if len(rev) >= 2:
            lag = 1 if freq == "annual" else min(4, len(rev) - 1)
            if len(rev) > lag and float(rev.iloc[lag]) != 0:
                blk["revenue_yoy_latest"] = round(float(rev.iloc[0]) / float(rev.iloc[lag]) - 1, 4)
        out[freq] = blk
    return out


def main() -> int:
    cache = SQLiteCache()
    yfs = YFinanceSource(cache)
    edgar = EdgarSource(cache)
    ledger = _load_ledger()

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=_PRICE_DAYS)).strftime("%Y-%m-%d")

    prices = {t: yfs.get_price(t, start, end) for t in CORE_HOLDINGS}
    date_str = None
    for df in prices.values():
        if df is not None and not df.empty:
            date_str = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
            break
    if date_str is None:
        logger.error("[Core] 全部核心票无价格数据，退出")
        return 1

    records: dict[str, dict] = {}
    core_bands: dict[str, dict] = {}
    core_caps: dict[str, float] = {}
    degraded: list[str] = []

    for t in CORE_HOLDINGS:
        df = prices.get(t)
        price = _last_close(df)
        rec: dict = {"ticker": t, "price": price}

        # ── 技术/缠论状态（只读复用，不改 55% 本体）──────
        if df is not None and len(df) >= 200:
            chan = compute_chan_signal(t, prices)
            close = df["Close"].dropna()
            sma200 = float(close.rolling(200).mean().iloc[-1])
            rec["technical"] = {
                "chan_score": round(float(chan.score), 3) if hasattr(chan, "score") else None,
                "buy_point": chan.buy_point_type,
                "sell_point": chan.sell_point_type,
                "weekly_trend": chan.weekly_trend,
                "trend_type": chan.trend_type,
                "sma200": round(sma200, 2),
                "dev_200dma": round(price / sma200 - 1, 4) if price else None,
                "atr_pct": round(float(chan.atr_pct), 4),
            }

        if t == "QQQ":
            records[t] = rec   # 估值代理在个股跑完后补
            continue

        # ── Filings + 财务（EDGAR 优先 → yfinance 回退）──
        filings = edgar.get_filings(t)
        rec["filings"] = filings.to_dict("records") if not filings.empty else []
        latest = filings["date"].iloc[0] if not filings.empty else None
        stale = EdgarSource.staleness(t, latest)
        if stale:
            degraded.append(stale)
        fin = edgar.get_financials(t)
        rec["financials"] = _fin_summary(fin)

        # ── 估值带 + 财报日历（info 直取：需要分析师目标价，仅 7 名，无缓存压力）──
        try:
            yft = yf.Ticker(t)
            info = yft.info or {}
        except Exception as e:
            logger.warning(f"[Core] info 失败 {t}: {e}")
            info, yft = {}, None
        band = valuation_band(t, df if df is not None else pd.DataFrame(), fin, info)
        rec["valuation"] = band
        core_bands[t] = band
        if info.get("marketCap"):
            core_caps[t] = float(info["marketCap"])
        try:
            cal = yft.calendar if yft is not None else {}
            ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if ed:
                rec["next_earnings"] = str(ed[0])
        except Exception:
            pass

        rec["ledger"] = _ledger_stats(ledger, t, price)
        records[t] = rec

    # ── QQQ 指数级估值代理（依赖个股 premium）──────────────
    qdf = prices.get("QQQ")
    if qdf is not None and not qdf.empty:
        records["QQQ"]["valuation"] = qqq_valuation(qdf, core_bands, core_caps)
        records["QQQ"]["ledger"] = _ledger_stats(ledger, "QQQ", _last_close(qdf))

    degraded += edgar.degraded
    total_invested = sum(r.get("ledger", {}).get("invested", 0) or 0 for r in records.values())
    total_capital = ledger.get("total_capital")
    portfolio = {
        "core_target_frac": CORE_TARGET_FRAC,
        "base_floor_frac": BASE_FLOOR_FRAC,
        "total_capital": total_capital,
        "core_invested": round(total_invested, 2),
        "core_built_frac": (round(total_invested / (total_capital * CORE_TARGET_FRAC), 4)
                            if total_capital else None),
        "ledger_filled": any(ledger["positions"][t]["fills"] for t in CORE_HOLDINGS
                             if t in ledger.get("positions", {})),
    }

    out_dir = Path("output") / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "portfolio": portfolio,
        "holdings": records,
        "degraded": sorted(set(degraded)),
    }
    out_path = out_dir / "core_inputs.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    logger.info(f"[Core] 预取完成 → {out_path}（degraded {len(set(degraded))} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
