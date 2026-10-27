"""F5 回购强度 —— 样本外(OOS)前向记录器（口径见仓库根 `PREREG-f5-forward.md`，2026-10-27 写定）。

yfinance 季度现金流只有约 5 季、无 PIT 历史 ⇒ 不能回测，只能从今天起向前记（同 R11 EPS 修正）。
除因子值外，原始分量（TTM 回购、流通股、收盘、SBC、所用季度期末…）一并落库 ——
**这张表是本项目唯一的回购 PIT 历史**，日后改口径也能从原始分量重算，而不必回填。

- 回购行缺失 ≠ NaN：现金流表本身有 ≥4 个连续季度时记 0 并打 `rep_row_missing`
  （缺行的名字实测都是近期不回购的公司；当 NaN 处理会把不回购者整体剔出横截面，改变要检验的问题）。
- 入场锚 = 记录日之后第一根收盘（快照运行时现取，理由同 `factor_forward_revision`）。
- 报告每次显示累计 IC/t，只是**进度**；正式查看每年 10 月、边界 |t| ≥ 2.5（预注册 §3）。绝不回填。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from backtest.factor_lab import VALIDATION_UNIVERSE

FACTORS           = ("bb_float", "bb_adv", "net_shrink")
PRIMARY_FACTOR    = "bb_float"
RAW               = ("ttm_rep", "float_shares", "shares_out", "close", "adv20", "ttm_sbc", "rep_row_missing")
FWD_HORIZONS      = (10, 20, 60)
MAX_H             = max(FWD_HORIZONS)
PRIMARY_H         = 20
MIN_NAMES         = 8
MIN_IC_DAYS       = 30
LOG_INTERVAL_DAYS = 5
RECORD_SPACING_TD = 5
BOUNDARY_T        = 2.5
FIRST_LOOK_YEAR   = 2027

DB_PATH = Path("cache") / "forward_signals.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS buyback_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_date TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            q_end       TEXT,
            {", ".join(f"{f} REAL" for f in (*FACTORS, *RAW))},
            entry_price REAL,
            UNIQUE(logged_date, ticker)
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS buyback_outcomes (
            event_id  INTEGER PRIMARY KEY REFERENCES buyback_events(id),
            eval_date TEXT NOT NULL,
            {", ".join(f"fwd{h} REAL" for h in FWD_HORIZONS)}
        )
    """)
    c.commit()
    return c


def _consecutive(s: pd.Series, n: int) -> Optional[pd.Series]:
    """最近 n 个季度且相邻期末间隔 80–100 天，否则 None（yfinance 的列会跳季）。"""
    s = s.dropna().sort_index(ascending=False).iloc[:n]
    if len(s) < n:
        return None
    idx = pd.DatetimeIndex(s.index)
    gaps = np.array([(idx[i] - idx[i + 1]).days for i in range(len(idx) - 1)])
    return s if ((gaps > 80) & (gaps < 100)).all() else None


def _ttm(table: pd.DataFrame, row: str) -> tuple[Optional[float], Optional[str], bool]:
    """(TTM 和取正, 最新期末, 是否缺行)。缺行而表本身有 4 个连续季度 ⇒ 0。"""
    if table is None or table.empty:
        return None, None, False
    if row not in table.index:
        cols = _consecutive(pd.Series(1.0, index=table.columns), 4)
        return (0.0, str(cols.index[0].date()), True) if cols is not None else (None, None, True)
    s = _consecutive(table.loc[row].astype(float), 4)
    if s is None:
        return None, None, False
    return float(abs(s.sum())), str(s.index[0].date()), False


def buyback_snapshot(ticker: str, price: pd.DataFrame) -> Optional[dict]:
    """单票 as-of 快照：原始分量 + 三个因子值（取不到的留 None）。"""
    import yfinance as yf
    t = yf.Ticker(ticker)
    cf, inc, info = t.quarterly_cashflow, t.quarterly_income_stmt, t.info or {}
    if price is None or len(price) < 20:
        return None
    close = float(price["Close"].iloc[-1])
    adv20 = float((price["Close"] * price["Volume"]).iloc[-20:].mean())
    ttm_rep, q_end, missing = _ttm(cf, "Repurchase Of Capital Stock")
    ttm_sbc, _, _ = _ttm(cf, "Stock Based Compensation")
    flt, sh = info.get("floatShares"), info.get("sharesOutstanding")
    net = None
    if inc is not None and "Diluted Average Shares" in inc.index:
        s = _consecutive(inc.loc["Diluted Average Shares"].astype(float), 5)
        if s is not None and s.iloc[4] > 0:
            net = -(float(s.iloc[0]) / float(s.iloc[4]) - 1.0)
    bb_float = ttm_rep / (flt * close) if (ttm_rep is not None and flt and close > 0) else None
    bb_adv = (ttm_rep / 252) / adv20 if (ttm_rep is not None and adv20 > 0) else None
    return {"q_end": q_end, "bb_float": bb_float, "bb_adv": bb_adv, "net_shrink": net,
            "ttm_rep": ttm_rep, "float_shares": flt, "shares_out": sh, "close": close,
            "adv20": adv20, "ttm_sbc": ttm_sbc, "rep_row_missing": float(missing)}


def log_buyback_events(date_str: str, pipeline, universe=VALIDATION_UNIVERSE) -> int:
    """按周 as-of 快照验证宇宙的回购强度。返回新增票数。"""
    if LOG_INTERVAL_DAYS <= 0:
        return 0
    c = _conn()
    last = c.execute("SELECT MAX(logged_date) AS d FROM buyback_events").fetchone()["d"]
    c.close()
    if last is not None:
        gap = (pd.Timestamp(date_str).normalize() - pd.Timestamp(last).normalize()).days
        if gap < LOG_INTERVAL_DAYS:
            logger.debug(f"[BuybackForward] 距上次记录 {gap}d < {LOG_INTERVAL_DAYS}d，跳过")
            return 0

    cols = ("q_end", *FACTORS, *RAW)
    c = _conn()
    inserted = failed = no_factor = 0
    for ticker in universe:
        try:
            snap = buyback_snapshot(ticker, pipeline.get_backtest_price(ticker))
        except Exception as exc:
            logger.warning(f"[BuybackForward] {ticker} 快照失败: {exc}")
            failed += 1
            continue
        if snap is None:
            failed += 1
            continue
        if all(snap[f] is None for f in FACTORS):
            no_factor += 1
        c.execute(f"INSERT OR IGNORE INTO buyback_events (logged_date, ticker, {', '.join(cols)}) "
                  f"VALUES (?, ?, {', '.join('?' * len(cols))})",
                  (date_str, ticker, *[snap[k] for k in cols]))
        inserted += c.execute("SELECT changes()").fetchone()[0]
    c.commit()
    c.close()
    if inserted:
        logger.info(f"[BuybackForward] 记录回购快照 {inserted} 票 snapshot={date_str}"
                    + (f"（失败 {failed}，三因子全缺 {no_factor}）" if failed or no_factor else ""))
    return inserted


def evaluate_buyback_pending(pipeline) -> int:
    """入场 = 记录日之后第一根收盘；满 MAX_H 交易日后计 fwd10/20/60。"""
    c = _conn()
    pending = c.execute("""
        SELECT e.id, e.ticker, e.logged_date FROM buyback_events e
        LEFT JOIN buyback_outcomes o ON o.event_id = e.id WHERE o.event_id IS NULL
    """).fetchall()
    c.close()
    if not pending:
        return 0
    by_ticker: Dict[str, list] = defaultdict(list)
    for r in pending:
        by_ticker[r["ticker"]].append(r)

    c = _conn()
    n = 0
    for ticker, rows in by_ticker.items():
        try:
            df = pipeline.get_backtest_price(ticker)
        except Exception as exc:
            logger.warning(f"[BuybackForward] {ticker} 拉价格失败: {exc}")
            continue
        if df is None or df.empty:
            continue
        for r in rows:
            future = df[df.index > r["logged_date"]]
            if len(future) < MAX_H + 1:
                continue
            entry = float(future["Close"].iloc[0])
            if not entry > 0:
                continue
            fwd = [float(future["Close"].iloc[h]) / entry - 1.0 for h in FWD_HORIZONS]
            c.execute("UPDATE buyback_events SET entry_price = ? WHERE id = ?", (entry, r["id"]))
            c.execute(f"INSERT OR REPLACE INTO buyback_outcomes VALUES (?,?,{','.join('?' * len(fwd))})",
                      (r["id"], str(future.index[MAX_H].date()), *fwd))
            n += 1
    c.commit()
    c.close()
    if n:
        logger.info(f"[BuybackForward] 完成评估 {n} 条")
    return n


def _t(series: pd.Series, h: int) -> dict:
    s = series.dropna()
    n = len(s)
    if n < 2:
        return {"n": n, "v": float(s.mean()) if n else np.nan, "t": np.nan}
    ir = s.mean() / s.std(ddof=1) if s.std(ddof=1) > 0 else np.nan
    return {"n": n, "v": float(s.mean()), "t": float(ir * np.sqrt(max(n * min(1.0, RECORD_SPACING_TD / h), 1.0)))}


def _ic_series(df: pd.DataFrame, factor: str, h: int) -> pd.Series:
    ics = {}
    for d, g in df.groupby("logged_date"):
        s = g[[factor, f"fwd{h}"]].dropna()
        if len(s) >= MIN_NAMES and s[factor].nunique() > 1:
            ics[d] = s[factor].corr(s[f"fwd{h}"], method="spearman")
    return pd.Series(ics, dtype=float)


def _dual_series(df: pd.DataFrame, h: int) -> pd.Series:
    """每个记录日：两比率都 ≥ 当日中位数的名字 vs 其余，fwd 均值差。"""
    out = {}
    for d, g in df.groupby("logged_date"):
        s = g[["bb_float", "bb_adv", f"fwd{h}"]].dropna()
        if len(s) < MIN_NAMES:
            continue
        flag = (s["bb_float"] >= s["bb_float"].median()) & (s["bb_adv"] >= s["bb_adv"].median())
        if flag.any() and (~flag).any():
            out[d] = s.loc[flag, f"fwd{h}"].mean() - s.loc[~flag, f"fwd{h}"].mean()
    return pd.Series(out, dtype=float)


def build_report(date_str: str) -> str:
    c = _conn()
    n_ev = c.execute("SELECT COUNT(*) FROM buyback_events").fetchone()[0]
    n_days = c.execute("SELECT COUNT(DISTINCT logged_date) FROM buyback_events").fetchone()[0]
    first = c.execute("SELECT MIN(logged_date) FROM buyback_events").fetchone()[0]
    df = pd.read_sql_query("""
        SELECT e.*, o.fwd10, o.fwd20, o.fwd60 FROM buyback_events e
        JOIN buyback_outcomes o ON o.event_id = e.id
    """, c)
    c.close()

    L = ["# F5 回购强度 · 样本外(OOS)前向记录", "",
         f"生成日期：{date_str}　首条记录：{first or '—'}　记录日 {n_days}　记录 {n_ev} 条　已成熟 {len(df)} 条", "",
         f"**预注册**（`PREREG-f5-forward.md`）：主口径 `{PRIMARY_FACTOR}` × fwd{PRIMARY_H}，方向 IC>0。"
         f"下表是**进度不是裁决**；正式查看每年 10 月后一次（首次 {FIRST_LOOK_YEAR}-10），边界 |t| ≥ {BOUNDARY_T}。", ""]
    if df.empty:
        L.append(f"> ⏳ 尚无成熟记录——每条记录需入场后 {MAX_H} 个交易日成熟。")
        return "\n".join(L)

    L += ["| 量 | " + " | ".join(f"fwd{h} (t, n)" for h in FWD_HORIZONS) + " |",
          "|---|" + "---|" * len(FWD_HORIZONS)]
    rows = [(f, lambda h, f=f: _ic_series(df, f, h)) for f in FACTORS]
    rows.append(("dual_flag 差值", lambda h: _dual_series(df, h)))
    rows.append(("bb_float（剔缺行）", lambda h: _ic_series(df[df["rep_row_missing"] != 1], "bb_float", h)))
    for name, fn in rows:
        cells = []
        for h in FWD_HORIZONS:
            s = _t(fn(h), h)
            v = f"{s['v']:+.4f}" if np.isfinite(s["v"]) else "—"
            t = f"{s['t']:+.2f}" if np.isfinite(s["t"]) else "—"
            cells.append(f"{v} ({t}, {s['n']})")
        label = f"**{name}**" if name == PRIMARY_FACTOR else name
        L.append(f"| {label} | " + " | ".join(cells) + " |")
    L.append("")
    p = _t(_ic_series(df, PRIMARY_FACTOR, PRIMARY_H), PRIMARY_H)
    if p["n"] < MIN_IC_DAYS:
        L.append(f"> ⏳ **待累积**：主口径 IC 日 {p['n']} < {MIN_IC_DAYS}，不判方向。")
    else:
        L.append(f"> 主口径累计 IC {p['v']:+.4f}、t {p['t']:+.2f}（进度）。"
                 f"只有正式查看日的 t ≥ +{BOUNDARY_T} 才算前向确认、≤ −{BOUNDARY_T} 才算证伪。")
    return "\n".join(L)


def write_buyback_forward_report(date_str: str, output_dir: Path) -> Optional[Path]:
    try:
        md = build_report(date_str)
    except Exception as exc:
        logger.warning(f"[BuybackForward] 报告生成失败: {exc}")
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "f5_buyback_oos.md"
    path.write_text(md, encoding="utf-8")
    logger.info(f"  F5 回购强度 OOS 报告: {path}")
    return path
