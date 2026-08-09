"""市场级 VIX 大盘拐点择时（R8）——给系统补**第三轴：均值回归 / 制度反转**。

## 为什么需要它（强洞察）
现有两轴其实同向——都是**顺势延续**：
  · 缠论(55%) 找 b1/b2/b3 = 「在既有趋势内买回调」，结构上**在每个大级别拐点都必然滞后**；
  · 宏观 VIX 门(35%) 是**同步风险节流阀**：VIX 高→砍仓。它对恐惧做**反应**，不**择时反转**。
两轴都抓不到**大盘摆动高低点**。且存在一处尖锐张力：现门控是**顺周期**的（VIX>35→仓位0%），
而本策略是**逆周期**的（VIX>28+回撤10%→抄底）——系统恰在最该抄底时让你空仓。

本模块即那条缺失的轴：**市场级（指数非个股）、逆势、领先**，与缠论延续正交、与 VIX 节流阀
互补又张力（由 governed 逆势 sleeve 化解，见 risk 层）。呼应 R7 结论——高波/高beta 的超额
集中于高 VIX 制度=被补偿 beta：R7 说了压力期**持谁**（高beta），本模块说**何时**（VIX尖峰+回撤+掉头）。

## 规则（用户研究 + 稳健化 refinement，全部无前视）
临界 VIX：28 / 32 / 40 / 60（>40 仅严重恐慌/危机，历史罕见）。

**抄底（逆势多，分级非二元）**：
  · 必要*区域* ZONE：VIX≥28 且 QQQ 或 SPY 回撤≥10%（对滚动峰值）。
  · **稳健化关键**：裸「VIX≥28+回撤≥10%」在 2008/2022 曾**连续数月为真而大盘续跌**——它是*区域*不是
    *触发*。升级为触发需**恐惧见顶+价格企稳**：+ VIX **掉头**(自尖峰回落、跌破短EMA)=SETUP；
    再 + 指数自身**缠论 b1 背驰**(创新低+MACD衰竭)=CONFIRMED（两轴锁：宏观标区域、缠论确认结构）。

**逃顶（减仓预警，非做空——保守单边，同周线SMA哲学）**：
  · VIX **抬高的波谷**(higher-lows，用已确认波谷)=复杂化侵蚀 → WATCH；
  · 再 + 指数**上行速率放缓**(近高位、仍升但 ROC 减速)=WARNING → 止盈/收紧止损/降新仓上限。

无前视纪律：回撤对滚动峰值(≤t)；VIX 波谷/波峰只用**右端确认**极值(同缠论定笔)；动量用 shift 回看；
一切 close[≤t]。右端最后一个波谷未确认前不参与（承接 [[insight_chan_right_edge]] 右端稳定性）。

live 取面板末行；回测逐日重放（同一 build_timing_panel，天然 as-of）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 阈值常量（用户研究 28/32/40/60）───────────────────────────────
VIX_ELEVATED = 28.0    # 抄底必要门槛下沿
VIX_STRONG   = 32.0
VIX_PANIC    = 40.0    # 仅严重恐慌 / 经济危机
VIX_EXTREME  = 60.0    # 世代级（2008 / 2020）
DD_MIN       = 0.10    # 单次回撤 ≥10%
DD_PEAK_WIN  = 120     # 回撤参照滚动峰值窗口（~6 个月交易日）
VIX_EMA_SPAN = 10      # VIX 掉头(roll-over)的短 EMA
ROLLOVER_RETRACE = 0.10  # VIX 自近 20 日尖峰回落 ≥10% 才算掉头
PIVOT_CONFIRM = 3      # VIX 波谷/波峰两侧确认 bar 数（右端稳定性）
MOM_WIN      = 20      # 指数动量窗口（ROC）
NEAR_HIGH_PCT = 0.97   # 「近高位」= ≥滚动峰值×0.97

# VIX 抄底分档 → 逆势 tranche 建议上限（占组合；仅建议，风控层最终裁决）
# (阈值下沿, 标签, tranche 上限)
BOTTOM_TIERS: List[Tuple[float, str, float]] = [
    (VIX_EXTREME,  "generational(>=60)", 1.00),
    (VIX_PANIC,    "panic(40-60)",       0.75),
    (VIX_STRONG,   "strong(32-40)",      0.50),
    (VIX_ELEVATED, "elevated(28-32)",    0.30),
]


# ── 序列级 no-lookahead 计算原语 ──────────────────────────────────
def rolling_drawdown(close: pd.Series, win: int = DD_PEAK_WIN) -> pd.Series:
    """对滚动峰值(≤t)的回撤比例（0..1）。无前视：峰值只回看。"""
    peak = close.rolling(win, min_periods=1).max()
    return (peak - close) / peak.replace(0, np.nan)


def _confirmed_trough_pair(s: pd.Series, confirm: int = PIVOT_CONFIRM
                           ) -> Tuple[pd.Series, pd.Series]:
    """每个 t 返回(最近确认波谷值, 上一确认波谷值)。波谷 i 于 t=i+confirm 起才可见（右端确认）。"""
    vals = s.to_numpy(dtype=float)
    n = len(vals)
    troughs: List[Tuple[int, float]] = []  # (confirm_pos, value)
    for i in range(confirm, n - confirm):
        w = vals[i - confirm:i + confirm + 1]
        if np.isnan(vals[i]) or np.isnan(w).all():
            continue
        if vals[i] == np.nanmin(w):
            troughs.append((i + confirm, vals[i]))  # 确认于 i+confirm
    troughs.sort()
    last1 = np.full(n, np.nan)
    last2 = np.full(n, np.nan)
    seq: List[float] = []
    ci = 0
    for t in range(n):
        while ci < len(troughs) and troughs[ci][0] <= t:
            seq.append(troughs[ci][1])
            ci += 1
        if len(seq) >= 1:
            last1[t] = seq[-1]
        if len(seq) >= 2:
            last2[t] = seq[-2]
    return pd.Series(last1, index=s.index), pd.Series(last2, index=s.index)


def vix_higher_lows_series(vix: pd.Series, confirm: int = PIVOT_CONFIRM) -> pd.Series:
    """VIX 抬高的波谷：最近两个已确认波谷递增 → True。"""
    l1, l2 = _confirmed_trough_pair(vix, confirm)
    return (l1 > l2) & l1.notna() & l2.notna()


def vix_rollover_series(vix: pd.Series) -> pd.Series:
    """VIX 掉头：近 20 日曾 ≥28、现自尖峰回落 ≥10% 且跌破短 EMA = 恐惧见顶回落。"""
    ema = vix.ewm(span=VIX_EMA_SPAN, adjust=False).mean()
    recent_peak = vix.rolling(20, min_periods=5).max()
    return (recent_peak >= VIX_ELEVATED) & (vix < ema) & (vix <= recent_peak * (1 - ROLLOVER_RETRACE))


def momentum_decel_series(close: pd.Series, win: int = MOM_WIN,
                          peak_win: int = DD_PEAK_WIN) -> pd.Series:
    """指数上行速率放缓：近高位 且 仍在上行(ROC>0) 且 速率下降(ROC[t]<ROC[t-win])。"""
    roc = close.pct_change(win)
    near_high = close >= close.rolling(peak_win, min_periods=1).max() * NEAR_HIGH_PCT
    return near_high & (roc > 0) & (roc < roc.shift(win))


# ── 择时面板（对齐交易日，逐日状态；live 取末行、回测逐日）──────────
def build_timing_panel(vix: pd.Series,
                       index_prices: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """构建 date × 状态 面板。index_prices: {"QQQ": df, "SPY": df}（需 Close）。"""
    idx_close = {k: v["Close"].dropna() for k, v in index_prices.items()
                 if v is not None and not v.empty and "Close" in v.columns}
    if not idx_close:
        return pd.DataFrame()
    ref = None
    for c in idx_close.values():
        ref = c.index if ref is None else ref.union(c.index)
    ref = ref.sort_values()

    v = vix.copy()
    v.index = pd.to_datetime(v.index)
    v = v.sort_index().reindex(ref.union(v.index)).ffill(limit=5).reindex(ref)

    df = pd.DataFrame(index=ref)
    df["vix"] = v
    df["vix_ema"] = v.ewm(span=VIX_EMA_SPAN, adjust=False).mean()

    dd_cols = []
    for name, c in idx_close.items():
        cc = c.reindex(ref).ffill(limit=3)
        df[f"dd_{name}"] = rolling_drawdown(cc)
        dd_cols.append(f"dd_{name}")
    df["max_dd"] = df[dd_cols].max(axis=1)
    df["dd_index"] = df[dd_cols].idxmax(axis=1).str.replace("dd_", "", regex=False)

    df["vix_rollover"] = vix_rollover_series(df["vix"])
    df["bottom_zone"] = (df["vix"] >= VIX_ELEVATED) & (df["max_dd"] >= DD_MIN)
    df["bottom_setup"] = df["bottom_zone"] & df["vix_rollover"]

    df["vix_higher_lows"] = vix_higher_lows_series(df["vix"])
    decel = pd.Series(False, index=ref)
    for name, c in idx_close.items():
        decel = decel | momentum_decel_series(c.reindex(ref).ffill(limit=3))
    df["mom_decel"] = decel
    df["top_watch"] = df["vix_higher_lows"]
    df["top_warning"] = df["vix_higher_lows"] & df["mom_decel"]
    return df


# ── 结果结构 + live 求值 ──────────────────────────────────────────
@dataclass
class VixTimingResult:
    date:            pd.Timestamp
    vix:             float = float("nan")
    vix_ema:         float = float("nan")
    vix_rollover:    bool = False
    drawdowns:       Dict[str, float] = field(default_factory=dict)
    max_drawdown:    float = 0.0
    dd_index:        str = ""
    bottom_state:    str = "NONE"    # NONE|ZONE|SETUP|CONFIRMED
    vix_tier:        Optional[str] = None
    suggested_tranche: float = 0.0   # 逆势 sleeve 建议上限（0..1，风控层裁决）
    index_b1:        bool = False    # 任一指数缠论 b1 背驰
    vix_higher_lows: bool = False
    momentum_decel:  bool = False
    top_state:       str = "NONE"    # NONE|WATCH|WARNING
    alerts:          List[str] = field(default_factory=list)
    reasoning:       str = ""


def _vix_tier(vix: float) -> Tuple[Optional[str], float]:
    for lo, label, tr in BOTTOM_TIERS:  # 阈值降序
        if vix >= lo:
            return label, tr
    return None, 0.0


def compute_vix_timing(vix: pd.Series,
                       index_prices: Dict[str, pd.DataFrame],
                       index_b1: bool = False) -> VixTimingResult:
    """在最新一根K上求当前择时状态（面板末行，无前视）。

    index_b1: 调用方（live）对 QQQ/SPY 跑 compute_chan_signal 得到「任一指数出 b1 背驰」，
    用于把 SETUP 升级为 CONFIRMED；回测侧另行 as-of 重放，不在此耦合。
    """
    panel = build_timing_panel(vix, index_prices)
    if panel.empty:
        return VixTimingResult(date=pd.Timestamp.now(), reasoning="[VIX择时] 无指数价格")
    row = panel.iloc[-1]
    vix_now = float(row["vix"]) if np.isfinite(row["vix"]) else float("nan")

    dds = {c[len("dd_"):]: float(row[c])
           for c in panel.columns if c.startswith("dd_") and c != "dd_index"}
    tier, tranche = _vix_tier(vix_now) if np.isfinite(vix_now) else (None, 0.0)

    # 抄底状态机
    if bool(row["bottom_setup"]) and index_b1:
        bottom = "CONFIRMED"
    elif bool(row["bottom_setup"]):
        bottom = "SETUP"
    elif bool(row["bottom_zone"]):
        bottom = "ZONE"
    else:
        bottom = "NONE"
    # 逃顶状态机
    if bool(row["top_warning"]):
        top = "WARNING"
    elif bool(row["top_watch"]):
        top = "WATCH"
    else:
        top = "NONE"

    alerts: List[str] = []
    if bottom == "CONFIRMED":
        alerts.append(f"🟢 抄底CONFIRMED：VIX={vix_now:.1f}[{tier}] + {row['dd_index']}回撤"
                      f"{row['max_dd']:.0%} + VIX掉头 + 指数b1背驰 → 逆势 sleeve 上限≤{tranche:.0%}")
    elif bottom == "SETUP":
        alerts.append(f"🟡 抄底SETUP：VIX={vix_now:.1f}[{tier}] + {row['dd_index']}回撤"
                      f"{row['max_dd']:.0%} + VIX掉头 → 分批建逆势仓（待指数b1确认升 CONFIRMED），上限≤{tranche:.0%}")
    elif bottom == "ZONE":
        alerts.append(f"⚪ 抄底ZONE：VIX={vix_now:.1f}[{tier}] + {row['dd_index']}回撤"
                      f"{row['max_dd']:.0%}（恐惧尚未见顶，等 VIX 掉头再动手——勿接飞刀）")
    if top == "WARNING":
        alerts.append("🔴 逃顶WARNING：VIX抬高波谷 + 指数上行减速 → 止盈/收紧止损/降新仓上限（减仓非做空）")
    elif top == "WATCH":
        alerts.append("🟠 逃顶WATCH：VIX抬高波谷（复杂化侵蚀）→ 监控，等动量背离确认")

    reasoning = (f"VIX={vix_now:.1f}(ema{row['vix_ema']:.1f}) rollover={bool(row['vix_rollover'])} | "
                 f"maxDD={row['max_dd']:.0%}@{row['dd_index']} | "
                 f"bottom={bottom}{'('+tier+')' if tier else ''} | "
                 f"top={top}(hl={bool(row['vix_higher_lows'])},decel={bool(row['mom_decel'])})")

    return VixTimingResult(
        date=pd.Timestamp(row.name),
        vix=vix_now, vix_ema=float(row["vix_ema"]),
        vix_rollover=bool(row["vix_rollover"]),
        drawdowns=dds, max_drawdown=float(row["max_dd"]), dd_index=str(row["dd_index"]),
        bottom_state=bottom, vix_tier=tier,
        suggested_tranche=(tranche if bottom in ("SETUP", "CONFIRMED") else 0.0),
        index_b1=index_b1,
        vix_higher_lows=bool(row["vix_higher_lows"]), momentum_decel=bool(row["mom_decel"]),
        top_state=top, alerts=alerts, reasoning=reasoning,
    )
