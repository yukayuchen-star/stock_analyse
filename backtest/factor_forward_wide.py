"""宽宇宙前向记录：R11 修正 / F5 回购 / F4′ 残差动量 × S&P 500（口径见仓库根 `PREREG-wide-forward.md`）。

78 只的三个记录器原样继续；这里只换宇宙、复用它们的因子函数（sha 冻结，不符即不记录）。
单次约 2,000 次 yfinance 请求，故不挂 `main.py`，由 `python forward_wide.py` 每周触发。绝不回填。
"""
from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from backtest.factor_lab import VALIDATION_UNIVERSE

ROOT = Path(__file__).resolve().parents[1]
CONSTITUENTS = ROOT / "config" / "sp500_constituents_2026-10-29.csv"
EXTRA = {"ARM": "Information Technology"}      # 78 只里唯一不在指数内的
FROZEN = {
    "config/sp500_constituents_2026-10-29.csv": "4b8ea078569b80ae1fdd688d30748f95ac1f6ee7b9b311c79cd1dc92f3fb3642",
    "backtest/factor_forward_revision.py": "11729c4ccb78480872fd3d34247cdc62d5c047a70e9303fa8d5e11196c6e649f",
    "backtest/factor_forward_buyback.py": "ac9e566029dadef9b9911991d34e42a0e4c6244be0fba51bec91ef49d3c215c0",
    "backtest/factor_forward_resmom.py": "9237572fafcebaf043761c64eff2ed7a3f49c0b61161fc6e867b51aca7491d71",
    "backtest/prereg_factor_v1.py": "959255e58c863385aeab2eca2c2e017f98d05c1589b64f52e7d2041779583b23",
}
REV = ("rev30_fy", "rev90_fy", "breadth_fy", "rev30_q", "breadth_q")
BB = ("bb_float", "bb_adv", "net_shrink")
BB_RAW = ("q_end", "ttm_rep", "float_shares", "shares_out", "close", "adv20", "ttm_sbc", "rep_row_missing")
COLS = (*REV, "f4p", *BB, *BB_RAW)
PRIMARY = {"rev30_fy": 20, "bb_float": 20, "f4p": 10}
HORIZONS = (5, 10, 20, 60)
MIN_NAMES = 30
MIN_IC_DAYS = 30
LOG_INTERVAL_DAYS = 5
RECORD_SPACING_TD = 5
BOUNDARY_T = 2.5
FIRST_LOOK_YEAR = 2027
WORKERS = 4

DB_PATH = ROOT / "cache" / "forward_signals.db"


def universe() -> dict[str, str]:
    df = pd.read_csv(CONSTITUENTS)
    return {**dict(zip(df["ticker"], df["gics_sector"])), **EXTRA}


def frozen_ok() -> bool:
    for rel, sha in FROZEN.items():
        got = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if got != sha:
            logger.warning(f"[WideForward] {rel} sha 不符（{got[:12]}… ≠ {sha[:12]}…）——口径可能已变，本次不记录")
            return False
    return True


def _conn(db: Path = DB_PATH) -> sqlite3.Connection:
    db.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS wide_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_date TEXT NOT NULL, ticker TEXT NOT NULL, sector TEXT, bar_date TEXT,
            {', '.join(f'{k} {"TEXT" if k == "q_end" else "REAL"}' for k in COLS)},
            err TEXT,
            UNIQUE(logged_date, ticker))""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS wide_outcomes (
            event_id INTEGER PRIMARY KEY REFERENCES wide_events(id),
            entry_date TEXT, entry_price REAL,
            fwd5 REAL, fwd10 REAL, fwd20 REAL, fwd60 REAL)""")
    c.commit()
    return c


def snapshot(ticker: str, pipeline, qqq_ret: pd.Series) -> dict:
    """单票 as-of 快照；任一分量失败只让那一部分留空，错误记进 err。"""
    from backtest.factor_forward_buyback import buyback_snapshot
    from backtest.factor_forward_resmom import f4p_last
    from backtest.factor_forward_revision import revision_factors
    out: dict = {k: None for k in COLS}
    errs = []
    price = None
    try:
        price = pipeline.get_backtest_price(ticker)
        out["bar_date"] = str(price.index[-1].date()) if price is not None and len(price) else None
    except Exception as exc:
        errs.append(f"price:{exc!r}"[:120])
    try:
        out.update(revision_factors(pipeline.yf.get_eps_revisions(ticker)))
    except Exception as exc:
        errs.append(f"rev:{exc!r}"[:120])
    if price is not None and len(price):
        try:
            out["f4p"] = f4p_last(price, qqq_ret)
        except Exception as exc:
            errs.append(f"f4p:{exc!r}"[:120])
        try:
            bb = buyback_snapshot(ticker, price)
            if bb:
                out.update({k: bb[k] for k in (*BB, *BB_RAW)})
        except Exception as exc:
            errs.append(f"bb:{exc!r}"[:120])
    out["err"] = "; ".join(errs) or None
    return out


def log_wide_events(date_str: str, pipeline, tickers: Optional[dict] = None, db: Path = DB_PATH) -> int:
    if LOG_INTERVAL_DAYS <= 0 or not frozen_ok():
        return 0
    c = _conn(db)
    last = c.execute("SELECT MAX(logged_date) AS d FROM wide_events").fetchone()["d"]
    c.close()
    if last is not None and (pd.Timestamp(date_str) - pd.Timestamp(last)).days < LOG_INTERVAL_DAYS:
        logger.info(f"[WideForward] 距上次记录 {last} 不足 {LOG_INTERVAL_DAYS} 天，跳过")
        return 0
    tickers = tickers or universe()
    qqq = pipeline.get_backtest_price("QQQ")
    qqq_ret = qqq["Close"].astype(float).pct_change()

    def one(tk):
        return tk, snapshot(tk, pipeline, qqq_ret)

    with ThreadPoolExecutor(WORKERS) as ex:
        snaps = list(ex.map(one, tickers))

    c = _conn(db)
    inserted = 0
    cols = ("sector", "bar_date", *COLS, "err")
    for tk, s in snaps:
        s["sector"] = tickers[tk]
        c.execute(f"INSERT OR IGNORE INTO wide_events (logged_date, ticker, {', '.join(cols)}) "
                  f"VALUES (?, ?, {', '.join('?' * len(cols))})", (date_str, tk, *[s.get(k) for k in cols]))
        inserted += c.execute("SELECT changes()").fetchone()[0]
    c.commit()
    cov = {k: sum(s[k] is not None for _, s in snaps) for k in PRIMARY}
    n_err = sum(s["err"] is not None for _, s in snaps)
    c.close()
    logger.info(f"[WideForward] 记录 {inserted} 票 logged={date_str} 覆盖 {cov} 有错 {n_err}")
    return inserted


def evaluate_wide_pending(pipeline, db: Path = DB_PATH, today: Optional[str] = None) -> int:
    """只拉日历上可能已成熟的票：入场 = logged_date 之后第一根收盘；满 20TD 计 5/10/20，满 60TD 补 60。"""
    today = pd.Timestamp(today or pd.Timestamp.today().normalize())
    c = _conn(db)
    rows = c.execute("""
        SELECT e.id, e.ticker, e.logged_date, o.fwd20, o.fwd60 FROM wide_events e
        LEFT JOIN wide_outcomes o ON o.event_id = e.id
        WHERE o.event_id IS NULL OR o.fwd60 IS NULL""").fetchall()
    c.close()
    # 20 交易日 ≈ 28 日历日、60 ≈ 84：日历上还不可能成熟的不去拉价格
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
            c.execute("INSERT OR REPLACE INTO wide_outcomes VALUES (?,?,?,?,?,?,?)",
                      (r["id"], str(fut.index[0].date()), float(e), fwd[5], fwd[10], fwd[20], fwd[60]))
            n += 1
    c.commit()
    c.close()
    if n:
        logger.info(f"[WideForward] 更新成熟结果 {n} 条")
    return n


# ── 报告（进度，不是裁决） ─────────────────────────────────

def _ic_series(df: pd.DataFrame, f: str, h: int, neutral: bool = False) -> pd.Series:
    out = {}
    for d, g in df.groupby("logged_date"):
        s = g[[f, f"fwd{h}", "sector"]].dropna()
        if len(s) < MIN_NAMES or s[f].nunique() < 2:
            continue
        if neutral:
            s = s.assign(**{f: s[f] - s.groupby("sector")[f].transform("mean"),
                            f"fwd{h}": s[f"fwd{h}"] - s.groupby("sector")[f"fwd{h}"].transform("mean")})
        out[d] = s[f].corr(s[f"fwd{h}"], method="spearman")
    return pd.Series(out, dtype=float).dropna()


def _t(ic: pd.Series, h: int) -> dict:
    n = len(ic)
    if n < 2 or ic.std(ddof=1) == 0:
        return {"n": n, "ic": float(ic.mean()) if n else np.nan, "t": np.nan}
    return {"n": n, "ic": float(ic.mean()),
            "t": float(ic.mean() / ic.std(ddof=1) * np.sqrt(max(n * min(1, RECORD_SPACING_TD / h), 1)))}


def build_report(date_str: str, db: Path = DB_PATH) -> str:
    c = _conn(db)
    ev = pd.read_sql_query("SELECT * FROM wide_events", c)
    df = pd.read_sql_query("SELECT e.*, o.fwd5, o.fwd10, o.fwd20, o.fwd60 FROM wide_events e "
                           "JOIN wide_outcomes o ON o.event_id = e.id", c)
    c.close()
    L = ["# 宽宇宙前向记录（S&P 500 + ARM）· 进度", "",
         f"生成日期：{date_str}　记录日 {ev['logged_date'].nunique()}　记录 {len(ev)} 条　已成熟 {len(df)} 条", "",
         f"**预注册** `PREREG-wide-forward.md`：下表是**进度不是裁决**；正式查看每年 10 月后一次（首次 {FIRST_LOOK_YEAR}-10），"
         f"确认须 t ≥ {BOUNDARY_T} 且行业中性与 78 只子集同号。", ""]
    if not ev.empty:
        last = ev[ev["logged_date"] == ev["logged_date"].max()]
        L += [f"最近一次覆盖（{last['logged_date'].iloc[0]}，{len(last)} 票）："
              + "　".join(f"`{k}` {last[k].notna().sum()}" for k in PRIMARY)
              + f"　有错 {last['err'].notna().sum()}", ""]
    if df.empty:
        L.append("> ⏳ 尚无成熟记录——每条记录需入场后 20 个交易日成熟。")
        return "\n".join(L)
    sub = df[df["ticker"].isin(VALIDATION_UNIVERSE)]
    L += ["| 主口径 | 全宇宙 IC (t, n) | 行业中性 | 78 只子集 |", "|---|---|---|---|"]
    for f, h in PRIMARY.items():
        cells = []
        for s in (_t(_ic_series(df, f, h), h), _t(_ic_series(df, f, h, True), h), _t(_ic_series(sub, f, h), h)):
            cells.append(f"{s['ic']:+.4f} ({s['t']:+.2f}, {s['n']})" if np.isfinite(s["ic"]) else "—")
        L.append(f"| `{f}` × fwd{h} | " + " | ".join(cells) + " |")
    n_days = min(len(_ic_series(df, f, h)) for f, h in PRIMARY.items())
    if n_days < MIN_IC_DAYS:
        L += ["", f"> ⏳ **待累积**：IC 日 {n_days} < {MIN_IC_DAYS}，不判方向。"]
    return "\n".join(L)


def write_wide_report(date_str: str, output_dir: Path, db: Path = DB_PATH) -> Optional[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "wide_forward_oos.md"
    path.write_text(build_report(date_str, db), encoding="utf-8")
    logger.info(f"  宽宇宙前向报告: {path}")
    return path


def last_logged(db: Path = DB_PATH) -> Optional[str]:
    if not db.exists():
        return None
    c = _conn(db)
    d = c.execute("SELECT MAX(logged_date) AS d FROM wide_events").fetchone()["d"]
    c.close()
    return d


def wide_reminder(date_str: str, db: Path = DB_PATH) -> None:
    """main.py 用：超过 7 天没跑宽宇宙记录就提醒一句（不在 main.py 里跑，单次约 15 分钟）。"""
    last = last_logged(db)
    if last is None or (pd.Timestamp(date_str) - pd.Timestamp(last)).days >= 7:
        logger.warning(f"[WideForward] 上次宽宇宙记录 {last or '无'} ⇒ 本周该跑 `python forward_wide.py`")
