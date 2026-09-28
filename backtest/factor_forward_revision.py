"""R11 分析师 EPS 修正因子 —— 样本外(OOS)前向记录器（仿 `factor_forward_amihud`）。

为什么只能向前记：Yahoo 的 `eps_trend` / `eps_revisions` 只有**当日快照**（当前 vs 7/30/60/90 天前），
没有历史 ⇒ 无法回测。而大盘股上「盈利预期修正」是比价格因子先验更强的一族（且它是价格之外的
信息 —— R6/R7/R10 已证明纯价格变形测不出新东西）。唯一诚实的路是：从上线日起按周快照全宇宙，
等前向收益成熟后算横截面 RankIC。**这张表本身就是本项目唯一的修正 PIT 历史**，
攒够后才可能做正式回测。

## 预注册（2026-09-28，首条记录之前写死）

- 宇宙：`VALIDATION_UNIVERSE`（78 只，与 R6/R7/R10/R11-EAR 同一横截面）。
- 频率：`LOG_INTERVAL_DAYS=5`（约每周一次；相邻记录前向窗口重叠，更密只加冗余）。
- 记录量（全部 as-of 当日快照，不回填）：
  - `rev30_fy`  = (0y 当前 − 0y 30 天前) / |0y 30 天前|  ← **主因子**
  - `rev90_fy`  = 同上，90 天
  - `breadth_fy` = (0y 30 天上修 − 下修) / (上修 + 下修)
  - `rev30_q` / `breadth_q` = 0q 口径（季度换期时 0q 会切到新季度，噪声大，仅作副口径）
- **主口径 = `rev30_fy` × fwd20**（修正因子的经济期限是 1–3 个月，不是 5 日）；fwd5/fwd10 并列。
- t 值：IC 日按周采样，fwd h 日窗口与相邻记录重叠 ⇒ 有效样本 `n_eff = n × min(1, 5/h)`。
- 结论门：主口径 IC 日 ≥ `MIN_IC_DAYS`(30) 才出方向判断；**t≥2 才谈 merge 进 quant**，
  之前一律「待累积」。按 R11 立项时的估算，IR≈0.2 量级需要数年才够 t=2 ——
  **近期的现实价值是看方向、攒 PIT 历史，不是出结论**。

诚实边界：绝不从任何历史源回填；前向收益用真实日历成熟；同 (日,票) 幂等。

⚠️ **入场锚 = 快照日之后的第一根收盘**（2026-09-28 code review 修正，首批记录成熟前）：
快照是运行时现取的，而价格数据的最后一根 K 总是昨天或更早。若拿「快照前最后一根收盘」入场，
隔夜/盘前发生的修正与它引起的跳空会被同时计入因子和前向收益 —— 伪造正 IC，且集中在财报日。
故 `logged_date` = 快照当天（墙钟日），`entry_price` 在成熟评估时取 `logged_date` **之后**第一根收盘；
盘后运行也不会拿同日收盘入场（那根收盘早于快照）。代价是最多晚一天入场，换口径干净。
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

FWD_HORIZONS      = (5, 10, 20)
MAX_H             = max(FWD_HORIZONS)
PRIMARY_FACTOR    = "rev30_fy"
PRIMARY_H         = 20
FACTORS           = ("rev30_fy", "rev90_fy", "breadth_fy", "rev30_q", "breadth_q")
MIN_NAMES         = 8        # 单日横截面最少名字数（与 factor_lab 一致）
MIN_IC_DAYS       = 30
LOG_INTERVAL_DAYS = 5        # 最小记录间隔（日历天）；0 或负 = 关闭
RECORD_SPACING_TD = 5        # 相邻记录约隔 5 个交易日，用于 n_eff

DB_PATH = Path("cache") / "forward_signals.db"


def _chg(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None or not np.isfinite(cur) or not np.isfinite(prev) or prev == 0:
        return None
    return (cur - prev) / abs(prev)


def _breadth(up: Optional[float], down: Optional[float]) -> Optional[float]:
    if up is None or down is None or (up + down) <= 0:
        return None
    return (up - down) / (up + down)


def revision_factors(snap: dict) -> Dict[str, Optional[float]]:
    """`YFinanceSource.get_eps_revisions` 快照 → 五个预注册量（缺哪个留 None）。"""
    fy, q = snap.get("0y", {}), snap.get("0q", {})
    return {
        "rev30_fy":   _chg(fy.get("current"), fy.get("d30")),
        "rev90_fy":   _chg(fy.get("current"), fy.get("d90")),
        "breadth_fy": _breadth(fy.get("up30"), fy.get("down30")),
        "rev30_q":    _chg(q.get("current"), q.get("d30")),
        "breadth_q":  _breadth(q.get("up30"), q.get("down30")),
    }


# ── 数据库 ────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS revision_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_date TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            {", ".join(f"{f} REAL" for f in FACTORS)},
            entry_price REAL,
            UNIQUE(logged_date, ticker)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS revision_outcomes (
            event_id  INTEGER PRIMARY KEY REFERENCES revision_events(id),
            eval_date TEXT NOT NULL,
            fwd5 REAL, fwd10 REAL, fwd20 REAL
        )
    """)
    c.commit()
    return c


# ── 记录 ──────────────────────────────────────────────────────────
def log_revision_events(date_str: str, pipeline, universe=VALIDATION_UNIVERSE) -> int:
    """按周 as-of 快照验证宇宙的 EPS 修正量。返回新增票数。"""
    if LOG_INTERVAL_DAYS <= 0:
        return 0
    c = _conn()
    last = c.execute("SELECT MAX(logged_date) AS d FROM revision_events").fetchone()["d"]
    c.close()
    if last is not None:
        gap = (pd.Timestamp(date_str).normalize() - pd.Timestamp(last).normalize()).days
        if gap < LOG_INTERVAL_DAYS:
            logger.debug(f"[RevisionForward] 距上次记录 {gap}d < {LOG_INTERVAL_DAYS}d，跳过")
            return 0

    c = _conn()
    inserted = missing = 0
    for ticker in universe:
        f = revision_factors(pipeline.yf.get_eps_revisions(ticker))
        if all(v is None for v in f.values()):
            missing += 1
            continue
        # entry_price 留空：成熟评估时取快照日之后第一根收盘（理由见模块 docstring）
        c.execute(
            f"INSERT OR IGNORE INTO revision_events (logged_date, ticker, {', '.join(FACTORS)}) "
            f"VALUES (?, ?, {', '.join('?' * len(FACTORS))})",
            (date_str, ticker, *[f[k] for k in FACTORS]),
        )
        inserted += c.execute("SELECT changes()").fetchone()[0]
    c.commit()
    c.close()
    if inserted:
        logger.info(f"[RevisionForward] 记录 EPS 修正快照 {inserted} 票 snapshot={date_str}"
                    + (f"（无修正数据 {missing}）" if missing else ""))
    return inserted


# ── 成熟评估 ──────────────────────────────────────────────────────
def evaluate_revision_pending(pipeline) -> int:
    """对满 MAX_H 交易日的记录计前向 5/10/20 日收益。"""
    c = _conn()
    pending = c.execute("""
        SELECT re.id, re.ticker, re.logged_date FROM revision_events re
        LEFT JOIN revision_outcomes ro ON ro.event_id = re.id WHERE ro.event_id IS NULL
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
            logger.warning(f"[RevisionForward] {ticker} 拉价格失败: {exc}")
            continue
        if df is None or df.empty:
            continue
        for r in rows:
            future = df[df.index > r["logged_date"]]   # 严格晚于快照日
            if len(future) < MAX_H + 1:
                continue
            entry = float(future["Close"].iloc[0])
            if not entry > 0:
                continue
            fwd = {h: float(future["Close"].iloc[h]) / entry - 1.0 for h in FWD_HORIZONS}
            c.execute("UPDATE revision_events SET entry_price = ? WHERE id = ?", (entry, r["id"]))
            c.execute("INSERT OR REPLACE INTO revision_outcomes VALUES (?,?,?,?,?)",
                      (r["id"], str(future.index[MAX_H].date()), fwd[5], fwd[10], fwd[20]))
            n += 1
    c.commit()
    c.close()
    if n:
        logger.info(f"[RevisionForward] 完成评估 {n} 条")
    return n


# ── 报告 ──────────────────────────────────────────────────────────
def _ic_stats(df: pd.DataFrame, factor: str, h: int) -> dict:
    ics = []
    for _, g in df.groupby("logged_date"):
        s = g[[factor, f"fwd{h}"]].dropna()
        if len(s) >= MIN_NAMES and s[factor].nunique() > 1:
            ics.append(s[factor].corr(s[f"fwd{h}"], method="spearman"))
    ic = pd.Series(ics, dtype=float).dropna()
    n = len(ic)
    if n < 2:
        return {"n": n, "ic": float(ic.mean()) if n else np.nan, "t": np.nan}
    ir = ic.mean() / ic.std(ddof=1) if ic.std(ddof=1) > 0 else np.nan
    n_eff = max(n * min(1.0, RECORD_SPACING_TD / h), 1.0)
    return {"n": n, "ic": float(ic.mean()), "t": float(ir * np.sqrt(n_eff))}


def build_report(date_str: str) -> str:
    c = _conn()
    n_ev = c.execute("SELECT COUNT(*) FROM revision_events").fetchone()[0]
    n_days = c.execute("SELECT COUNT(DISTINCT logged_date) FROM revision_events").fetchone()[0]
    first = c.execute("SELECT MIN(logged_date) FROM revision_events").fetchone()[0]
    df = pd.read_sql_query("""
        SELECT re.*, ro.fwd5, ro.fwd10, ro.fwd20 FROM revision_events re
        JOIN revision_outcomes ro ON ro.event_id = re.id
    """, c)
    c.close()

    L = ["# R11 分析师 EPS 修正 · 样本外(OOS)前向记录", "",
         f"生成日期：{date_str}　首条记录：{first or '—'}　记录日 {n_days}　记录 {n_ev} 条"
         f"　已成熟 {len(df)} 条", "",
         f"**预注册主口径**：`{PRIMARY_FACTOR}` × fwd{PRIMARY_H}（高修正 ⇒ 预期高前向收益）。"
         f"IC 日 ≥{MIN_IC_DAYS} 才判方向，t≥2 才谈 merge。", ""]
    if df.empty:
        L.append(f"> ⏳ 尚无成熟记录——每条记录需 {MAX_H} 个交易日成熟。")
        return "\n".join(L)

    L += ["| 因子 | " + " | ".join(f"fwd{h} IC (t, n)" for h in FWD_HORIZONS) + " |",
          "|---|" + "---|" * len(FWD_HORIZONS)]
    for f in FACTORS:
        cells = []
        for h in FWD_HORIZONS:
            s = _ic_stats(df, f, h)
            ic = f"{s['ic']:+.4f}" if np.isfinite(s["ic"]) else "—"
            t = f"{s['t']:+.2f}" if np.isfinite(s["t"]) else "—"
            cells.append(f"{ic} ({t}, {s['n']})")
        L.append(f"| {'**' + f + '**' if f == PRIMARY_FACTOR else f} | " + " | ".join(cells) + " |")
    L.append("")

    p = _ic_stats(df, PRIMARY_FACTOR, PRIMARY_H)
    if p["n"] < MIN_IC_DAYS:
        L.append(f"> ⏳ **待累积**：主口径 IC 日 {p['n']} < {MIN_IC_DAYS}，不判方向。")
    elif np.isfinite(p["t"]) and p["t"] >= 2:
        L.append(f"> ✅ 主口径 t={p['t']:+.2f} ≥ 2（IC {p['ic']:+.4f}）—— 可以开始讨论 merge 进 quant（仍须过 factor_lab 独立性门）。")
    else:
        L.append(f"> 主口径 IC {p['ic']:+.4f}、t={p['t']:+.2f}：方向"
                 f"{'与预注册一致' if p['ic'] > 0 else '与预注册相反'}，**未达 t≥2，不 merge**。")
    return "\n".join(L)


def write_revision_forward_report(date_str: str, output_dir: Path) -> Optional[Path]:
    try:
        md = build_report(date_str)
    except Exception as exc:
        logger.warning(f"[RevisionForward] 报告生成失败: {exc}")
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "r11_revision_oos.md"
    path.write_text(md, encoding="utf-8")
    logger.info(f"  R11 EPS 修正 OOS 报告: {path}")
    return path
