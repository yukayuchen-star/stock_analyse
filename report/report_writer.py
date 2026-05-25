"""
P6 报告层

write_all_reports() 入口，为每日运行生成：
  output/{date}/{TICKER}.md   — 个股详情报告
  output/{date}/daily_summary.md — 全池汇总 + 可操作信号
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

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

    # 缠论模块
    if chan:
        lines += [
            "## 缠论分析（权重 40%）",
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
            "## 量化分析（权重 40%）",
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
        "## 宏观背景（权重 20%）",
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


# ── 公共入口 ──────────────────────────────────────────────────

def write_all_reports(
    decisions:  Dict[str, StockDecision],
    macro:      MacroSignalResult,
    date_str:   str,
    output_dir: Path,
) -> List[Path]:
    """
    生成所有报告文件，返回已写入的路径列表。
    output_dir 应为 Path(settings.output_dir) / date_str。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    for ticker, d in decisions.items():
        path = output_dir / f"{ticker}.md"
        path.write_text(_stock_report(d, date_str), encoding="utf-8")
        written.append(path)

    summary_path = output_dir / "daily_summary.md"
    summary_path.write_text(_daily_summary(decisions, macro, date_str), encoding="utf-8")
    written.append(summary_path)

    return written
