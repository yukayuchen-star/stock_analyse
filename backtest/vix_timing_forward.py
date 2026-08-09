"""R8 VIX 大盘拐点择时——样本外(OOS)前向验证器（承 [[project_r8_vix_swing_timing]]）。

R8 触发器**样本内**强（SETUP fwd10 +3.69%/胜83%、TOP_WARNING fwd20 −0.53%<基线），但 n 小
（恐慌 episode 稀少、CONFIRMED 仅 n=2）。**真正的检验须样本外**：从上线日起逐日记录**当日 live 触发
状态**（BOTTOM_SETUP / BOTTOM_CONFIRMED / TOP_WARNING）+ 指数入场价，待前向 5/10/20TD 成熟后计
前向指数收益，按事件类型累积、collapse 成 episode，逐月确认/证伪样本内结论。

与 `factor_forward`(R5 breakout)同为**事件级**前向收益模型；与 `factor_forward_amihud`(横截面 RankIC)
不同——本模块是市场级择时事件、桶=事件类型。三者独立表、共用 cache/forward_signals.db。

诚实边界（严守 R5.6 纪律）：**绝不回填历史**（历史已被 backtest 覆盖=in-sample；回填=再污染）；
只从上线日向前累积。触发状态与入场价皆 as-of（面板末行、close[≤今]），前向收益等真实日历成熟——非前视。
每事件类型未达 MIN_EPISODES 前报告标注「待累积」，不硬下结论。价值=确认/证伪样本内，非独立造信号。

流程：log_vix_timing_event()（每日 as-of 记录 live 触发）→ evaluate_vix_timing_pending()（≥MAX_H 成熟）
→ build_report()（前向收益按事件类型 collapse episode 累积 + 对照 in-sample 判读）。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

# ── 常量 ────────────────────────────────────────────────────────
FWD_HORIZONS      = (5, 10, 20)     # 前向收益口径（交易日）
MAX_H             = max(FWD_HORIZONS)
PRIMARY_H         = 10
INDICES           = ("QQQ", "SPY")
EPISODE_GAP_DAYS  = 7               # 同类事件相邻记录日间隔 >此=新 episode（去重连续活跃日）
MIN_EPISODES      = 5               # 单事件类型 episode 数低于此只做「待累积」提示
# 记录的触发状态（ZONE 是 watch-only/接飞刀，不记）
_TRACKED = {"BOTTOM_SETUP", "BOTTOM_CONFIRMED", "TOP_WARNING"}

# in-sample 参照（backtest QQQ 前向收益；随 cache 快照微动，取近似）
INSAMPLE_REF = {
    "BOTTOM_SETUP":     {10: 0.0369, 20: 0.0447},
    "BOTTOM_CONFIRMED": {10: 0.0292, 20: 0.0886},
    "TOP_WARNING":      {10: 0.0010, 20: -0.0053},
}
BASELINE_REF = {10: 0.0063, 20: 0.0125}   # 无条件基线（QQQ）

DB_PATH = Path("cache") / "forward_signals.db"


# ── 数据库 ────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS vix_timing_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_date  TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            index_name   TEXT NOT NULL,
            entry_price  REAL NOT NULL,
            vix_level    REAL,
            vix_regime   TEXT,
            vix_tier     TEXT,
            max_dd       REAL,
            UNIQUE(logged_date, event_type, index_name)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS vix_timing_outcomes (
            event_id   INTEGER PRIMARY KEY REFERENCES vix_timing_events(id),
            eval_date  TEXT NOT NULL,
            fwd5       REAL,
            fwd10      REAL,
            fwd20      REAL
        )
    """)
    c.commit()
    return c


# ── 事件记录（每日 as-of 记录当日 live 触发状态）──────────────────
def log_vix_timing_event(date_str: str, swing, index_prices: Dict[str, pd.DataFrame]) -> int:
    """记录当日 live 触发（BOTTOM_SETUP/CONFIRMED / TOP_WARNING）+ 各指数入场价。返回新增行数。

    swing: macro.swing_timing（VixTimingResult）；index_prices: 含 QQQ/SPY 的价格字典。
    同 (日,事件,指数) 幂等；ZONE/WATCH 不记（非交易触发）。绝不回填——只记今日 live 态。
    """
    if swing is None:
        return 0
    events: List[str] = []
    if swing.bottom_state == "CONFIRMED":
        events.append("BOTTOM_CONFIRMED")
    elif swing.bottom_state == "SETUP":
        events.append("BOTTOM_SETUP")
    if swing.top_state == "WARNING":
        events.append("TOP_WARNING")
    events = [e for e in events if e in _TRACKED]
    if not events:
        return 0

    logged_date = str(pd.Timestamp(swing.date).date()) if swing.date is not None else date_str
    c = _conn()
    inserted = 0
    for etype in events:
        for name in INDICES:
            df = index_prices.get(name)
            if df is None or df.empty or "Close" not in df.columns:
                continue
            try:
                entry = float(df.loc[:logged_date]["Close"].iloc[-1])
            except Exception:
                try:
                    entry = float(df["Close"].iloc[-1])
                except Exception:
                    continue
            if not np.isfinite(entry) or entry <= 0:
                continue
            try:
                c.execute(
                    """INSERT OR IGNORE INTO vix_timing_events
                       (logged_date, event_type, index_name, entry_price,
                        vix_level, vix_regime, vix_tier, max_dd)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (logged_date, etype, name, entry, swing.vix,
                     None, swing.vix_tier, swing.max_drawdown),
                )
                if c.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
            except Exception as exc:
                logger.warning(f"[VIXTimingFwd] log {etype}/{name}: {exc}")
    c.commit()
    c.close()
    if inserted:
        logger.info(f"[VIXTimingFwd] 记录 OOS 择时事件 {inserted} 行 "
                    f"[{'/'.join(events)}] @ {logged_date}")
    return inserted


# ── 事件评估（满 MAX_H 交易日计前向指数收益）────────────────────
def evaluate_vix_timing_pending(pipeline) -> int:
    """对满 MAX_H 交易日的择时事件计前向 5/10/20 日**指数**收益。返回评估数。"""
    c = _conn()
    pending = c.execute("""
        SELECT e.* FROM vix_timing_events e
        LEFT JOIN vix_timing_outcomes o ON o.event_id = e.id
        WHERE o.event_id IS NULL
        ORDER BY e.logged_date
    """).fetchall()
    c.close()
    if not pending:
        return 0

    by_index: Dict[str, list] = defaultdict(list)
    for row in pending:
        by_index[row["index_name"]].append(row)

    c = _conn()
    evaluated = 0
    for name, rows in by_index.items():
        try:
            df = pipeline.get_backtest_price(name)
        except Exception as exc:
            logger.warning(f"[VIXTimingFwd] {name} 拉价格失败: {exc}")
            continue
        if df is None or df.empty:
            continue
        first_bar = df.index.min()
        aged_out = 0
        for row in rows:
            logged_date, entry = row["logged_date"], row["entry_price"]
            if pd.Timestamp(logged_date) < first_bar:
                aged_out += 1  # 早于回测窗口起点：当前窗口无法定位入场bar
                continue
            if entry <= 0:
                continue
            future = df[df.index > logged_date]
            if len(future) < MAX_H:
                continue  # 未成熟
            fwd = {h: (float(future.iloc[h - 1]["Close"]) - entry) / entry for h in FWD_HORIZONS}
            eval_date = str(future.index[MAX_H - 1].date())
            c.execute("""INSERT OR REPLACE INTO vix_timing_outcomes
                         (event_id, eval_date, fwd5, fwd10, fwd20) VALUES (?,?,?,?,?)""",
                      (row["id"], eval_date, fwd[5], fwd[10], fwd[20]))
            evaluated += 1
        if aged_out:
            logger.warning(f"[VIXTimingFwd] {name} {aged_out} 个待成熟事件 logged_date 早于回测窗口起点")
    c.commit()
    c.close()
    if evaluated:
        logger.info(f"[VIXTimingFwd] 完成评估 OOS 择时事件 {evaluated} 行")
    return evaluated


# ── 报告（前向收益按事件类型 collapse episode 累积 + 对照 in-sample）──
def _episode_first(dates: List[str]) -> List[str]:
    """把同类事件的连续活跃日 collapse 成 episode，返回每个 episode 的首日。"""
    if not dates:
        return []
    ds = sorted(pd.Timestamp(d) for d in set(dates))
    firsts = [ds[0]]
    for prev, cur in zip(ds, ds[1:]):
        if (cur - prev).days > EPISODE_GAP_DAYS:
            firsts.append(cur)
    return [str(d.date()) for d in firsts]


def _accum() -> Dict[str, Dict]:
    """成熟事件 → 每事件类型：episode 首日的前向收益均值/命中率/n（QQQ 主口径）。"""
    c = _conn()
    rows = c.execute("""
        SELECT e.event_type AS et, e.index_name AS ix, e.logged_date AS d,
               o.fwd5 AS fwd5, o.fwd10 AS fwd10, o.fwd20 AS fwd20
        FROM vix_timing_events e JOIN vix_timing_outcomes o ON o.event_id = e.id
    """).fetchall()
    c.close()
    if not rows:
        return {}
    df = pd.DataFrame([dict(r) for r in rows])
    out: Dict[str, Dict] = {}
    for et in sorted(df["et"].unique()):
        out[et] = {}
        for ix in INDICES:
            sub = df[(df["et"] == et) & (df["ix"] == ix)]
            firsts = set(_episode_first(list(sub["d"])))
            ep = sub[sub["d"].isin(firsts)]
            per_h = {}
            for h in FWD_HORIZONS:
                vals = ep[f"fwd{h}"].dropna()
                per_h[h] = {"n": int(len(vals)),
                            "mean": float(vals.mean()) if len(vals) else float("nan"),
                            "hit": float((vals > 0).mean()) if len(vals) else float("nan")}
            out[et][ix] = per_h
    return out


def build_report(date_str: str) -> str:
    c = _conn()
    n_pending = c.execute("""
        SELECT COUNT(*) FROM vix_timing_events e
        LEFT JOIN vix_timing_outcomes o ON o.event_id = e.id
        WHERE o.event_id IS NULL
    """).fetchone()[0]
    n_matured = c.execute("SELECT COUNT(*) FROM vix_timing_outcomes").fetchone()[0]
    c.close()

    acc = _accum()
    L = ["# R8 VIX 大盘拐点择时 · 样本外(OOS)前向验证", ""]
    L.append(f"生成日期：{date_str}　已成熟事件行：{n_matured}　待成熟：{n_pending}")
    L.append("")
    L.append("> OOS 从上线日累积**不回填**。抄底看前向指数收益是否 >0 且≈in-sample；逃顶看是否 <基线。"
             f"episode 去重(间隔>{EPISODE_GAP_DAYS}日)后每类 <{MIN_EPISODES} 只标「待累积」，不硬下结论。")
    L.append("")

    if not acc:
        L.append("> ⏳ 尚无成熟的择时事件——OOS 从上线日累积，触发发生后约 20TD 成熟。"
                 "（若长期无事件，说明当前无大盘拐点信号，正常）")
        return "\n".join(L)

    for et in ("BOTTOM_CONFIRMED", "BOTTOM_SETUP", "TOP_WARNING"):
        if et not in acc:
            continue
        L.append(f"## {et}")
        L.append("")
        L.append("| 指数 | " + " | ".join(f"fwd{h}" for h in FWD_HORIZONS)
                 + f" | in-sample fwd{PRIMARY_H} |")
        L.append("|------|" + "------|" * (len(FWD_HORIZONS) + 1))
        for ix in INDICES:
            ph = acc[et][ix]
            cells = []
            for h in FWD_HORIZONS:
                s = ph[h]
                cells.append("—" if s["n"] == 0 or not np.isfinite(s["mean"])
                             else f"{s['mean']:+.2%}(胜{s['hit']:.0%},n{s['n']})")
            ref = INSAMPLE_REF.get(et, {}).get(PRIMARY_H)
            ref_s = f"{ref:+.2%}" if ref is not None else "—"
            L.append(f"| {ix} | " + " | ".join(cells) + f" | {ref_s} |")
        # 判读（QQQ, fwd10 主口径, episode 去重后 n）
        q = acc[et]["QQQ"][PRIMARY_H]
        n, m = q["n"], q["mean"]
        if n < MIN_EPISODES or not np.isfinite(m):
            L.append(f"\n> ⏳ **待累积**：QQQ episode n={n} < {MIN_EPISODES}，暂不下 OOS 结论。")
        else:
            base = BASELINE_REF[PRIMARY_H]
            if et.startswith("BOTTOM"):
                ok = m > base
                L.append(f"\n> {'✅ OOS 确认' if ok else '🔴 OOS 证伪'}：QQQ fwd{PRIMARY_H}={m:+.2%} "
                         f"{'>' if ok else '≤'} 基线{base:+.2%}——"
                         + ("样本外复现「掉头确认后抄底有边」。" if ok
                            else "样本外未跑赢基线，抄底触发存疑（对照 in-sample，警惕照搬翻车）。"))
            else:  # TOP
                ok = m < base
                L.append(f"\n> {'✅ OOS 确认' if ok else '🔴 OOS 证伪'}：QQQ fwd{PRIMARY_H}={m:+.2%} "
                         f"{'<' if ok else '≥'} 基线{base:+.2%}——"
                         + ("样本外复现「逃顶后跑输基线=减仓有据」。" if ok
                            else "样本外未跑输基线，逃顶信号存疑。"))
        L.append("")

    L.append("---")
    L.append("**诚实边界**：in-sample≠OOS；恐慌/拐点 episode 天然稀少，达 MIN_EPISODES 需数年。"
             "即便 OOS 确认，抄底仍走 governed sleeve、逃顶仍 trim-only——门控非造信号。")
    return "\n".join(L)


def write_vix_timing_forward_report(date_str: str, output_dir: Path) -> Optional[Path]:
    try:
        md = build_report(date_str)
    except Exception as exc:
        logger.warning(f"[VIXTimingFwd] 报告生成失败: {exc}")
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "r8_vix_timing_oos.md"
    path.write_text(md, encoding="utf-8")
    logger.info(f"  R8 大盘择时 OOS 报告: {path}")
    return path
