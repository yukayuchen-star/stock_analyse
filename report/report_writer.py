"""
P6 报告层

write_all_reports() 入口，为每日运行生成：
  output/{date}/{TICKER}.md   — 个股详情报告
  output/{date}/daily_summary.md — 全池汇总 + 可操作信号
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from config.stocks               import CORE_HOLDINGS, MAX_PORTFOLIO_EXPOSURE
from signals.chan.chan_signal     import ChanSignalResult
from signals.quant.factor_engine import QuantSignalResult
from signals.macro.macro_signal  import MacroSignalResult
from decision.strategy           import StockDecision


# ── 评级 → Markdown 标签 ──────────────────────────────────────

_RATING_EMOJI = {
    "Buy":         "🟢 Buy",
    "Overweight":  "🔵 Overweight",
    "Hold":        "⚪ Hold",
    "Underweight": "🔴 Underweight",
    "Sell":        "🔴 Sell",
}

_VIX_DESC = {
    "calm":    "平静 (<15)",
    "neutral": "中性 (15–25)",
    "tense":   "紧张 (25–35)",
    "panic":   "恐慌 (>35)",
}


# ── 个股报告 ──────────────────────────────────────────────────

def _stock_report(d: StockDecision, date_str: str) -> str:
    chan  = d.chan_signal
    quant = d.quant_signal
    macro = d.macro_signal

    # 评级行
    rating_label = _RATING_EMOJI.get(d.rating, d.rating)
    pos_pct = f"{d.suggested_position:.0%}"
    entry   = f"{d.entry_price_range[0]:.2f} ~ {d.entry_price_range[1]:.2f}"

    # 缠论中枢
    if chan and chan.current_pivot:
        pv = chan.current_pivot
        pivot_str = f"ZD={pv['ZD']:.2f}  ZG={pv['ZG']:.2f}  mid={pv['mid']:.2f}  ({pv['strokes']}笔)"
    else:
        pivot_str = "无"

    # 买卖点
    chan_point = "—"
    if chan:
        if chan.buy_point_type:
            chan_point = chan.buy_point_type.upper()
        elif chan.sell_point_type:
            chan_point = chan.sell_point_type.upper()

    # 风控标签
    flags_md = "\n".join(f"- {f}" for f in d.risk_flags) if d.risk_flags else "无"

    # 宏观
    macro_vix    = f"{macro.vix_level:.1f}" if macro else "N/A"
    macro_regime = _VIX_DESC.get(macro.vix_regime, macro.vix_regime) if macro else "N/A"
    macro_limit  = f"{macro.position_limit:.0%}" if macro else "N/A"
    macro_spread = f"{macro.yield_spread:+.2f}%" if macro else "N/A"
    macro_score  = f"{macro.score:+.3f}" if macro else "N/A"

    lines: List[str] = [
        f"# {d.ticker} — {date_str}",
        "",
        "## 综合评级",
        "",
        f"| 项目 | 值 |",
        f"|------|----|",
        f"| 评级 | {rating_label} |",
        f"| 综合得分 | {d.final_score:+.3f} |",
        f"| 建议仓位 | {pos_pct} |",
        f"| 入场区间 | {entry} |",
        f"| 止损价格 | {d.stop_loss:.2f} |",
        f"| 止盈价格 | {d.take_profit:.2f} |",
        "",
        "## 得分合成",
        "",
        f"{d.score_reasoning}",
        "",
    ]

    # R4.1：权重从 StockDecision 动态取（背离票 70/20/10，不再硬编码 55/35/10）
    if d.divergence_applied:
        lines += [
            f"> ⚠️ **背离加权生效**：缠论强(≥0.45)×量化弱(≤-0.10)，结构优先 → "
            f"缠论 {d.chan_weight:.0%} / 宏观 {d.macro_weight:.0%} / 量化 {d.quant_weight:.0%}",
            "",
        ]

    # 缠论模块
    if chan:
        lines += [
            f"## 缠论分析（权重 {d.chan_weight:.0%}）",
            "",
            f"| 项目 | 值 |",
            f"|------|----|",
            f"| 买卖点 | {chan_point} |",
            f"| 缠论得分 | {chan.score:+.2f} |",
            f"| 笔数 | {chan.stroke_count} |",
            f"| 中枢 | {pivot_str} |",
            f"| 末笔方向 | {chan.last_stroke_direction} |",
            f"| MACD背驰 | {'是' if chan.divergence else '否'} |",
            f"| 周线趋势 | {chan.weekly_trend} |",
            f"| 级别共振 | {chan.level_resonance} |",
            f"| 置信度 | {chan.confidence:.2f} |",
            "",
            f"> {chan.reasoning}",
            "",
        ]

    # 量化模块
    if quant:
        lines += [
            f"## 量化分析（权重 {d.quant_weight:.0%}）",
            "",
            f"| 因子 | 权重 | 得分 |",
            f"|------|------|------|",
            f"| 基本面 | 15% | {quant.fundamental_score:+.2f} |",
            f"| 趋势 | 25% | {quant.trend_score:+.2f} |",
            f"| 动量 | 30% | {quant.momentum_score:+.2f} |",
            f"| 相对强度 | 20% | {quant.relative_strength_score:+.2f} |",
            f"| 量价 | 10% | {quant.volume_score:+.2f} |",
            f"| **综合** | 100% | **{quant.score:+.2f}** |",
            "",
            f"> {quant.reasoning}",
            "",
        ]

    # 宏观模块
    ext = macro.external if macro else None
    lines += [
        f"## 宏观背景（权重 {d.macro_weight:.0%}）",
        "",
        f"| 项目 | 值 |",
        f"|------|----|",
        f"| VIX | {macro_vix} [{macro_regime}] |",
        f"| 仓位上限 | {macro_limit} |",
        f"| 10Y利差 | {macro_spread} |",
    ]
    if ext:
        lines += [
            f"| WTI 原油 | ${ext.oil_price:.1f} 20d{ext.oil_ret_20d:+.0%} (信号{ext.oil_signal:+.2f}) |",
            f"| 加息预期 2Y-FF | {ext.rate_hike_gap:+.2f}pp (信号{ext.rate_hike_signal:+.2f}) |",
            f"| 美元 DXY | {ext.dxy_level:.1f} 20d{ext.dxy_ret_20d:+.1%} (信号{ext.dollar_signal:+.2f}) |",
            f"| 通胀预期 BE10Y | {ext.breakeven_10y:.2f}% (信号{ext.inflation_signal:+.2f}) |",
        ]
    lines += [
        f"| 宏观得分 | {macro_score} |",
        "",
    ]
    if ext and ext.anomalies:
        lines += ["**⚠️ 异动预警**", ""]
        for alert in ext.anomalies:
            lines.append(f"> {alert}")
        lines.append("")
    lines += [
        "## 风险标签",
        "",
        flags_md,
        "",
        "---",
        f"*生成时间: {date_str}*",
    ]

    return "\n".join(lines)


# ── 每日汇总报告 ──────────────────────────────────────────────

def _daily_summary(
    decisions: Dict[str, StockDecision],
    macro: MacroSignalResult,
    date_str: str,
) -> str:
    ranked = sorted(decisions.values(), key=lambda d: d.final_score, reverse=True)

    macro_vix    = f"{macro.vix_level:.1f}"
    macro_regime = _VIX_DESC.get(macro.vix_regime, macro.vix_regime)
    macro_limit  = f"{macro.position_limit:.0%}"
    macro_spread = f"{macro.yield_spread:+.2f}%"
    macro_score  = f"{macro.score:+.3f}"

    ext = macro.external

    lines: List[str] = [
        f"# 每日量化分析报告 — {date_str}",
        "",
    ]

    # 数据降级区块（R3.2）：仅降级日出现，正常日无此区块
    degraded = getattr(macro, "degraded", None) or []
    if degraded:
        lines += [
            f"> ⚠️ **宏观数据降级（{len(degraded)} 项）**：以下数据缺失/过期，"
            f"相关因子已剔除或按保守默认处理，评级可信度下降：  ",
        ]
        for d in degraded:
            lines.append(f"> - `{d}`")
        lines.append("")

    lines += [
        "## 宏观环境",
        "",
        f"| 指标 | 值 | 信号 |",
        f"|------|----|----|",
        f"| VIX | {macro_vix} [{macro_regime}] | — |",
        f"| 仓位上限 | {macro_limit} | — |",
        f"| 10Y-2Y利差 | {macro_spread} | {macro.yield_score:+.2f} |",
        f"| WTI 原油 | ${ext.oil_price:.1f} (20d {ext.oil_ret_20d:+.0%}) | {ext.oil_signal:+.2f} |",
        f"| 加息预期 (2Y-FF) | {ext.rate_hike_gap:+.2f}pp | {ext.rate_hike_signal:+.2f} |",
        f"| 美元指数 DXY | {ext.dxy_level:.1f} (20d {ext.dxy_ret_20d:+.1%}) | {ext.dollar_signal:+.2f} |",
        f"| 通胀预期 BE10Y | {ext.breakeven_10y:.2f}% | {ext.inflation_signal:+.2f} |",
        f"| 外部因子综合 | — | {ext.composite_score:+.2f} |",
        f"| **宏观得分** | — | **{macro_score}** |",
        "",
    ]

    # 异动预警
    if ext.anomalies:
        lines += [
            "### ⚠️ 宏观异动预警",
            "",
        ]
        for alert in ext.anomalies:
            lines.append(f"> {alert}")
        lines.append("")

    # R8 市场级 VIX 大盘拐点择时（第三轴：均值回归/制度反转）
    sw = getattr(macro, "swing_timing", None)
    if sw is not None and (sw.bottom_state != "NONE" or sw.top_state != "NONE"):
        lines += [
            "### 🎯 大盘择时（VIX 拐点 · 详见 vix_market_timing.md）",
            "",
            f"- 抄底状态：`{sw.bottom_state}`"
            + (f"（{sw.vix_tier}，逆势 sleeve≤{sw.suggested_tranche:.0%}）" if sw.vix_tier else "")
            + f"　逃顶状态：`{sw.top_state}`",
            f"- VIX={sw.vix:.1f}（掉头={sw.vix_rollover}）　最大回撤 {sw.max_drawdown:.0%}@{sw.dd_index}",
        ]
        for a in sw.alerts:
            lines.append(f"> {a}")
        lines.append("")

    # 桶强度
    if macro.bucket_ir:
        lines += [
            "### 桶强度（IR）",
            "",
            f"| 板块 | IR | 桶得分 |",
            f"|------|----|--------|",
        ]
        for bucket, ir_val in macro.bucket_ir.items():
            bscore = macro.bucket_scores.get(bucket, 0.0)
            lines.append(f"| {bucket} | {ir_val:+.3f} | {bscore:+.2f} |")
        lines.append("")

    # 综合评级排行
    lines += [
        "## 综合评级排行",
        "",
        f"| 股票 | 评级 | 综合得分 | 仓位 | 入场区间 | 止损 | 止盈 |",
        f"|------|------|---------|------|---------|------|------|",
    ]
    for d in ranked:
        entry = f"{d.entry_price_range[0]:.1f}~{d.entry_price_range[1]:.1f}"
        lines.append(
            f"| {d.ticker} | {d.rating} | {d.final_score:+.3f} | "
            f"{d.suggested_position:.0%} | {entry} | "
            f"{d.stop_loss:.1f} | {d.take_profit:.1f} |"
        )
    lines.append("")

    # 可操作信号
    # ⚠️「买入 / 增持」是**指令式**标题，而 ranked 覆盖全池：核心名（归 Core sleeve
    # 手动执行）与 HELD_NO_ADD（强制留池的持仓票，只分析不加仓）评级也可能是 Buy，
    # 但战术账本一律不会买。把原因写在**行内**——只把标记留在下方风险提示里，
    # 等于让读者照着一条账本永远不会执行的建议下单（"评级说买、账本不买、报告不提"）。
    actionable_buy  = [d for d in ranked if d.rating in ("Buy", "Overweight") and d.suggested_position > 0]
    actionable_sell = [d for d in ranked if d.rating in ("Sell", "Underweight")]

    lines += ["## 可操作信号", ""]

    if actionable_buy:
        lines += ["### 买入 / 增持", ""]
        for d in actionable_buy:
            entry = f"{d.entry_price_range[0]:.2f}~{d.entry_price_range[1]:.2f}"
            chan_pt = ""
            if d.chan_signal and (d.chan_signal.buy_point_type or d.chan_signal.sell_point_type):
                pt = d.chan_signal.buy_point_type or d.chan_signal.sell_point_type
                chan_pt = f" [{pt.upper()}]"
            note = ""
            if d.ticker in CORE_HOLDINGS:
                note = "　⛔ 核心持仓，归 Core sleeve 手动执行，战术账本不下单"
            elif any(str(f).startswith("HELD_NO_ADD") for f in (d.risk_flags or [])):
                note = "　⛔ 已轮出扫描池、仅为风控强制留池 → **只分析不加仓**（卖点/止损照常）"
            lines.append(
                f"- **{d.ticker}**{chan_pt} [{d.rating} {d.final_score:+.3f}]  "
                f"入场 {entry}  止损 {d.stop_loss:.2f}  止盈 {d.take_profit:.2f}{note}"
            )
        lines.append("")
    else:
        lines += ["### 买入 / 增持", "", "（无）", ""]

    if actionable_sell:
        lines += ["### 减持 / 卖出", ""]
        for d in actionable_sell:
            lines.append(f"- **{d.ticker}** [{d.rating} {d.final_score:+.3f}]")
        lines.append("")
    else:
        lines += ["### 减持 / 卖出", "", "（无）", ""]

    # 量化因子排行
    quant_ranked = sorted(
        [d for d in decisions.values() if d.quant_signal],
        key=lambda d: d.quant_signal.score,
        reverse=True,
    )
    if quant_ranked:
        lines += [
            "## 量化因子排行",
            "",
            f"| 股票 | 量化得分 | 基本面 | 趋势 | 动量 | 相对强度 | 量价 |",
            f"|------|---------|--------|------|------|---------|------|",
        ]
        for d in quant_ranked:
            q = d.quant_signal
            lines.append(
                f"| {d.ticker} | {q.score:+.2f} | {q.fundamental_score:+.2f} | "
                f"{q.trend_score:+.2f} | {q.momentum_score:+.2f} | "
                f"{q.relative_strength_score:+.2f} | {q.volume_score:+.2f} |"
            )
        lines.append("")

    # 缠论信号汇总
    chan_signals = [(d.ticker, d.chan_signal) for d in ranked if d.chan_signal]
    if chan_signals:
        lines += [
            "## 缠论信号汇总",
            "",
            f"| 股票 | 买卖点 | 缠论得分 | 笔数 | 周线 | 背驰 | 共振 |",
            f"|------|--------|---------|------|------|------|------|",
        ]
        for ticker, c in chan_signals:
            pt = c.buy_point_type or c.sell_point_type or "—"
            lines.append(
                f"| {ticker} | {pt} | {c.score:+.2f} | {c.stroke_count} | "
                f"{c.weekly_trend} | {'是' if c.divergence else '否'} | {c.level_resonance} |"
            )
        lines.append("")

    # 风险标签汇总
    all_flags = [(d.ticker, f) for d in ranked for f in d.risk_flags]
    if all_flags:
        lines += ["## 风险提示", ""]
        for ticker, flag in all_flags:
            lines.append(f"- **{ticker}**: {flag}")
        lines.append("")

    lines += [
        "---",
        f"*生成时间: {date_str}  |  股票池: {', '.join(decisions.keys())}*",
    ]

    return "\n".join(lines)


# ── 精简每日执行单（live 每日照此下单）───────────────────────────

def _daily_action_sheet(
    decisions: Dict[str, StockDecision],
    macro:     MacroSignalResult,
    state:     dict,
    date_str:  str,
    no_buy:    Optional[List[str]] = None,
) -> str:
    """精简执行单：①今日判断 ②今日买卖点 ③当前仓位。数据取自组合 state（已按 MAX_PORTFOLIO_EXPOSURE 上限成交的**真实**结果）。"""
    hist = state.get("history", [])
    cur = hist[-1] if hist else None
    positions = state.get("positions", {})
    trades_today = [t for t in state.get("trades", []) if cur and t["date"] == cur["date"]]
    initial = state.get("initial_capital", 0.0)

    L: List[str] = [f"# 今日操作单 — {date_str}", ""]

    # ── 一、今日判断（简）──
    regime = _VIX_DESC.get(macro.vix_regime, macro.vix_regime)
    L += ["## 一、今日判断", "",
          f"- 大盘环境：VIX **{macro.vix_level:.1f}**[{regime}]　仓位上限(VIX门) {macro.position_limit:.0%}　"
          f"宏观得分 {macro.score:+.2f}"]
    sw = getattr(macro, "swing_timing", None)
    stance = f"常规：按买卖点执行，总仓≤{MAX_PORTFOLIO_EXPOSURE:.0%}（战术 sleeve）。"
    if sw is not None:
        if sw.top_state == "WARNING":
            stance = "🔴 **逃顶预警：只减不加**，对存量止盈/收紧止损，新仓上限已自动折半。"
        elif sw.top_state == "WATCH":
            stance = "🟠 逃顶监控：VIX 抬高波谷，暂不加仓，等动量确认。"
        elif sw.bottom_state in ("SETUP", "CONFIRMED"):
            stance = (f"🟢 **抄底窗口[{sw.bottom_state}]**：VIX 已掉头，逆势 sleeve 可越 panic 门"
                      f"（上限≤{sw.suggested_tranche:.0%}）。")
        elif sw.bottom_state == "ZONE":
            stance = "⚪ 抄底区域但 **VIX 未掉头 → 勿接飞刀**，等掉头确认再动手。"
        if sw.bottom_state != "NONE" or sw.top_state != "NONE":
            L.append(f"- 大盘择时：抄底`{sw.bottom_state}` / 逃顶`{sw.top_state}`（VIX 掉头={sw.vix_rollover}，"
                     f"最大回撤 {sw.max_drawdown:.0%}@{sw.dd_index}）")
    L += [f"- **今日策略取向**：{stance}", ""]

    # ── 二、今日买卖点（来自真实成交）──
    L += ["## 二、今日买卖点", ""]
    sells = [t for t in trades_today if t["action"] == "卖出"]
    buys  = [t for t in trades_today if t["action"] == "买入"]
    if not sells and not buys:
        L += ["> 今日无成交（无满足条件的买卖点，或已被仓位上限/现金挡下——见文末候补）。", ""]
    if sells:
        L += ["### 🔺 卖出", "",
              "| 代码 | 卖价 | 股数 | 盈亏 | 原因 |", "|--|--|--|--|--|"]
        for t in sells:
            L.append(f"| **{t['code']}** | {t['price']:.2f} | {t['shares']} | ${t['pnl']:+,.0f} | {t['reason']} |")
        L.append("")
    if buys:
        L += ["### 🟢 买入", "",
              "| 代码 | 买点 | 买价 | 结构止损 | 第一止盈 | 股数 | 金额 | 占权益 |",
              "|--|--|--|--|--|--|--|--|"]
        equity = cur["equity"] if cur else initial
        for t in buys:
            d = decisions.get(t["code"])
            bp = "—"
            stop = tp = None
            if d is not None:
                if d.chan_signal and d.chan_signal.buy_point_type:
                    bp = d.chan_signal.buy_point_type.upper()
                stop, tp = d.stop_loss, d.take_profit
            cost = t["price"] * t["shares"]
            stop_s = f"{stop:.2f}" if stop else "—"
            tp_s = f"{tp:.2f}" if tp else "—"
            L.append(f"| **{t['code']}** | {bp} | {t['price']:.2f} | {stop_s} | {tp_s} | "
                     f"{t['shares']} | ${cost:,.0f} | {cost/equity:.0%} |")
        L.append("")

    # ── 三、当前仓位 ──
    L += ["## 三、当前仓位（次日初始持仓）", ""]
    if cur:
        mv, cash, equity = cur["market_value"], cur["cash"], cur["equity"]
        L += [f"- 总权益 **${equity:,.0f}**　持仓 **${mv:,.0f}（{mv/equity:.0%}）**　"
              f"现金 ${cash:,.0f}（{cash/equity:.0%}）　累计盈亏 {cur['total_pnl_pct']:+.2%}",
              f"- 持仓上限 {MAX_PORTFOLIO_EXPOSURE:.0%}（战术 sleeve）→ 剩余可加仓额度约 **${max(0.0, MAX_PORTFOLIO_EXPOSURE*equity - mv):,.0f}**", ""]
    if positions:
        L += ["| 代码 | 买入价 | 现价 | 浮盈 | 股数 | 市值 | 止损 |", "|--|--|--|--|--|--|--|"]
        # 取信号日收盘价；取不到就**如实留空**，不再拿成本价冒充现价。
        # 旧写法 `if ... getattr(d, "current_price", 0) else pos["cost_price"]` 在
        # StockDecision 尚无该字段时恒走 else 分支 → 每一行都显示「现价=买入价、浮盈 +0.0%」，
        # 一个看着正常、实则从不更新的假数（2026-08-29 修，同时补齐了 current_price 字段）。
        price_now = {c: float(getattr(decisions[c], "current_price", 0) or 0)
                     for c in positions if c in decisions}
        missing = [c for c in positions if not price_now.get(c)]
        for code, pos in sorted(positions.items(), key=lambda kv: kv[1]["buy_date"]):
            px   = price_now.get(code) or 0.0
            cost = pos["cost_price"]
            sl   = f"{pos['stop_loss']:.2f}" if pos.get("stop_loss") else "—"
            if px:
                pnl = (px - cost) / cost if cost else 0.0
                px_s, pnl_s, mv_s = f"{px:.2f}", f"{pnl:+.1%}", f"${pos['shares']*px:,.0f}"
            else:
                # 无当日价：市值按成本计（与 portfolio_core._snapshot 的兜底同口径），并标注
                px_s, pnl_s, mv_s = "—", "—", f"${pos['shares']*cost:,.0f}*"
            L.append(f"| {code} | {cost:.2f} | {px_s} | {pnl_s} | "
                     f"{pos['shares']} | {mv_s} | {sl} |")
        if missing:
            L.append("")
            L.append(f"> \\* {'、'.join(missing)} 无当日价（不在本次扫描池或取价失败），"
                     f"市值按**成本价**计，浮盈无法计算——与上方总权益口径一致。")
        L.append("")
    else:
        L += ["> 当前空仓。", ""]

    # ── 候补：想买但被上限/现金挡下 ──
    # ⚠️ `no_buy`（强制留池的持仓票）**必须排除**：候补区那句"腾出额度/现金后优先补入"
    # 是个承诺，而账本对它永远不会执行。平时它在 `held` 里被过滤掉，但**它当日被止损
    # 卖出后就不在 `held` 里了**——而那恰恰是强制留池要促成的事，于是刚砍掉的票立刻
    # 出现在候补里劝你补回去。评级说买、账本不买、报告不说 = 又一次静默背离。
    _no_buy = set(no_buy or ())
    held = set(positions.keys())
    bought_today = {t["code"] for t in buys}
    queued = [d for d in sorted(decisions.values(), key=lambda x: x.final_score, reverse=True)
              if d.rating in ("Buy", "Overweight") and d.suggested_position > 0
              and d.ticker not in held and d.ticker not in bought_today
              # 核心名归 Core sleeve（手动执行），不得出现在战术候补里诱导重复建仓
              and d.ticker not in CORE_HOLDINGS
              and d.ticker not in _no_buy]
    if queued:
        L += ["## 候补（评级达标，暂被仓位上限/现金挡下）", ""]
        for d in queued:
            bp = (d.chan_signal.buy_point_type.upper()
                  if d.chan_signal and d.chan_signal.buy_point_type else "—")
            L.append(f"- {d.ticker} [{bp} {d.final_score:+.2f}] 建议仓位 {d.suggested_position:.0%}"
                     f"（腾出额度/现金后优先补入）")
        L.append("")

    # 排除掉的那些单独说明：不写等于把"今天不买"伪装成"今天没信号"
    _blocked = [d for d in sorted(decisions.values(), key=lambda x: x.final_score, reverse=True)
                if d.ticker in _no_buy and d.rating in ("Buy", "Overweight")]
    if _blocked:
        L += ["## 评级达标但不加仓（风控强制留池）", ""]
        for d in _blocked:
            L.append(f"- {d.ticker} [{d.rating} {d.final_score:+.2f}] "
                     f"已轮出扫描池，仅为让止损/卖点每日被检验而留池 → **不加仓、不补回**")
        L.append("")

    L += ["---", "> 执行提示：成交价=信号日收盘；实盘次日开盘下单会有滑点。止损为**结构位**，跌破即离场。"]
    return "\n".join(L)


# ── 公共入口 ──────────────────────────────────────────────────

def write_daily_action_sheet(
    decisions:  Dict[str, StockDecision],
    macro:      MacroSignalResult,
    state:      dict,
    date_str:   str,
    output_dir: Path,
    no_buy:     Optional[List[str]] = None,
) -> Path:
    """写精简每日执行单 output/{date}/今日操作.md（live 每日照此下单）。

    `no_buy`：强制留池的持仓票（只分析不加仓）——与 `write_tactical_snapshot` 同一份
    名单。**这份文件是照着下单的**，所以它比任何报告都更不能出现账本不会执行的建议。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "今日操作.md"
    path.write_text(_daily_action_sheet(decisions, macro, state, date_str, no_buy=no_buy),
                    encoding="utf-8")
    return path


def write_all_reports(
    decisions:  Dict[str, StockDecision],
    macro:      MacroSignalResult,
    date_str:   str,
    output_dir: Path,
    detail_tickers: Optional[set] = None,
) -> List[Path]:
    """
    生成汇总 + 个股报告，返回已写入的路径列表。
    output_dir 应为 Path(settings.output_dir) / date_str。
    detail_tickers: 若给定，仅为这些代码写个股 {TICKER}.md（精简输出，只留可操作/持仓票）；
    None 则全量（保留原行为）。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    for ticker, d in decisions.items():
        if detail_tickers is not None and ticker not in detail_tickers:
            continue
        path = output_dir / f"{ticker}.md"
        path.write_text(_stock_report(d, date_str), encoding="utf-8")
        written.append(path)

    summary_path = output_dir / "daily_summary.md"
    summary_path.write_text(_daily_summary(decisions, macro, date_str), encoding="utf-8")
    written.append(summary_path)

    return written


# ── 战术 sleeve 结构化快照（供核心 sleeve 交叉引用）─────────────

def _bar_asof(prices: Dict) -> tuple:
    """全池最后一根**有效**收盘的日期（众数）+ 落后 / 领先于它的票。

    main.py 的 `date_str` 是墙钟日（决定输出目录），core_research.py 用的是最后一根
    K 线日——两者可以差一天。快照两个都写，交叉检查才有得比。

    ⚠️ `dropna` 不是多余的：Yahoo 会返回**只有 Volume、OHLC 全 NaN** 的未完成尾行，
    它不会报错、不会缺列，只是把缠论的末笔悄悄改掉（memory: insight_yf_nan_tail_bar
    —— MSFT 的 b3 与 META 的 s3 曾因此双双消失且无任何告警）。这里顺带报出
    **哪些票的有效收盘落后于全池最新**，让这种事下次能被看见而不是被平均掉。
    """
    last: Dict[str, str] = {}
    for t, df in (prices or {}).items():
        if df is None or getattr(df, "empty", True):
            continue
        s = df["Close"].dropna()
        if s.empty:
            continue
        last[t] = str(s.index[-1])[:10]
    if not last:
        return None, {}, {}
    # 用**众数**而非 max：Yahoo 的填充是逐票到的，2026-08-29 实测 46 只里只有 2 只
    # 拿到了 08-28 的 OHLC、其余 44 只还是 NaN 占位。取 max 会把整池的 as-of 定成
    # 两只票的日期、然后报告 44 只「落后」——正好把主体说成了例外。
    counts: Dict[str, int] = {}
    for d in last.values():
        counts[d] = counts.get(d, 0) + 1
    asof = max(counts, key=lambda d: (counts[d], d))
    return (asof,
            {t: d for t, d in sorted(last.items()) if d < asof},
            {t: d for t, d in sorted(last.items()) if d > asof})


def _chan_buy_allowed(macro: MacroSignalResult) -> list:
    """战术侧 VIX 四档对缠论买点类型的门控（与 core_research._vix_view 同一函数）。

    ⚠️ 只对战术 sleeve 生效。核心 policy 的扳机明写接受 b1/b2/b3，且核心遇恐慌要
    加速而非收紧——拿这个门去卡核心方向正好反了（CLAUDE.md R9.7）。
    """
    try:
        from signals.macro.regime import chan_buy_threshold, classify_vix
        return sorted(chan_buy_threshold(classify_vix(float(macro.vix_level))))
    except Exception:
        return []


def write_tactical_snapshot(
    decisions:  Dict[str, StockDecision],
    macro:      MacroSignalResult,
    state:      dict,
    prices:     Dict,
    date_str:   str,
    output_dir: Path,
    no_buy:     Optional[List[str]] = None,
) -> Path:
    """写战术 sleeve 结构化快照 output/{date}/tactical_snapshot.json。

    纯落盘，不参与任何判定——`今日操作.md` 是给人看的散文，字段被排版吃掉了，
    核心侧无法程序化引用。此文件把同一次运行的裁决原样存成结构化数据，
    供 `core_research.py` 交叉检查（as-of 是否同日、缠论是否两侧一致、
    两个宏观口径差多少、两本账是否重叠），**不改任何引擎逻辑**。

    ⚠️ 两本账彼此独立：paper `initial_capital` 与真金 `core_ledger.total_capital`
    各自 $100k、互不相干，合起来不等于一本 70/30。`book` 字段写死这一点。

    `no_buy`：仅为风控被强制入池的持仓票（已轮出扫描池）——它们**只分析不加仓**，
    故 `tactical_buyable=false` 且 `no_buy_reason` 写明原因。评级照出，但那是
    「这只票现在什么状态」，不是「可以买」——两者在报告里必须能分开读。

    ⚠️ `tactical_tradable`（在不在战术账本里）与 `tactical_buyable`（能不能买）
    **是两个标，不可合并**。合并过一次：持仓票转 Sell 时整行被 `core_research`
    的 `actionable` 过滤器丢弃，统一操作指引里那条离场根本不出现——
    风控修复漏在呈现层，与它本要修的决策层盲区同构。
    """
    import json

    _no_buy = set(no_buy or ())

    output_dir.mkdir(parents=True, exist_ok=True)
    hist = state.get("history", [])
    cur  = hist[-1] if hist else {}

    rows = {}
    for t, d in decisions.items():
        c = d.chan_signal
        q = d.quant_signal
        px = prices.get(t) if prices else None
        close = None
        if px is not None and not px.empty:
            s = px["Close"].dropna()
            close = round(float(s.iloc[-1]), 2) if not s.empty else None
        rows[t] = {
            "rating": d.rating,
            "final_score": round(float(d.final_score), 4),
            "price": close,
            "suggested_position": round(float(d.suggested_position), 4),
            "entry_price_range": [round(float(x), 2) for x in d.entry_price_range],
            "stop_loss": round(float(d.stop_loss), 2) if d.stop_loss else None,
            "take_profit": round(float(d.take_profit), 2) if d.take_profit else None,
            "weights": {"chan": d.chan_weight, "macro": d.macro_weight,
                        "quant": d.quant_weight},
            "divergence_applied": bool(d.divergence_applied),
            "chan_sell_confirmed": bool(d.chan_sell_confirmed),
            "risk_flags": list(d.risk_flags),
            "score_reasoning": d.score_reasoning,
            "chan": ({
                "score": round(float(c.score), 4),
                "buy_point": c.buy_point_type,
                "sell_point": c.sell_point_type,
                "divergence": bool(c.divergence),
                "stroke_confirmed": bool(c.stroke_confirmed),
                "trend_type": c.trend_type,
                "weekly_trend": c.weekly_trend,
                "level_resonance": int(c.level_resonance),
                "confidence": round(float(c.confidence), 3),
                "atr_pct": round(float(c.atr_pct), 4),
                "pivot": ({k: round(float(v), 2)
                           for k, v in (c.current_pivot or {}).items()
                           if k in ("ZD", "ZG", "mid") and v is not None} or None),
            } if c is not None else None),
            "quant_score": round(float(q.score), 4) if q is not None else None,
            "is_core": t in CORE_HOLDINGS,
            # 核心名：paper 账本里压根没有它（main.py 排除，防同名双重敞口），
            # 买卖两侧都不是战术指令，评级只是**分析结论** → 两标皆 false。
            "tactical_tradable": t not in CORE_HOLDINGS,
            # 强制入池的持仓票：账本里**有真仓位** → tradable=true（卖点/结构止损
            # 照常执行，这正是它入池的唯一目的），buyable=false（不加仓）。
            "tactical_buyable": t not in CORE_HOLDINGS and t not in _no_buy,
            "no_buy_reason": ("core-holding" if t in CORE_HOLDINGS
                              else "held-forced-into-pool" if t in _no_buy
                              else None),
        }

    bar_asof, lagging, ahead = _bar_asof(prices)
    payload = {
        "run_date": date_str,                    # 墙钟日 = 输出目录名
        "bar_asof": bar_asof,                    # 最后一根有效 K 线日（全池最新）
        # 有效收盘落后于全池最新的票：多半是 Yahoo 的 NaN 尾行占位（有 Volume 无 OHLC）。
        # 它会静默改写缠论末笔，故必须逐名可见，不可被"全池 as-of"一个数掩盖。
        "bar_lagging": lagging,
        # 反向：个别票已拿到更新的 OHLC 而全池还没（Yahoo 逐票填充）。
        # 它们的价位比 `bar_asof` 新一天，不可与其余名并列比较。
        "bar_ahead": ahead,
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "sleeve": "tactical",
        "book": {
            "kind": "paper",                     # 模拟盘，与真金核心台账互不相干
            "initial_capital": state.get("initial_capital"),
            "equity": cur.get("equity"),
            "cash": cur.get("cash"),
            "market_value": cur.get("market_value"),
            "n_positions": cur.get("n_positions"),
            "total_pnl_pct": cur.get("total_pnl_pct"),
            "max_exposure_frac": MAX_PORTFOLIO_EXPOSURE,
            "positions": state.get("positions", {}),
        },
        "macro": {
            # ⚠️ 这是战术侧的 35% macro_score（需全池价格 + 桶强度才算得出），
            # **不是**核心侧的 VIX 档位口径。两者不可互换，见 CLAUDE.md R9.7。
            "score": round(float(macro.score), 4),
            "vix": round(float(macro.vix_level), 2),
            "regime": macro.vix_regime,
            "position_limit": macro.position_limit,
            "chan_buy_allowed": _chan_buy_allowed(macro),
            "yield_spread": round(float(macro.yield_spread), 3),
            "bucket_scores": {k: round(float(v), 3)
                              for k, v in (macro.bucket_scores or {}).items()},
            "swing_timing": ({"bottom_state": macro.swing_timing.bottom_state,
                              "top_state": macro.swing_timing.top_state,
                              "vix_tier": macro.swing_timing.vix_tier,
                              "suggested_tranche": macro.swing_timing.suggested_tranche}
                             if macro.swing_timing is not None else None),
            "degraded": list(macro.degraded or []),
            "reasoning": macro.reasoning,
        },
        "decisions": rows,
    }
    path = output_dir / "tactical_snapshot.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    return path
