"""R7 amihud=压力-beta 假设——样本外(OOS)前向验证器（承接 [[project_r7_regime_beta]]）。

R7.2 in-sample 发现 amihud_20 的横截面 IC **随 VIX 单调升**（calm+.027→stress+.064→
panic+.205），提出唯一前瞻假设：**「非流动/低流动 beta 溢价集中于高 VIX 制度」**。但 in-sample
调参有过拟合风险，且 panic 桶仅 n_days=8（overlap 后≈1 独立观测）统计无用。**真正的检验须样本外**：
从上线日起逐日 as-of 记录**验证宇宙每只票的 amihud_20 值 + 当日 VIX 制度**，待前向 10TD 成熟后计
横截面 RankIC，按记录日的 VIX 桶累积，逐月确认/证伪「IC 随 VIX 升」的梯度。

与 `factor_forward`(R5 breakout)的区别：breakout 跟踪**事件级**触发的原始前向收益（桶=bo_ratio 档）；
本模块跟踪**横截面因子**——每个记录日跨票算一个 RankIC，桶=当日 VIX 制度。二者独立表、共用 DB。

诚实边界（严守 R5.6 纪律）：**绝不从历史 cache 回填**（那是 in-sample 数据、回填=再污染）；只从
上线日向前累积。amihud 值是 as-of（仅用 ≤今日的滚动 20 日），前向收益等真实日历成熟——非前视。
样本未达门槛前报告标注「待累积」；panic 桶大概率数年难满，如实标注、不硬下结论。价值=确认/证伪
in-sample 梯度，非独立造信号；即便确认，amihud 是**压力-beta**（归 VIX 门），非独立流动性 alpha。

流程：log_amihud_events()（每日 as-of 记录验证宇宙）→ evaluate_amihud_pending()（≥20TD 成熟）
→ build_report()（横截面 IC 按 VIX 桶累积 + 梯度判定）。存储：cache/forward_signals.db（独立表）。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from backtest.factor_lab import VALIDATION_UNIVERSE, _load_universe_prices
from signals.quant.regime_ic import VIX_BINS, VIX_LABELS
from signals.quant.transforms import DERIVED_CANDIDATES

# ── 常量 ────────────────────────────────────────────────────────
FWD_HORIZONS    = (5, 10, 20)        # 前向收益口径（交易日）
MAX_H           = max(FWD_HORIZONS)   # 事件成熟所需最少未来交易日
PRIMARY_H       = 10                  # 主口径
MIN_NAMES       = 8                   # 单日横截面算 IC 所需最少名字数（与 factor_lab 一致）
MIN_REGIME_DAYS = 30                  # 单制度桶低于此 IC 日数只做「待累积」提示
_AMIHUD         = DERIVED_CANDIDATES["amihud_20"]
# in-sample 参照（R7.2 fwd10 IC，随 cache 快照微动，取近似）
INSAMPLE_REF = {"calm(<15)": 0.027, "normal(15-25)": 0.013,
                "stress(25-35)": 0.064, "panic(>35)": 0.205}

DB_PATH = Path("cache") / "forward_signals.db"


def _regime(vix: float) -> Optional[str]:
    """VIX 标量 → 四档制度标签（与 regime_ic / CLAUDE.md 边界一致）。"""
    if vix is None or not np.isfinite(vix):
        return None
    idx = int(np.digitize([vix], VIX_BINS)[0]) - 1  # bins 含 -inf 起点
    if 0 <= idx < len(VIX_LABELS):
        return VIX_LABELS[idx]
    return None


# ── 数据库 ────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS amihud_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_date  TEXT NOT NULL,
            ticker       TEXT NOT NULL,
            amihud       REAL NOT NULL,
            vix_level    REAL,
            vix_regime   TEXT,
            entry_price  REAL NOT NULL,
            UNIQUE(logged_date, ticker)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS amihud_outcomes (
            event_id   INTEGER PRIMARY KEY REFERENCES amihud_events(id),
            eval_date  TEXT NOT NULL,
            fwd5       REAL,
            fwd10      REAL,
            fwd20      REAL
        )
    """)
    c.commit()
    return c


# ── 事件记录（每日 as-of，记录验证宇宙每票 amihud + 当日 VIX 制度）──
def log_amihud_events(date_str: str, pipeline, universe=VALIDATION_UNIVERSE) -> int:
    """as-of 记录验证宇宙每票的 amihud_20（末值）+ 当日 VIX 制度。返回新增数。

    自载验证宇宙（cache 命中即离线）以得**同 in-sample 的 78 票横截面**，与 R7.2 直接可比；
    不依赖 main 的小扫描池。VIX 取 FRED VIXCLS 当日末值（与宏观主轴同源）。
    同 (日,票) 已存在则跳过（幂等）。VIX 不可用则跳过当日（无法给制度标签）。
    """
    try:
        vix_df = pipeline.fred.get_macro("VIXCLS")
        vix = float(vix_df["value"].iloc[-1]) if vix_df is not None and not vix_df.empty else None
    except Exception as exc:
        logger.warning(f"[AmihudForward] VIX 取值失败: {exc}")
        vix = None
    reg = _regime(vix)
    if reg is None:
        logger.info("[AmihudForward] VIX 不可用，跳过当日 OOS 记录")
        return 0

    prices = _load_universe_prices(universe)
    c = _conn()
    inserted = 0
    for ticker, df in prices.items():
        if df is None or df.empty or "Close" not in df.columns or len(df) < 25:
            continue
        try:
            a = float(np.asarray(_AMIHUD(df), dtype=float)[-1])
        except Exception as exc:
            logger.debug(f"[AmihudForward] {ticker} amihud 失败: {exc}")
            continue
        entry_price = float(df["Close"].iloc[-1])
        entry_date  = str(pd.Timestamp(df.index[-1]).date())
        if not np.isfinite(a) or entry_price <= 0:
            continue
        try:
            c.execute(
                """INSERT OR IGNORE INTO amihud_events
                   (logged_date, ticker, amihud, vix_level, vix_regime, entry_price)
                   VALUES (?,?,?,?,?,?)""",
                (entry_date, ticker, a, vix, reg, entry_price),
            )
            if c.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as exc:
            logger.warning(f"[AmihudForward] log {ticker}: {exc}")
    c.commit()
    c.close()
    if inserted:
        logger.info(f"[AmihudForward] 记录 amihud OOS 事件 {inserted} 票 @ VIX={vix:.1f}({reg}) ({date_str})")
    return inserted


# ── 事件评估（满 MAX_H 交易日计前向收益）────────────────────────
def evaluate_amihud_pending(pipeline) -> int:
    """对满 MAX_H 交易日的 amihud 事件计前向 5/10/20 日收益。返回评估数。"""
    c = _conn()
    pending = c.execute("""
        SELECT ae.* FROM amihud_events ae
        LEFT JOIN amihud_outcomes ao ON ao.event_id = ae.id
        WHERE ao.event_id IS NULL
        ORDER BY ae.logged_date
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
            logger.warning(f"[AmihudForward] {ticker} 拉价格失败: {exc}")
            continue
        if df is None or df.empty:
            continue
        first_bar = df.index.min()
        for row in rows:
            logged_date, entry_price = row["logged_date"], row["entry_price"]
            if pd.Timestamp(logged_date) < first_bar or entry_price <= 0:
                continue
            future = df[df.index > logged_date]
            if len(future) < MAX_H:
                continue  # 未成熟
            fwd = {h: (float(future.iloc[h - 1]["Close"]) - entry_price) / entry_price
                   for h in FWD_HORIZONS}
            eval_date = str(future.index[MAX_H - 1].date())
            c.execute("""INSERT OR REPLACE INTO amihud_outcomes
                         (event_id, eval_date, fwd5, fwd10, fwd20) VALUES (?,?,?,?,?)""",
                      (row["id"], eval_date, fwd[5], fwd[10], fwd[20]))
            evaluated += 1
    c.commit()
    c.close()
    if evaluated:
        logger.info(f"[AmihudForward] 完成评估 amihud OOS 事件 {evaluated} 票")
    return evaluated


# ── 报告（横截面 RankIC 按 VIX 制度累积 + 梯度判定）─────────────
def _regime_ic_accum() -> pd.DataFrame:
    """成熟事件 → 每记录日横截面 RankIC(amihud, fwd10)，按 VIX 制度聚合。"""
    c = _conn()
    rows = c.execute("""
        SELECT ae.logged_date AS d, ae.vix_regime AS reg, ae.amihud AS amihud,
               ao.fwd10 AS fwd10
        FROM amihud_events ae JOIN amihud_outcomes ao ON ao.event_id = ae.id
    """).fetchall()
    c.close()
    if not rows:
        return pd.DataFrame(columns=["regime", "n_days", "ic_mean", "t_stat"])
    df = pd.DataFrame([dict(r) for r in rows])
    per_date = []
    for (d, reg), g in df.groupby(["d", "reg"]):
        s = g[["amihud", "fwd10"]].dropna()
        if len(s) < MIN_NAMES or s["amihud"].nunique() < 2:
            continue
        per_date.append({"regime": reg, "ic": s["amihud"].corr(s["fwd10"], method="spearman")})
    if not per_date:
        return pd.DataFrame(columns=["regime", "n_days", "ic_mean", "t_stat"])
    ic_df = pd.DataFrame(per_date).dropna()
    out = []
    for reg in VIX_LABELS:
        sub = ic_df[ic_df["regime"] == reg]["ic"]
        n = len(sub)
        if n == 0:
            out.append({"regime": reg, "n_days": 0, "ic_mean": np.nan, "t_stat": np.nan})
            continue
        mean = float(sub.mean())
        std = float(sub.std(ddof=1)) if n > 1 else np.nan
        ir = mean / std if (std and np.isfinite(std) and std > 0) else np.nan
        t = ir * np.sqrt(max(n / PRIMARY_H, 1.0)) if np.isfinite(ir) else np.nan
        out.append({"regime": reg, "n_days": n, "ic_mean": mean, "t_stat": t})
    return pd.DataFrame(out).set_index("regime")


def build_report(date_str: str) -> str:
    """OOS amihud 制度梯度累积报告：横截面 IC × VIX 四档 + 梯度确认/证伪。"""
    c = _conn()
    n_pending = c.execute("""
        SELECT COUNT(*) FROM amihud_events ae
        LEFT JOIN amihud_outcomes ao ON ao.event_id = ae.id
        WHERE ao.event_id IS NULL
    """).fetchone()[0]
    n_matured = c.execute("SELECT COUNT(*) FROM amihud_outcomes").fetchone()[0]
    c.close()

    tbl = _regime_ic_accum()
    L = ["# R7 amihud=压力-beta · 样本外(OOS)制度梯度验证", ""]
    L.append(f"生成日期：{date_str}　已成熟事件：{n_matured}　待成熟：{n_pending}")
    L.append("")
    L.append("**预注册假设**：amihud 横截面 IC 随 VIX 升（in-sample fwd10："
             + " → ".join(f"{k.split('(')[0]}≈+{v:.3f}" for k, v in INSAMPLE_REF.items()) + "）")
    L.append("")

    if tbl.empty or tbl["n_days"].sum() == 0:
        L.append("> ⏳ 尚无可算横截面 IC 的成熟记录日——OOS 从上线日累积，"
                 f"每日需 ≥{MIN_NAMES} 票同记、约 20TD 后首批成熟。")
        return "\n".join(L)

    L.append(f"| VIX 制度 | OOS IC日数 | OOS IC@fwd{PRIMARY_H} | t | in-sample 参照 |")
    L.append("|------|------|------|------|------|")
    for reg in VIX_LABELS:
        r = tbl.loc[reg]
        n = int(r["n_days"])
        ic_s = f"{r['ic_mean']:+.4f}" if np.isfinite(r["ic_mean"]) else "—"
        t_s = f"{r['t_stat']:+.2f}" if np.isfinite(r["t_stat"]) else "—"
        flag = "" if n >= MIN_REGIME_DAYS else f" ⚠️待累积(<{MIN_REGIME_DAYS})"
        L.append(f"| {reg} | {n}{flag} | {ic_s} | {t_s} | +{INSAMPLE_REF[reg]:.3f} |")
    L.append("")

    # 梯度判定：低 VIX vs 高 VIX（calm/normal 合为低，stress/panic 合为高）
    lo = [tbl.loc[r] for r in ("calm(<15)", "normal(15-25)")]
    hi = [tbl.loc[r] for r in ("stress(25-35)", "panic(>35)")]
    lo_ok = sum(int(x["n_days"]) for x in lo) >= MIN_REGIME_DAYS
    hi_ok = sum(int(x["n_days"]) for x in hi) >= MIN_REGIME_DAYS
    if not (lo_ok and hi_ok):
        L.append("> ⏳ **待累积**：低 VIX(calm+normal) 或高 VIX(stress+panic) 桶 IC 日数未达"
                 f" ≥{MIN_REGIME_DAYS}，暂不下 OOS 梯度结论。"
                 "（panic 稀有、大概率数年难满——如实标注，不硬下结论）")
    else:
        def _wmean(xs):
            ns = np.array([x["n_days"] for x in xs], float)
            ms = np.array([x["ic_mean"] for x in xs], float)
            m = np.isfinite(ms) & (ns > 0)
            return float((ms[m] * ns[m]).sum() / ns[m].sum()) if m.any() else np.nan
        ic_lo, ic_hi = _wmean(lo), _wmean(hi)
        grad = ic_hi - ic_lo
        if np.isfinite(grad) and grad > 0 and ic_hi > 0:
            L.append(f"> ✅ **OOS 确认梯度**：高VIX IC(+{ic_hi:.4f}) > 低VIX IC({ic_lo:+.4f})、"
                     f"差 =+{grad:.4f}——样本外复现「amihud 溢价集中于压力期」。"
                     "仍属**压力-beta**归 VIX 门，非独立 alpha（勿并入 quant sleeve）。")
        else:
            L.append(f"> 🔴 **OOS 证伪梯度**：高VIX IC({ic_hi:+.4f}) − 低VIX IC({ic_lo:+.4f}) "
                     f"={grad:+.4f} ≤0——in-sample 梯度未在样本外复现，假设不成立"
                     "（对照 [[project_r7_regime_beta]]，与 R5 pullback 门同类证伪风险）。")
    return "\n".join(L)


def write_amihud_forward_report(date_str: str, output_dir: Path) -> Optional[Path]:
    """写 OOS amihud 制度梯度报告到 output/{date}/。无事件也写（含待累积提示）。"""
    try:
        md = build_report(date_str)
    except Exception as exc:
        logger.warning(f"[AmihudForward] 报告生成失败: {exc}")
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "r7_amihud_regime_oos.md"
    path.write_text(md, encoding="utf-8")
    logger.info(f"  R7 amihud 制度 OOS 报告: {path}")
    return path
