"""R8 市场级 VIX 大盘拐点择时——**投资决策参考 MD** 生成器。

每次运行输出 output/{date}/vix_market_timing.md：当日**实时状态** + 全量**判断信号/逻辑/预警规则**
（用户明确要求「fully documented ... for my investment decision-making reference」）。规则表为静态
文档、状态为动态求值。回测证据见同目录 r8_vix_timing_backtest.md。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from loguru import logger

from signals.macro.vix_timing import (
    VixTimingResult, VIX_ELEVATED, VIX_STRONG, VIX_PANIC, VIX_EXTREME,
    DD_MIN, DD_PEAK_WIN, ROLLOVER_RETRACE, PIVOT_CONFIRM, MOM_WIN, BOTTOM_TIERS,
)

# 回测关键数字（output/{date}/r8_vix_timing_backtest.md，样本 2021-08→2026-08，前向指数收益）
_EVIDENCE = (
    "| 触发 | QQQ fwd10 | QQQ fwd20 | 结论 |\n"
    "|------|------|------|------|\n"
    "| 基线(无条件) | +0.63% (胜59%) | +1.25% (胜62%) | — |\n"
    "| V0 ZONE(仅VIX≥28+回撤≥10%) | **−0.28% (胜40%)** | +1.29% (胜53%) | 🔴接飞刀，watch-only |\n"
    "| V1 SETUP(+VIX掉头) | **+3.69% (胜83%)** | **+4.47% (胜75%)** | ✅ 抄底触发（wire-in） |\n"
    "| V2 CONFIRMED(+指数b1) | +2.92% (胜100%,n2) | +8.86% (胜100%,n2) | ✅ 方向对，n少待OOS |\n"
    "| TOP_WARNING(逃顶) | +0.10% (胜44%) | **−0.53% (胜51%)** | ✅ 低于基线=减仓有据 |\n"
)


def _tier_table() -> str:
    rows = ["| VIX 分档 | 区间 | 逆势 sleeve 上限 | 语义 |", "|------|------|------|------|"]
    spans = {
        "elevated(28-32)":   f"{VIX_ELEVATED:.0f}–{VIX_STRONG:.0f}",
        "strong(32-40)":     f"{VIX_STRONG:.0f}–{VIX_PANIC:.0f}",
        "panic(40-60)":      f"{VIX_PANIC:.0f}–{VIX_EXTREME:.0f}",
        "generational(>=60)": f"≥{VIX_EXTREME:.0f}",
    }
    sem = {
        "elevated(28-32)":   "情绪紧张，试探性逆势",
        "strong(32-40)":     "强恐慌，主力逆势档",
        "panic(40-60)":      "严重恐慌/危机，激进（罕见、剧烈，分批）",
        "generational(>=60)": "世代级（2008/2020），最大逆势",
    }
    for _lo, label, tr in sorted(BOTTOM_TIERS, key=lambda x: x[0]):
        rows.append(f"| {label} | {spans[label]} | ≤{tr:.0%} | {sem[label]} |")
    return "\n".join(rows)


def build_vix_timing_doc(date_str: str, swing: Optional[VixTimingResult]) -> str:
    L = ["# 🎯 市场级 VIX 大盘拐点择时 — 投资决策参考", ""]
    L.append(f"生成日期：{date_str}　｜　第三轴：**均值回归 / 制度反转**（与缠论顺势延续正交、与 VIX 节流阀互补）")
    L.append("")

    # ── 一、当日实时状态 ──
    L.append("## 一、当日实时状态")
    L.append("")
    if swing is None:
        L.append("> ⚠️ 本次无择时结果（缺 VIX 序列或 QQQ/SPY 价格）。")
    else:
        dd_str = " ".join(f"{k} {v:.0%}" for k, v in swing.drawdowns.items())
        L.append(f"- **VIX**：{swing.vix:.1f}（EMA{swing.vix_ema:.1f}，掉头={swing.vix_rollover}）")
        L.append(f"- **指数回撤**（对{DD_PEAK_WIN}日峰值）：{dd_str}　→ 最大 {swing.max_drawdown:.0%}@{swing.dd_index}")
        L.append(f"- **抄底状态**：`{swing.bottom_state}`"
                 + (f"　档位={swing.vix_tier}，逆势 sleeve 建议≤{swing.suggested_tranche:.0%}"
                    if swing.vix_tier else ""))
        L.append(f"- **逃顶状态**：`{swing.top_state}`"
                 f"（VIX抬高波谷={swing.vix_higher_lows}，指数减速={swing.momentum_decel}）")
        L.append("")
        if swing.alerts:
            L.append("### 🚨 当前预警与建议动作")
            for a in swing.alerts:
                L.append(f"- {a}")
        else:
            L.append("### ✅ 当前无大盘拐点信号（正常顺势环境，按缠论主轴执行）")
    L.append("")

    # ── 二、判断信号与逻辑（静态规则全文）──
    L.append("## 二、判断信号 · 逻辑 · 预警规则（完整）")
    L.append("")
    L.append(f"**核心洞察**：系统原两轴（缠论 55% 顺势延续 + VIX 门 35% 同步节流）都**抓不到大盘摆动高低点**，"
             f"且存在张力——旧门控**顺周期**（VIX>35→仓位0%），本策略**逆周期**（恐慌里抄底）。本轴即那条缺失的"
             f"**逆势/领先**轴，由 governed sleeve 化解张力。")
    L.append("")
    L.append(f"**临界 VIX 阈值**：{VIX_ELEVATED:.0f} / {VIX_STRONG:.0f} / {VIX_PANIC:.0f} / {VIX_EXTREME:.0f}"
             f"（>{VIX_PANIC:.0f} 仅严重恐慌/危机，历史罕见）。")
    L.append("")
    L.append("### A. 抄底（逆势多，分级）")
    L.append(f"| 状态 | 触发条件（全部满足） | 动作 |")
    L.append("|------|------|------|")
    L.append(f"| ⚪ **ZONE** | VIX≥{VIX_ELEVATED:.0f} 且 (QQQ 或 SPY 对{DD_PEAK_WIN}日峰值回撤≥{DD_MIN:.0%}) | "
             f"**观察勿动手**（恐惧未见顶=接飞刀；回测该裸规则 fwd10 −0.28%） |")
    L.append(f"| 🟡 **SETUP** | ZONE + **VIX 掉头**（自近20日尖峰回落≥{ROLLOVER_RETRACE:.0%} 且跌破 EMA{10}） | "
             f"**分批建逆势仓 ≤tier×0.6**（回测主触发，12 episode、fwd10/20/60 全带 +2.8% 稳定边） |")
    L.append(f"| 🟢 **CONFIRMED** | SETUP + **指数缠论 b1 背驰**（创新低+MACD衰竭） | "
             f"**满逆势 tranche（tier×1.0，结构加码）**（两轴锁；解锁越 panic 门的 sleeve） |")
    L.append("")
    L.append("**VIX 分档 → 逆势 sleeve 上限**（组合级，跨名分摊）：")
    L.append("")
    L.append(_tier_table())
    L.append("")
    L.append("### B. 逃顶（减仓预警，**非做空**——保守单边）")
    L.append("| 状态 | 触发条件 | 动作 |")
    L.append("|------|------|------|")
    L.append(f"| 🟠 **WATCH** | VIX **抬高的波谷**（higher-lows，已确认波谷递增=复杂化侵蚀） | 监控，等动量确认 |")
    L.append(f"| 🔴 **WARNING** | WATCH + **指数上行减速**（近高位、仍升但{MOM_WIN}日ROC 减速） | "
             f"**止盈/收紧止损/新仓上限折半**（回测 fwd20 低于基线） |")
    L.append("")

    # ── 三、决策层耦合（怎么影响下单）──
    L.append("## 三、与交易框架的耦合（governed）")
    L.append("- **抄底分级解锁**（2026-08-09 阈值回测定标）→ risk_overlay 开**独立逆势 sleeve**（越 VIX>35 的 0% 门）："
             "**SETUP → tier×0.6**（缩额，主触发有稳定边）、**CONFIRMED → tier×1.0**（结构确认满额加码）；"
             "ZONE 仍不解锁（勿接飞刀），普通顺势买点在 panic 仍节流为 0（sleeve 与顺势仓分离）。")
    L.append("- **逃顶 WARNING** → 新仓上限×0.5 + `TOP_WARNING_TRIM` 标记（减仓非做空）。")
    L.append("- 择时为**门控叠加**，**不进** 0.55/0.35/0.10 线性 final_score（守 macro=制度门控 之职）。")
    L.append("")

    # ── 四、回测证据 ──
    L.append("## 四、样本内回测证据（wiring 依据）")
    L.append("")
    L.append(_EVIDENCE)
    L.append("")
    L.append("**读法**：用户原始「VIX≥28+回撤≥10%」裸规则回测**接飞刀**（fwd10 负、胜40%）；加 **VIX 掉头** 后"
             "跃升为强触发（fwd20 +4.5%/胜75%）——**掉头确认是本策略的胜负手**。逃顶前向收益稳定低于基线=减仓有据。")
    L.append("")
    L.append("**阈值敏感性（2026-08-09 网格回测）**：① VIX掉头的 retrace 0–12% 是 plateau（EMA10 cross 才是真杠杆，"
             "非 retrace 深度）→ 10%/EMA10 落在稳健区，保留。② **SETUP vs CONFIRMED**：SETUP 12 episode、全 horizon "
             "+2.8% 稳定边；CONFIRMED 历史仅 2 次、fwd60 边转负、且**漏掉 2025-04 整段抄底** → 定为「SETUP 缩额解锁、"
             "CONFIRMED 满额加码」而非 CONFIRMED-only 门。③ 逃顶 **中位数各 horizon 仍为正**（fwd20 +0.5%），负边全来自"
             "**左尾**（fwd60 有 15% 概率跌>10%）→ WARNING 是**尾部对冲**非方向做空，×0.5 折半（留一半正中位漂移、砍一半尾部）合适。")
    L.append("")

    # ── 五、诚实边界 ──
    L.append("## 五、诚实边界（必读）")
    L.append("- **样本内 ≠ 样本外**：n 小（恐慌 episode 稀少、彼此重叠），CONFIRMED 仅 n=2。已挂 OOS 无回填累积，"
             "数月/数年后才定谳（承 R1.3 幸存者偏差 / R5 pullback 门照搬证伪教训）。")
    L.append(f"- **右端不稳**：VIX 波谷/波峰用**{PIVOT_CONFIRM}根右端确认**（同缠论定笔）——最后一个波谷未确认前不发信号，"
             "故逃顶信号天然滞后数日，换取不被右端重画反复。")
    L.append("- **减仓非做空**：逃顶只降险，不反向做空（守项目保守单边 = 周线SMA单边过滤 哲学）。")
    L.append("- **门控非造信号**：本轴决定「环境是否允许/是否该逆势」，个股买卖点仍由缠论主轴给出。")
    return "\n".join(L)


def write_vix_timing_report(date_str: str, output_dir: Path,
                            swing: Optional[VixTimingResult]) -> Optional[Path]:
    try:
        md = build_vix_timing_doc(date_str, swing)
    except Exception as exc:
        logger.warning(f"[VIXTimingReport] 生成失败: {exc}")
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "vix_market_timing.md"
    path.write_text(md, encoding="utf-8")
    logger.info(f"  R8 大盘择时决策参考: {path}")
    return path
