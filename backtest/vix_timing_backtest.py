"""R8 VIX 大盘拐点择时——触发器样本内回测（决策：wiring 前先 backtest）。

在 QQQ/SPY + VIX(FRED VIXCLS) 历史上重放择时触发器，量化其**前向指数收益**，对照
**无条件基线**（同期全样本平均前向收益）。诚实纪律（承 R1.3/R5）：不预设胜率，让证据说话；
比较 user 原始规则 vs 稳健化 refinement 是否真的更优。

抄底变体（前向收益越高越好）：
  V0 ZONE   = VIX≥28 且 (QQQ|SPY)回撤≥10%      （用户原始规则）
  V1 SETUP  = V0 且 VIX 掉头                     （+ 恐惧见顶 refinement）
  V2 CONFIRM= V1 且 指数缠论 b1 背驰（as-of 重放）（+ 结构确认，best-effort）
逃顶（前向收益应**低于**基线才说明减仓有据）：
  TOP_WARN  = VIX 抬高波谷 且 指数上行减速

事件用**上升沿**（今真昨假）计，避免同一 episode 每日重复灌水；前向收益在指数自身 close 上算。
数据全可得（指数 OHLC + VIX 历史，无 PIT 问题），非前视由 build_timing_panel 天然保证。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from signals.macro.vix_timing import build_timing_panel

HORIZONS = (10, 20, 60)   # 前向交易日
INDICES = ("QQQ", "SPY")


def _load():
    from data.pipeline import DataPipeline
    pl = DataPipeline()
    vdf = pl.fred.get_macro("VIXCLS")
    if vdf is None or vdf.empty:
        raise RuntimeError("VIXCLS 不可用——先跑 `python main.py` 预热 FRED cache")
    vix = vdf["value"].copy()
    vix.index = pd.to_datetime(vix.index)
    vix = vix.sort_index()
    idx: Dict[str, pd.DataFrame] = {}
    for t in INDICES:
        try:
            df = pl.get_backtest_price(t)
            if df is not None and not df.empty and "Close" in df.columns:
                idx[t] = df
        except Exception as e:
            logger.warning(f"[VIXTimingBT] {t} 价格失败: {e}")
    if not idx:
        raise RuntimeError("QQQ/SPY 价格不可用")
    return vix, idx, pl


def _fwd(close: pd.Series, h: int) -> pd.Series:
    return close.shift(-h) / close - 1.0


def _rising_edge(col: pd.Series) -> pd.DatetimeIndex:
    col = col.fillna(False).astype(bool)
    return col.index[col & ~col.shift(1, fill_value=False)]


def _stat(fwd_by_index: Dict[str, Dict[int, pd.Series]], dates: pd.DatetimeIndex) -> Dict:
    """事件日在各指数 × 各 horizon 的前向收益均值 / 命中率 / n。"""
    out: Dict[str, Dict[int, dict]] = {}
    for name, byh in fwd_by_index.items():
        out[name] = {}
        for h, fr in byh.items():
            vals = fr.reindex(dates).dropna()
            out[name][h] = {
                "n": int(len(vals)),
                "mean": float(vals.mean()) if len(vals) else float("nan"),
                "hit": float((vals > 0).mean()) if len(vals) else float("nan"),
            }
    return out


def _index_b1_asof(pl, name: str, df: pd.DataFrame, when: pd.Timestamp) -> bool:
    """as-of 在 ≤when 截断的指数上跑缠论，判是否 b1 背驰（best-effort，异常即 False）。"""
    try:
        from signals.chan.chan_signal import compute_chan_signal
        sub = df.loc[:when]
        if len(sub) < 60:
            return False
        res = compute_chan_signal(name, {name: sub})
        return getattr(res, "buy_point_type", None) == "b1" and bool(getattr(res, "divergence", False))
    except Exception:
        return False


def run(write_dir: Optional[Path] = None) -> str:
    vix, idx, pl = _load()
    panel = build_timing_panel(vix, idx)
    if panel.empty:
        return "面板为空"

    # 前向收益（各指数自身 close）
    fwd: Dict[str, Dict[int, pd.Series]] = {}
    for name, df in idx.items():
        c = df["Close"].dropna()
        fwd[name] = {h: _fwd(c, h) for h in HORIZONS}

    span = f"{panel.index.min():%Y-%m-%d} → {panel.index.max():%Y-%m-%d}"

    # 基线：全样本无条件前向收益
    all_dates = panel.index
    baseline = _stat(fwd, all_dates)

    # 变体事件
    variants = {
        "V0_ZONE(VIX≥28&DD≥10%)":  _rising_edge(panel["bottom_zone"]),
        "V1_SETUP(+VIX掉头)":       _rising_edge(panel["bottom_setup"]),
        "TOP_WARNING(逃顶)":        _rising_edge(panel["top_warning"]),
    }

    # V2 CONFIRMED：在 V1 setup 事件日 as-of 查指数 b1（best-effort）
    setup_dates = variants["V1_SETUP(+VIX掉头)"]
    confirmed: List[pd.Timestamp] = []
    for d in setup_dates:
        if any(_index_b1_asof(pl, name, df, d) for name, df in idx.items()):
            confirmed.append(d)
    if confirmed:
        variants["V2_CONFIRMED(+指数b1)"] = pd.DatetimeIndex(confirmed)

    stats = {k: _stat(fwd, v) for k, v in variants.items()}

    # ── 渲染报告 ──
    L: List[str] = []
    L.append("# R8 VIX 大盘拐点择时 · 触发器样本内回测")
    L.append("")
    L.append(f"样本区间：{span}　指数：{'/'.join(idx.keys())}　前向口径：{HORIZONS} 交易日")
    L.append("")
    L.append("> ⚠️ **样本内**（in-sample）：证据强度弱于样本外。抄底变体看前向收益是否**显著高于基线**，"
             "逃顶看是否**显著低于基线**。上升沿计事件（episode 去重），但恐慌 episode 稀少、"
             "彼此仍高度重叠——n 小、置信有限，勿 overclaim。")
    L.append("")

    def _fmt_row(label: str, st: Dict, name: str) -> str:
        cells = []
        for h in HORIZONS:
            s = st[name][h]
            if s["n"] == 0 or not np.isfinite(s["mean"]):
                cells.append("—")
            else:
                cells.append(f"{s['mean']:+.2%} (胜{s['hit']:.0%},n{s['n']})")
        return f"| {label} | " + " | ".join(cells) + " |"

    for name in idx.keys():
        L.append(f"## 前向{name}收益：事件 vs 基线")
        L.append("")
        L.append("| 触发 | " + " | ".join(f"fwd{h}" for h in HORIZONS) + " |")
        L.append("|------|" + "------|" * len(HORIZONS))
        L.append(_fmt_row("**基线(全样本无条件)**", baseline, name))
        for k, st in stats.items():
            L.append(_fmt_row(k, st, name))
        L.append("")
        # 判读（以 fwd20 为主口径）
        base20 = baseline[name][20]["mean"]
        L.append(f"**判读（fwd20，基线 {base20:+.2%}）**：")
        for k, st in stats.items():
            m = st[name][20]["mean"]; n = st[name][20]["n"]
            if n == 0 or not np.isfinite(m):
                continue
            edge = m - base20
            if "TOP" in k:
                verdict = "✅ 低于基线=减仓有据" if edge < 0 else "🔴 未低于基线=逃顶信号存疑"
            else:
                verdict = "✅ 高于基线=抄底有边" if edge > 0 else "🔴 未跑赢基线=该变体存疑"
            L.append(f"- {k}: {m:+.2%}（vs 基线 {edge:+.2%}, n={n}）→ {verdict}")
        L.append("")

    L.append("---")
    L.append("**诚实边界**：in-sample 触发器统计仅供**是否值得 wiring 进 live 决策**的判据；"
             "确认后仍需 R5.6 式无回填 OOS 累积。恐慌样本天然稀少，n 常个位数——梯度方向>单点显著性。")
    md = "\n".join(L)

    if write_dir is not None:
        write_dir.mkdir(parents=True, exist_ok=True)
        p = write_dir / "r8_vix_timing_backtest.md"
        p.write_text(md, encoding="utf-8")
        logger.info(f"[VIXTimingBT] 报告: {p}")
    return md


if __name__ == "__main__":
    print(run())
