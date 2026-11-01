"""资本开支与现金流前向记录：F6 capex_assets / F7 fcf_yield / F8 gross_prof（口径见仓库根 `PREREG-capex-forward.md`）。

yfinance 只给约 5 个季度、无 PIT 历史，只能向前记，绝不回填。由 `python forward_capex.py` 每周触发，不挂 `main.py`。
每票写 status，报告以各因子非空数为准：Yahoo 401 会让表静默为空，宽宇宙首条记录里 err 因此严重低估缺失。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from backtest.factor_forward_buyback import _consecutive
from backtest.factor_forward_wide import _ic_series, _t, universe
from backtest.factor_lab import VALIDATION_UNIVERSE

ROOT = Path(__file__).resolve().parents[1]
FROZEN = {
    "PREREG-capex-forward.md": "6ee88ca6dc7a7bcd1a16743b5663da86fdaa7caa6cb739d463ed6eb944eda4fa",
    "config/sp500_constituents_2026-10-29.csv": "4b8ea078569b80ae1fdd688d30748f95ac1f6ee7b9b311c79cd1dc92f3fb3642",
    "backtest/factor_forward_buyback.py": "ac9e566029dadef9b9911991d34e42a0e4c6244be0fba51bec91ef49d3c215c0",
}
EXCLUDED_SECTORS = ("Financials", "Real Estate")
PRIMARY = {"capex_assets": -1, "fcf_yield": 1, "gross_prof": 1}   # 预期 IC 方向
SECONDARY = ("asset_growth", "capex_sales")
RAW = ("ttm_capex", "ttm_fcf", "ttm_ocf", "ttm_gp", "ttm_rev", "total_assets", "total_assets_4q", "shares_out", "close")
COLS = (*PRIMARY, *SECONDARY, *RAW)
CORE_NAMES = ("NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", "META")
HORIZONS = (5, 10, 20, 60)
PRIMARY_H = 20
MIN_NAMES = 30
MIN_IC_DAYS = 30
LOG_INTERVAL_DAYS = 5
BOUNDARY_T = 2.5
FIRST_LOOK_YEAR = 2027
WORKERS = 2
FETCH_TRIES = 3

DB_PATH = ROOT / "cache" / "forward_signals.db"


def capex_universe() -> dict[str, str]:
    return {t: s for t, s in universe().items() if s not in EXCLUDED_SECTORS}


def frozen_ok() -> bool:
    for rel, sha in FROZEN.items():
        got = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if got != sha:
            logger.warning(f"[CapexForward] {rel} sha 不符（{got[:12]}… ≠ {sha[:12]}…）——口径可能已变，本次不记录")
            return False
    return True


def _conn(db: Path = DB_PATH) -> sqlite3.Connection:
    db.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS capex_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_date TEXT NOT NULL, ticker TEXT NOT NULL, sector TEXT, bar_date TEXT, q_end TEXT,
            {', '.join(f'{k} REAL' for k in COLS)},
            status TEXT, err TEXT,
            UNIQUE(logged_date, ticker))""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS capex_outcomes (
            event_id INTEGER PRIMARY KEY REFERENCES capex_events(id),
            entry_date TEXT, entry_price REAL,
            fwd5 REAL, fwd10 REAL, fwd20 REAL, fwd60 REAL)""")
    c.commit()
    return c


def _ttm_signed(table: pd.DataFrame, row: str) -> tuple[Optional[float], Optional[str]]:
    """保留符号的 TTM（FCF 可为负）。行缺失或不足 4 个连续季度 ⇒ 留空，不记 0。"""
    if table is None or table.empty or row not in table.index:
        return None, None
    s = _consecutive(table.loc[row].astype(float), 4)
    if s is None:
        return None, None
    return float(s.sum()), str(s.index[0].date())


def _fetch(ticker: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    import yfinance as yf
    t = yf.Ticker(ticker)
    return t.quarterly_cashflow, t.quarterly_balance_sheet, t.quarterly_income_stmt, (t.info or {})


def factors_from(cf, bs, inc, shares, close) -> dict:
    out: dict = {k: None for k in (*COLS, "q_end")}
    capex, q = _ttm_signed(cf, "Capital Expenditure")
    out["ttm_capex"] = abs(capex) if capex is not None else None
    out["ttm_fcf"], q2 = _ttm_signed(cf, "Free Cash Flow")
    out["ttm_ocf"], _ = _ttm_signed(cf, "Operating Cash Flow")
    out["ttm_gp"], _ = _ttm_signed(inc, "Gross Profit")
    out["ttm_rev"], _ = _ttm_signed(inc, "Total Revenue")
    out["q_end"] = q or q2
    if bs is not None and not bs.empty and "Total Assets" in bs.index:
        ta = bs.loc["Total Assets"].astype(float).dropna().sort_index(ascending=False)
        if len(ta) and ta.iloc[0] > 0:
            out["total_assets"] = float(ta.iloc[0])
        five = _consecutive(ta, 5)
        if five is not None and five.iloc[4] > 0:
            out["total_assets_4q"] = float(five.iloc[4])
            out["asset_growth"] = float(five.iloc[0] / five.iloc[4] - 1)
    ta0 = out["total_assets"]
    if ta0:
        out["capex_assets"] = out["ttm_capex"] / ta0 if out["ttm_capex"] is not None else None
        out["gross_prof"] = out["ttm_gp"] / ta0 if out["ttm_gp"] is not None else None
    if out["ttm_capex"] is not None and out["ttm_rev"]:
        out["capex_sales"] = out["ttm_capex"] / out["ttm_rev"]
    out["shares_out"], out["close"] = shares, close
    if out["ttm_fcf"] is not None and shares and close and close > 0:
        out["fcf_yield"] = out["ttm_fcf"] / (shares * close)
    return out


def snapshot(ticker: str, pipeline) -> dict:
    """同一次运行内失败最多重试 FETCH_TRIES 次（仍是同一次快照，不算回填）。"""
    close, bar = None, None
    errs = []
    try:
        price = pipeline.get_backtest_price(ticker)
        if price is not None and len(price):
            close, bar = float(price["Close"].iloc[-1]), str(price.index[-1].date())
    except Exception as exc:
        errs.append(f"price:{exc!r}"[:120])
    cf = bs = inc = None
    info: dict = {}
    for attempt in range(FETCH_TRIES):
        try:
            cf, bs, inc, info = _fetch(ticker)
            if not any(x is None or x.empty for x in (cf, bs, inc)) and info.get("sharesOutstanding"):
                errs = [e for e in errs if not e.startswith("fetch:")]
                break
        except Exception as exc:
            errs = [e for e in errs if not e.startswith("fetch:")] + [f"fetch:{exc!r}"[:120]]
        if attempt < FETCH_TRIES - 1:
            time.sleep(2 * (attempt + 1))
    out = factors_from(cf, bs, inc, info.get("sharesOutstanding"), close)
    out["bar_date"] = bar
    n_ok = sum(out[k] is not None for k in PRIMARY)
    all_empty = all(x is None or x.empty for x in (cf, bs, inc))
    out["status"] = ("error" if errs and n_ok == 0 else "empty" if all_empty else "ok" if n_ok == len(PRIMARY) else "partial")
    out["err"] = "; ".join(errs) or None
    return out


def log_capex_events(date_str: str, pipeline, tickers: Optional[dict] = None, db: Path = DB_PATH) -> int:
    if LOG_INTERVAL_DAYS <= 0 or not frozen_ok():
        return 0
    c = _conn(db)
    last = c.execute("SELECT MAX(logged_date) AS d FROM capex_events").fetchone()["d"]
    c.close()
    if last is not None and (pd.Timestamp(date_str) - pd.Timestamp(last)).days < LOG_INTERVAL_DAYS:
        logger.info(f"[CapexForward] 距上次记录 {last} 不足 {LOG_INTERVAL_DAYS} 天，跳过")
        return 0
    tickers = tickers or capex_universe()

    def one(tk):
        return tk, snapshot(tk, pipeline)

    with ThreadPoolExecutor(WORKERS) as ex:
        snaps = list(ex.map(one, tickers))
    c = _conn(db)
    inserted = 0
    cols = ("sector", "bar_date", "q_end", *COLS, "status", "err")
    for tk, s in snaps:
        s["sector"] = tickers[tk]
        c.execute(f"INSERT OR IGNORE INTO capex_events (logged_date, ticker, {', '.join(cols)}) "
                  f"VALUES (?, ?, {', '.join('?' * len(cols))})", (date_str, tk, *[s.get(k) for k in cols]))
        inserted += c.execute("SELECT changes()").fetchone()[0]
    c.commit()
    c.close()
    cov = {k: sum(s[k] is not None for _, s in snaps) for k in PRIMARY}
    st = pd.Series([s["status"] for _, s in snaps]).value_counts().to_dict()
    logger.info(f"[CapexForward] 记录 {inserted} 票 logged={date_str} 覆盖 {cov} 状态 {st}")
    return inserted


def evaluate_capex_pending(pipeline, db: Path = DB_PATH, today: Optional[str] = None) -> int:
    """入场 = logged_date 之后第一根收盘；满 20TD 计 5/10/20，满 60TD 补 60。"""
    today = pd.Timestamp(today or pd.Timestamp.today().normalize())
    c = _conn(db)
    rows = c.execute("""
        SELECT e.id, e.ticker, e.logged_date, o.fwd20, o.fwd60 FROM capex_events e
        LEFT JOIN capex_outcomes o ON o.event_id = e.id
        WHERE o.event_id IS NULL OR o.fwd60 IS NULL""").fetchall()
    c.close()
    due = [r for r in rows if (today - pd.Timestamp(r["logged_date"])).days >= (30 if r["fwd20"] is None else 86)]
    if not due:
        return 0
    by: dict[str, list] = {}
    for r in due:
        by.setdefault(r["ticker"], []).append(r)

    def prices(tk):
        try:
            return tk, pipeline.get_backtest_price(tk)
        except Exception:
            return tk, None

    with ThreadPoolExecutor(WORKERS) as ex:
        px = dict(ex.map(prices, by))
    c = _conn(db)
    n = 0
    for tk, rs in by.items():
        df = px.get(tk)
        if df is None or df.empty:
            continue
        close = df["Close"].astype(float)
        for r in rs:
            fut = close[close.index > pd.Timestamp(r["logged_date"])]
            if len(fut) < 21 or not fut.iloc[0] > 0:
                continue
            e = fut.iloc[0]
            fwd = {h: (float(fut.iloc[h] / e - 1) if len(fut) > h else None) for h in HORIZONS}
            c.execute("INSERT OR REPLACE INTO capex_outcomes VALUES (?,?,?,?,?,?,?)",
                      (r["id"], str(fut.index[0].date()), float(e), fwd[5], fwd[10], fwd[20], fwd[60]))
            n += 1
    c.commit()
    c.close()
    if n:
        logger.info(f"[CapexForward] 更新成熟结果 {n} 条")
    return n


# ── 报告（进度，不是裁决） ─────────────────────────────────

def build_report(date_str: str, db: Path = DB_PATH) -> str:
    c = _conn(db)
    ev = pd.read_sql_query("SELECT * FROM capex_events", c)
    df = pd.read_sql_query("SELECT e.*, o.fwd5, o.fwd10, o.fwd20, o.fwd60 FROM capex_events e "
                           "JOIN capex_outcomes o ON o.event_id = e.id", c)
    c.close()
    L = ["# 资本开支与现金流前向记录 · 进度", "",
         f"生成日期：{date_str}　记录日 {ev['logged_date'].nunique()}　记录 {len(ev)} 条　已成熟 {len(df)} 条", "",
         f"**预注册** `PREREG-capex-forward.md`：下表是**进度不是裁决**；正式查看每年 10 月后一次（首次 {FIRST_LOOK_YEAR}-10），"
         f"确认须预期方向 |t| ≥ {BOUNDARY_T} 且行业中性与 78 只子集同向。", ""]
    if not ev.empty:
        last = ev[ev["logged_date"] == ev["logged_date"].max()]
        L += [f"最近一次覆盖（{last['logged_date'].iloc[0]}，{len(last)} 票）："
              + "　".join(f"`{k}` {last[k].notna().sum()}" for k in PRIMARY)
              + "　状态 " + " · ".join(f"{k} {v}" for k, v in last["status"].value_counts().items()), "",
              "六只核心名的因子分位（只作描述，**不是证据，也不用于判断 thesis**）：", "",
              "| 票 | " + " | ".join(f"`{k}`" for k in PRIMARY) + " |", "|---|" + "---|" * len(PRIMARY)]
        pct = {k: last.set_index("ticker")[k].rank(pct=True) for k in PRIMARY}
        for t in CORE_NAMES:
            cells = [f"{pct[k].get(t):.0%}" if pd.notna(pct[k].get(t)) else "—" for k in PRIMARY]
            L.append(f"| {t} | " + " | ".join(cells) + " |")
        L.append("")
    if df.empty:
        L.append("> ⏳ 尚无成熟记录——每条记录需入场后 20 个交易日成熟。")
        return "\n".join(L)
    sub = df[df["ticker"].isin(VALIDATION_UNIVERSE)]
    L += ["| 主口径（预期方向） | 全宇宙 IC (t, n) | 行业中性 | 78 只子集 |", "|---|---|---|---|"]
    for f, sign in PRIMARY.items():
        cells = []
        for s in (_t(_ic_series(df, f, PRIMARY_H), PRIMARY_H), _t(_ic_series(df, f, PRIMARY_H, True), PRIMARY_H),
                  _t(_ic_series(sub, f, PRIMARY_H), PRIMARY_H)):
            cells.append(f"{s['ic']:+.4f} ({s['t']:+.2f}, {s['n']})" if np.isfinite(s["ic"]) else "—")
        L.append(f"| `{f}` × fwd{PRIMARY_H}（{'<' if sign < 0 else '>'} 0） | " + " | ".join(cells) + " |")
    n_days = min(len(_ic_series(df, f, PRIMARY_H)) for f in PRIMARY)
    if n_days < MIN_IC_DAYS:
        L += ["", f"> ⏳ **待累积**：IC 日 {n_days} < {MIN_IC_DAYS}，不判方向。"]
    return "\n".join(L)


def write_capex_report(date_str: str, output_dir: Path, db: Path = DB_PATH) -> Optional[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "capex_forward_oos.md"
    path.write_text(build_report(date_str, db), encoding="utf-8")
    logger.info(f"  资本开支前向报告: {path}")
    return path
