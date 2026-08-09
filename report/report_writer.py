"""
P6 报告层

write_all_reports() 入口，为每日运行生成：
  output/{date}/{TICKER}.md   — 个股详情报告
  output/{date}/daily_summary.md — 全池汇总 + 可操作信号
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

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
            lines.append(
                f"- **{d.ticker}**{chan_pt} [{d.rating} {d.final_score:+.3f}]  "
                f"入场 {entry}  止损 {d.stop_loss:.2f}  止盈 {d.take_profit:.2f}"
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
) -> str:
    """精简执行单：①今日判断 ②今日买卖点 ③当前仓位。数据取自组合 state（已按 60% 上限成交的**真实**结果）。"""
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
    stance = "常规：按买卖点执行，总仓≤60%。"
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
        L += ["> 今日无成交（无满足条件的买卖点，或已被 60% 仓位上限/现金挡下——见文末候补）。", ""]
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
              f"- 持仓上限 60% → 剩余可加仓额度约 **${max(0.0, 0.60*equity - mv):,.0f}**", ""]
    if positions:
        L += ["| 代码 | 买入价 | 现价 | 浮盈 | 股数 | 市值 | 止损 |", "|--|--|--|--|--|--|--|"]
        price_now = {c: (float(decisions[c].current_price)
                         if c in decisions and getattr(decisions[c], "current_price", 0) else pos["cost_price"])
                     for c, pos in positions.items()}
        for code, pos in sorted(positions.items(), key=lambda kv: kv[1]["buy_date"]):
            px = price_now.get(code, pos["cost_price"])
            pnl = (px - pos["cost_price"]) / pos["cost_price"] if pos["cost_price"] else 0.0
            sl = f"{pos['stop_loss']:.2f}" if pos.get("stop_loss") else "—"
            L.append(f"| {code} | {pos['cost_price']:.2f} | {px:.2f} | {pnl:+.1%} | "
                     f"{pos['shares']} | ${pos['shares']*px:,.0f} | {sl} |")
        L.append("")
    else:
        L += ["> 当前空仓。", ""]

    # ── 候补：想买但被上限/现金挡下 ──
    held = set(positions.keys())
    bought_today = {t["code"] for t in buys}
    queued = [d for d in sorted(decisions.values(), key=lambda x: x.final_score, reverse=True)
              if d.rating in ("Buy", "Overweight") and d.suggested_position > 0
              and d.ticker not in held and d.ticker not in bought_today]
    if queued:
        L += ["## 候补（评级达标，暂被 60% 上限/现金挡下）", ""]
        for d in queued:
            bp = (d.chan_signal.buy_point_type.upper()
                  if d.chan_signal and d.chan_signal.buy_point_type else "—")
            L.append(f"- {d.ticker} [{bp} {d.final_score:+.2f}] 建议仓位 {d.suggested_position:.0%}"
                     f"（腾出额度/现金后优先补入）")
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
) -> Path:
    """写精简每日执行单 output/{date}/今日操作.md（live 每日照此下单）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "今日操作.md"
    path.write_text(_daily_action_sheet(decisions, macro, state, date_str), encoding="utf-8")
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
