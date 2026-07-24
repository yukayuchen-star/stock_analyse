"""R5 breakout 量能门——样本外(OOS)前向验证器。

动机：R5 breakout 门在 **in-sample**（127只/2021-2026 回测，`backtest/factor_eval.py`）
证明 KEEP(放量≥1.5×) 前向收益 ≫ DEMOTE(无量<1.0×) 且随 bo_ratio 单调。但 in-sample
调参有过拟合风险——真正的验证需**样本外**：从今日起逐日记录**扫描池中每一个 breakout
special 触发**（不限评级，因 breakout 是 10% 量化子信号极少翻动最终评级，Buy 门下样本会饿死），
标注其 bo_ratio 与 KEEP/MID/DEMOTE 桶，待前向 5/10/20 交易日成熟后计收益，
逐月累积以确认 KEEP≫DEMOTE 单调性在**未来数据**上仍成立。

与 `forward_tracker` 的区别：后者跟踪 Buy/OW 交易信号（含止损、按缠论类型）；本模块跟踪
**因子级触发的原始前向收益**（无止损，方法对齐 in-sample factor_eval），二者互不干扰、共用 DB。

诚实边界：**不从历史 cache 回填**（那是 in-sample 数据，回填=再污染）；只从上线日向前累积，
样本未达门槛前报告标注"待累积"。价值=确认/证伪 in-sample 门，非独立造信号。

流程：log_breakout_events()（每日）→ evaluate_pending()（≥20td 成熟后）→ build_report()。
存储：cache/forward_signals.db（与 forward_tracker 共库，独立表）。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from signals.quant.momentum import _special_signal

# ── 常量 ────────────────────────────────────────────────────────

FWD_HORIZONS = (5, 10, 20)       # 前向收益口径（交易日）
MAX_H        = max(FWD_HORIZONS)  # 事件成熟所需的最少未来交易日
KEEP_THR     = 1.5                # shipped breakout_thr：≥ KEEP / [1.0,KEEP) MID / <1.0 DEMOTE
PRIMARY_H    = 10                 # 主口径
MIN_BUCKET_N = 30                 # 单桶低于此数只做"待累积"提示
# in-sample 参照（factor_eval fwd10 exp；随 cache 快照微动，取近似）
INSAMPLE_REF = {"DEMOTE": 0.016, "MID": 0.021, "KEEP": 0.036}

DB_PATH = Path("cache") / "forward_signals.db"


def _bucket(bo_ratio: float) -> str:
    if bo_ratio >= KEEP_THR:
        return "KEEP"
    if bo_ratio >= 1.0:
        return "MID"
    return "DEMOTE"


# ── 数据库 ────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS factor_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_date  TEXT NOT NULL,
            ticker       TEXT NOT NULL,
            factor       TEXT NOT NULL,
            bo_ratio     REAL,
            bucket       TEXT,
            close_pos    REAL,
            entry_price  REAL,
            UNIQUE(logged_date, ticker, factor)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS factor_outcomes (
            event_id   INTEGER PRIMARY KEY REFERENCES factor_events(id),
            eval_date  TEXT NOT NULL,
            fwd5       REAL,
            fwd10      REAL,
            fwd20      REAL
        )
    """)
    c.commit()
    return c


# ── 事件记录 ──────────────────────────────────────────────────────

def log_breakout_events(
    prices: Dict[str, "pd.DataFrame"],
    date_str: str,
    tickers: Optional[set] = None,
) -> int:
    """记录当日扫描池中每个 breakout special 触发（as-of，仅用 ≤今日数据）。

    入场价/入场日锚定该收盘 bar（与 forward_tracker 同口径），避免评估期错位。
    同一 (日,票,factor) 已存在则跳过。返回新增数。

    tickers：仅记录该集合内的票；用于剔除基准 ETF（QQQ/SPY/^VIX 等——它们在 prices
    里供相对强度用，量能动态与个股不同，混入会污染 OOS 样本）。None=记录全部键。
    """
    allow = set(tickers) if tickers is not None else None
    c = _conn()
    inserted = 0
    for ticker, df in (prices or {}).items():
        if allow is not None and ticker not in allow:
            continue
        if df is None or df.empty or "Close" not in df.columns:
            continue
        try:
            _, aux = _special_signal(df)   # 默认 breakout 门 ON；仅用末行及之前
        except Exception as exc:
            logger.debug(f"[FactorForward] {ticker} special 失败: {exc}")
            continue
        if not aux.get("breakout_trig") or aux.get("breakout_vol_ratio") is None:
            continue
        bo = float(aux["breakout_vol_ratio"])
        entry_price = float(df["Close"].iloc[-1])
        entry_date  = str(pd.Timestamp(df.index[-1]).date())
        if entry_price <= 0:
            continue
        try:
            c.execute(
                """INSERT OR IGNORE INTO factor_events
                   (logged_date, ticker, factor, bo_ratio, bucket, close_pos, entry_price)
                   VALUES (?,?,?,?,?,?,?)""",
                (entry_date, ticker, "breakout", bo, _bucket(bo),
                 aux.get("close_pos"), entry_price),
            )
            if c.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as exc:
            logger.warning(f"[FactorForward] log {ticker}: {exc}")
    c.commit()
    c.close()
    if inserted:
        logger.info(f"[FactorForward] 记录 breakout OOS 事件 {inserted} 只 ({date_str})")
    return inserted


# ── 事件评估 ──────────────────────────────────────────────────────

def evaluate_pending(pipeline) -> int:
    """对满 MAX_H 交易日的事件计前向 5/10/20 日收益。返回本次评估数。"""
    c = _conn()
    pending = c.execute("""
        SELECT fe.* FROM factor_events fe
        LEFT JOIN factor_outcomes fo ON fo.event_id = fe.id
        WHERE fo.event_id IS NULL
        ORDER BY fe.logged_date
    """).fetchall()
    c.close()
    if not pending:
        return 0

    by_ticker: Dict[str, list] = defaultdict(list)
    for row in pending:
        by_ticker[row["ticker"]].append(row)

    c = _conn()
    evaluated = 0
    for ticker, rows in by_ticker.items():
        try:
            df = pipeline.get_backtest_price(ticker)
        except Exception as exc:
            logger.warning(f"[FactorForward] {ticker} 拉价格失败: {exc}")
            continue
        if df is None or df.empty:
            continue
        first_bar = df.index.min()
        for row in rows:
            logged_date = row["logged_date"]
            entry_price = row["entry_price"]
            if pd.Timestamp(logged_date) < first_bar or entry_price <= 0:
                continue
            future = df[df.index > logged_date]
            if len(future) < MAX_H:
                continue  # 未成熟，继续等待
            fwd = {}
            for h in FWD_HORIZONS:
                exit_price = float(future.iloc[h - 1]["Close"])
                fwd[h] = (exit_price - entry_price) / entry_price
            eval_date = str(future.index[MAX_H - 1].date())
            c.execute("""
                INSERT OR REPLACE INTO factor_outcomes
                (event_id, eval_date, fwd5, fwd10, fwd20) VALUES (?,?,?,?,?)
            """, (row["id"], eval_date, fwd[5], fwd[10], fwd[20]))
            evaluated += 1
    c.commit()
    c.close()
    if evaluated:
        logger.info(f"[FactorForward] 完成评估 breakout OOS 事件 {evaluated} 只")
    return evaluated


# ── 报告 ──────────────────────────────────────────────────────────

def build_report(date_str: str) -> str:
    """OOS breakout 门累积报告：KEEP/MID/DEMOTE 桶前向收益 + 单调性 + 是否确认 in-sample。"""
    c = _conn()
    rows = c.execute("""
        SELECT fe.bucket AS bucket, fo.fwd5 AS fwd5, fo.fwd10 AS fwd10, fo.fwd20 AS fwd20
        FROM factor_events fe JOIN factor_outcomes fo ON fo.event_id = fe.id
        WHERE fe.factor = 'breakout'
    """).fetchall()
    n_pending = c.execute("""
        SELECT COUNT(*) FROM factor_events fe
        LEFT JOIN factor_outcomes fo ON fo.event_id = fe.id
        WHERE fo.event_id IS NULL AND fe.factor='breakout'
    """).fetchone()[0]
    c.close()

    L = ["# R5 Breakout 门 · 样本外(OOS)前向验证", ""]
    L.append(f"生成日期：{date_str}　已成熟事件：{len(rows)}　待成熟：{n_pending}")
    L.append("")
    L.append(f"in-sample 参照（fwd{PRIMARY_H} exp）：DEMOTE≈+{INSAMPLE_REF['DEMOTE']:.3f}"
             f" → MID≈+{INSAMPLE_REF['MID']:.3f} → KEEP≈+{INSAMPLE_REF['KEEP']:.3f}（单调 ✓）")
    L.append("")

    if not rows:
        L.append("> ⏳ 尚无成熟事件——OOS 从上线日向前累积，约需 20 交易日首批成熟。")
        return "\n".join(L)

    agg: Dict[str, list] = defaultdict(list)
    for r in rows:
        agg[r["bucket"]].append(r)

    L.append(f"| 桶 | n | win@fwd{PRIMARY_H} | exp@fwd{PRIMARY_H} | exp@fwd5 | exp@fwd20 |")
    L.append("|----|---|------|------|------|------|")
    exp_by_bucket: Dict[str, float] = {}
    for b in ("DEMOTE", "MID", "KEEP"):
        rs = agg.get(b, [])
        if not rs:
            L.append(f"| {b} | 0 | — | — | — | — |")
            continue
        f10 = np.array([x["fwd10"] for x in rs], float)
        f5  = np.array([x["fwd5"]  for x in rs], float)
        f20 = np.array([x["fwd20"] for x in rs], float)
        exp_by_bucket[b] = float(f10.mean())
        flag = "" if len(rs) >= MIN_BUCKET_N else " ⚠️样本不足"
        L.append(f"| {b} | {len(rs)}{flag} | {(f10>0).mean():.3f} | "
                 f"{f10.mean():+.4f} | {f5.mean():+.4f} | {f20.mean():+.4f} |")

    # 单调性与确认判定
    L.append("")
    have_all = all(b in exp_by_bucket for b in ("DEMOTE", "MID", "KEEP"))
    enough   = all(len(agg.get(b, [])) >= MIN_BUCKET_N for b in ("DEMOTE", "KEEP"))
    if not have_all or not enough:
        L.append("> ⏳ **待累积**：KEEP/DEMOTE 桶样本未达门槛"
                 f"（各需 ≥{MIN_BUCKET_N}），暂不下 OOS 结论。")
    else:
        mono = exp_by_bucket["DEMOTE"] <= exp_by_bucket.get("MID", exp_by_bucket["DEMOTE"]) <= exp_by_bucket["KEEP"]
        sep  = exp_by_bucket["KEEP"] - exp_by_bucket["DEMOTE"]
        if mono and sep > 0:
            L.append(f"> ✅ **OOS 确认**：KEEP−DEMOTE 分离 =+{sep:.4f}、桶间单调 ✓，"
                     "样本外复现 in-sample breakout 门方向。")
        elif sep > 0:
            L.append(f"> 🟡 **部分确认**：KEEP>DEMOTE(+{sep:.4f}) 但中间档非单调，继续观察。")
        else:
            L.append(f"> 🔴 **OOS 背离**：KEEP−DEMOTE ={sep:+.4f} ≤0——in-sample 门未在样本外复现，"
                     "需复核是否过拟合（对照 [[insight-r5-volume-confirmation]]）。")
    return "\n".join(L)


def write_factor_forward_report(date_str: str, output_dir: Path) -> Optional[Path]:
    """写 OOS breakout 验证报告到 output/{date}/。无事件也写（含待累积提示）。"""
    try:
        md = build_report(date_str)
    except Exception as exc:
        logger.warning(f"[FactorForward] 报告生成失败: {exc}")
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "r5_breakout_oos.md"
    path.write_text(md, encoding="utf-8")
    logger.info(f"  R5 breakout OOS 报告: {path}")
    return path
