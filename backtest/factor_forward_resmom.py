"""F4′ 残差动量 —— 样本外(OOS)前向记录器（口径见仓库根 `PREREG-f4-forward.md`，2026-10-27 写定）。

样本内（`PREREG-factor-v1.md` 变更 1，探索性）IC +0.0336、t 1.69，分不开零；再在同一段历史上换参数
只会变成挑参数 ⇒ 只用冻结之后的新数据检验。结构仿 `factor_forward_revision`（同 DB、独立表）。

- 因子代码冻结在 `backtest/prereg_factor_v1.make_resid_mom`（sha 写死在预注册里）；sha 不符则**不记录**
  并告警 —— 记下来的数要是改过口径的，整张表就作废了。
- 入场锚 = `bar_date` 之后第一根收盘（t+1），不用信号同根收盘成交。
- 报告每次显示累计 IC/t，但**只是进度**；正式裁决只在每年 10 月后的固定查看日、边界 |t| ≥ 2.5
  （多次查看的纠正，见预注册 §3）。绝不回填。
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from backtest.factor_lab import VALIDATION_UNIVERSE

FACTOR_CODE = Path(__file__).with_name("prereg_factor_v1.py")
FACTOR_CODE_SHA = "959255e58c863385aeab2eca2c2e017f98d05c1589b64f52e7d2041779583b23"
LOOKBACK, EST = 252, 756
FWD_HORIZONS      = (5, 10, 20)
MAX_H             = max(FWD_HORIZONS)
PRIMARY_H         = 10
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS resmom_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_date TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            bar_date    TEXT NOT NULL,
            f4p         REAL NOT NULL,
            entry_price REAL,
            UNIQUE(logged_date, ticker)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS resmom_outcomes (
            event_id  INTEGER PRIMARY KEY REFERENCES resmom_events(id),
            eval_date TEXT NOT NULL,
            fwd5 REAL, fwd10 REAL, fwd20 REAL
        )
    """)
    c.commit()
    return c


def _code_frozen() -> bool:
    sha = hashlib.sha256(FACTOR_CODE.read_bytes()).hexdigest()
    if sha != FACTOR_CODE_SHA:
        logger.warning(f"[ResMomForward] 因子代码 sha 不符（{sha[:12]}… ≠ 预注册 {FACTOR_CODE_SHA[:12]}…）"
                       "——口径可能已变，本次不记录")
        return False
    return True


def f4p_last(df: pd.DataFrame, qqq_ret: pd.Series) -> Optional[float]:
    """只算最后一根 K 的 F4′：截取末 EST+1 根即与全序列在末点逐位相同（窗口只回看 EST 根收益）。"""
    from backtest.prereg_factor_v1 import make_resid_mom
    if df is None or len(df) < EST + 1:
        return None
    v = float(make_resid_mom([qqq_ret], LOOKBACK, EST)(df.iloc[-(EST + 1):]).iloc[-1])
    return v if np.isfinite(v) else None


def log_resmom_events(date_str: str, pipeline, universe=VALIDATION_UNIVERSE) -> int:
    """as-of 记录验证宇宙每票的 F4′ 末值。返回新增票数。"""
    if LOG_INTERVAL_DAYS <= 0 or not _code_frozen():
        return 0
    c = _conn()
    last = c.execute("SELECT MAX(logged_date) AS d FROM resmom_events").fetchone()["d"]
    c.close()
    if last is not None:
        gap = (pd.Timestamp(date_str).normalize() - pd.Timestamp(last).normalize()).days
        if gap < LOG_INTERVAL_DAYS:
            logger.debug(f"[ResMomForward] 距上次记录 {gap}d < {LOG_INTERVAL_DAYS}d，跳过")
            return 0
    qqq = pipeline.get_backtest_price("QQQ")
    if qqq is None or qqq.empty:
        logger.warning("[ResMomForward] QQQ 价格缺失，跳过当日记录")
        return 0
    qqq_ret = qqq["Close"].astype(float).pct_change()

    c = _conn()
    inserted = missing = 0
    for ticker in universe:
        try:
            df = pipeline.get_backtest_price(ticker)
        except Exception as exc:
            logger.warning(f"[ResMomForward] {ticker} 拉价格失败: {exc}")
            missing += 1
            continue
        v = f4p_last(df, qqq_ret)
        if v is None:
            missing += 1
            continue
        c.execute("INSERT OR IGNORE INTO resmom_events (logged_date, ticker, bar_date, f4p) "
                  "VALUES (?, ?, ?, ?)", (date_str, ticker, str(df.index[-1].date()), v))
        inserted += c.execute("SELECT changes()").fetchone()[0]
    c.commit()
    c.close()
    if inserted:
        logger.info(f"[ResMomForward] 记录 F4′ {inserted} 票 logged={date_str}"
                    + (f"（缺 {missing}）" if missing else ""))
    return inserted


def evaluate_resmom_pending(pipeline) -> int:
    """入场 = bar_date 之后第一根收盘；满 MAX_H 交易日后计 fwd5/10/20。"""
    c = _conn()
    pending = c.execute("""
        SELECT e.id, e.ticker, e.bar_date FROM resmom_events e
        LEFT JOIN resmom_outcomes o ON o.event_id = e.id WHERE o.event_id IS NULL
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
            logger.warning(f"[ResMomForward] {ticker} 拉价格失败: {exc}")
            continue
        if df is None or df.empty:
            continue
        for r in rows:
            future = df[df.index > r["bar_date"]]
            if len(future) < MAX_H + 1:
                continue
            entry = float(future["Close"].iloc[0])
            if not entry > 0:
                continue
            fwd = {h: float(future["Close"].iloc[h]) / entry - 1.0 for h in FWD_HORIZONS}
            c.execute("UPDATE resmom_events SET entry_price = ? WHERE id = ?", (entry, r["id"]))
            c.execute("INSERT OR REPLACE INTO resmom_outcomes VALUES (?,?,?,?,?)",
                      (r["id"], str(future.index[MAX_H].date()), fwd[5], fwd[10], fwd[20]))
            n += 1
    c.commit()
    c.close()
    if n:
        logger.info(f"[ResMomForward] 完成评估 {n} 条")
    return n


def _ic_stats(df: pd.DataFrame, h: int) -> dict:
    ics = []
    for _, g in df.groupby("logged_date"):
        s = g[["f4p", f"fwd{h}"]].dropna()
        if len(s) >= MIN_NAMES and s["f4p"].nunique() > 1:
            ics.append(s["f4p"].corr(s[f"fwd{h}"], method="spearman"))
    ic = pd.Series(ics, dtype=float).dropna()
    n = len(ic)
    if n < 2:
        return {"n": n, "ic": float(ic.mean()) if n else np.nan, "t": np.nan}
    ir = ic.mean() / ic.std(ddof=1) if ic.std(ddof=1) > 0 else np.nan
    n_eff = max(n * min(1.0, RECORD_SPACING_TD / h), 1.0)
    return {"n": n, "ic": float(ic.mean()), "t": float(ir * np.sqrt(n_eff))}


def build_report(date_str: str) -> str:
    c = _conn()
    n_ev = c.execute("SELECT COUNT(*) FROM resmom_events").fetchone()[0]
    n_days = c.execute("SELECT COUNT(DISTINCT logged_date) FROM resmom_events").fetchone()[0]
    first = c.execute("SELECT MIN(logged_date) FROM resmom_events").fetchone()[0]
    df = pd.read_sql_query("""
        SELECT e.*, o.fwd5, o.fwd10, o.fwd20 FROM resmom_events e
        JOIN resmom_outcomes o ON o.event_id = e.id
    """, c)
    c.close()

    L = ["# F4′ 残差动量 · 样本外(OOS)前向记录", "",
         f"生成日期：{date_str}　首条记录：{first or '—'}　记录日 {n_days}　记录 {n_ev} 条　已成熟 {len(df)} 条", "",
         f"**预注册**（`PREREG-f4-forward.md`）：主口径 fwd{PRIMARY_H}，方向 IC>0。"
         f"下表是**进度不是裁决**；正式查看每年 10 月后一次（首次 {FIRST_LOOK_YEAR}-10），边界 |t| ≥ {BOUNDARY_T}。", ""]
    if df.empty:
        L.append(f"> ⏳ 尚无成熟记录——每条记录需入场后 {MAX_H} 个交易日成熟。")
        return "\n".join(L)
    L += ["| " + " | ".join(f"fwd{h} IC (t, n)" for h in FWD_HORIZONS) + " |",
          "|" + "---|" * len(FWD_HORIZONS)]
    cells = []
    for h in FWD_HORIZONS:
        s = _ic_stats(df, h)
        ic = f"{s['ic']:+.4f}" if np.isfinite(s["ic"]) else "—"
        t = f"{s['t']:+.2f}" if np.isfinite(s["t"]) else "—"
        cells.append(f"{'**' if h == PRIMARY_H else ''}{ic} ({t}, {s['n']}){'**' if h == PRIMARY_H else ''}")
    L += ["| " + " | ".join(cells) + " |", ""]
    p = _ic_stats(df, PRIMARY_H)
    if p["n"] < MIN_IC_DAYS:
        L.append(f"> ⏳ **待累积**：主口径 IC 日 {p['n']} < {MIN_IC_DAYS}，不判方向。")
    else:
        L.append(f"> 主口径累计 IC {p['ic']:+.4f}、t {p['t']:+.2f}（进度）。"
                 f"只有正式查看日的 t ≥ +{BOUNDARY_T} 才算前向确认、≤ −{BOUNDARY_T} 才算证伪。")
    return "\n".join(L)


def write_resmom_forward_report(date_str: str, output_dir: Path) -> Optional[Path]:
    try:
        md = build_report(date_str)
    except Exception as exc:
        logger.warning(f"[ResMomForward] 报告生成失败: {exc}")
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "f4_resmom_oos.md"
    path.write_text(md, encoding="utf-8")
    logger.info(f"  F4′ 残差动量 OOS 报告: {path}")
    return path
