"""七巨头缠论 K 线图 —— 近三个月蜡烛 + MA5/10/20 + b1/b2/b3 / s1/s2/s3。

产出 `output/{date}/charts/mag8_chan.html`（单文件、离线可用、八只切页签）。

⚠️⚠️ **本模块唯一的设计要点：买卖点用 as-of 重放，不用全历史几何。**

`compute_chan_signal` 只返回**最后一根 K** 的信号，要在三个月的图上标出历史买卖点，
只有两条路：

  ① **as-of 重放**（本模块所采用）：对视窗内每个交易日 t，用 `df.loc[:t]` 重算一遍缠论，
     记下那一天**真正可见**的信号；
  ② 拿全历史的笔/中枢几何回头标注"当时应该是个 b3"。

②**是 R1.3 已经踩过的坑**：全历史几何里，那些后来被新 K 重画掉的失败笔**根本不存在了**，
于是图上只剩下事后看起来漂亮的买点 —— 这正是旧回测把缠论胜率算成 79.8%（诚实基线 53.2%
< 随机 55.5%）的同一个幸存者偏差。**一张会骗人的图比没有图更糟**，所以这里宁可多花十几秒。

代价：视窗 ~63 个交易日 × 8 只 ≈ 500 次缠论计算，实测约 **17 秒**。

📌 **实测副产品（2026-09-10，八只 / 近三个月）**：绝大多数信号**只活 1~2 天**
（META 三个月里出了五次 s3，全是短命的；NVDA 七次首现里多次 ×1d）。
所以本图把**信号存活天数编码成标记大小** —— 一眼就能看出某只票的结构是"稳"还是"天天翻脸"，
这比单纯标一个三角形有用得多。它同时是 `insight_chan_right_edge` 那条记忆的可视化。

📌 另一条实测结论：`compute_chan_signal` **只在末笔已定笔时才发买卖点**，
所以重放出来的每一个标记天然都是"定笔✓"。图上不再重复标注这个恒真的字段
（`stroke_confirmed` 的护栏作用发生在上游，不在这里）。

—— 本模块 **advisory / 只读**：不改缠论本体、不产生任何交易指令、不写台账。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from signals.chan.chan_signal import compute_chan_signal

# 八只 = 标准七巨头（含 TSLA）∪ 核心持仓七只（含 QQQ）。
# ⚠️ 三者**不在同一本账上**，图上必须标清楚，否则就是一次静默的 sleeve 混淆：
#    core     = 真金核心 sleeve（70%，长持数年）
#    index    = 基准 / 核心 sleeve 的指数位
#    tactical = paper 战术 sleeve（数周，与核心对 VIX 方向故意相反）
MAG8: List[tuple[str, str]] = [
    ("NVDA",  "core"),
    ("AAPL",  "core"),
    ("GOOGL", "core"),
    ("MSFT",  "core"),
    ("AMZN",  "core"),
    ("META",  "core"),
    ("QQQ",   "index"),
    ("TSLA",  "tactical"),
]

SLEEVE_LABEL = {
    "core":     ("核心 sleeve · 真金", "#b45309"),
    "index":    ("指数 / 基准",        "#4338ca"),
    "tactical": ("战术 sleeve · paper", "#0f766e"),
}

VIEW_MONTHS   = 3      # 视窗：近三个月
MIN_BARS_CHAN = 200    # compute_chan_signal 自身的硬下限

# 买点冷色、卖点暖色 —— **刻意不与红涨绿跌的蜡烛同色系**，否则标记会淹没在 K 线里。
# b3 最亮：R4.2 重标定后只有 b3 期望为正（b1/b2 贴近保本线），亮度对应可信度。
MARKER_STYLE = {
    "b1": ("#1e40af", "triangle-up",   "一买 · 底背驰"),
    "b2": ("#0284c7", "triangle-up",   "二买 · 中枢下沿回踩"),
    "b3": ("#06b6d4", "triangle-up",   "三买 · 中枢上沿回踩（R4.2 唯一期望为正）"),
    "s1": ("#9333ea", "triangle-down", "一卖 · 顶背驰"),
    "s2": ("#c026d3", "triangle-down", "二卖"),
    "s3": ("#e11d48", "triangle-down", "三卖"),
}

UP_COLOR, DOWN_COLOR = "#e11d48", "#059669"   # 红涨绿跌（中国习惯）
MA_STYLE = [(5, "#f59e0b", 1.4), (10, "#8b5cf6", 1.4), (20, "#64748b", 1.6)]


# ────────────────────────── as-of 重放 ──────────────────────────
def _asof_markers(ticker: str, df: pd.DataFrame,
                  view_dates: pd.DatetimeIndex) -> List[dict]:
    """对视窗内每一天用 `df.loc[:t]` 重算缠论，返回信号**首现**记录。

    信号会连续存在多天（结构没变就一直是那个 b3），逐日画会得到一串一样的三角形。
    这里只记**首现日**（= 你当天才会看到它、才可能据此动手的那一天），
    并把它此后连续存在了几天记进 `days`：

      - `days` 大 ⇒ 结构稳，信号扛住了后续 K 线的重画；
      - `days` = 1 ⇒ 它第二天就不见了 —— 图上会画成一个很小的标记。

    `alive` = 该信号是否一直活到视窗最后一根 K。
    """
    out: List[dict] = []
    prev: Optional[str] = None

    for d in view_dates:
        sub = df.loc[:d]
        if len(sub) < MIN_BARS_CHAN:
            continue
        try:
            res = compute_chan_signal(ticker, {ticker: sub})
        except Exception as e:                       # 单日失败不该毁掉整张图
            logger.debug(f"[Chart] {ticker} {d.date()} 缠论重算失败: {e}")
            continue

        sig = res.buy_point_type or res.sell_point_type
        if sig != prev:
            if sig:
                bar = df.loc[d]
                out.append({
                    "date": d, "type": sig,
                    "kind": "buy" if sig.startswith("b") else "sell",
                    "low": float(bar["Low"]), "high": float(bar["High"]),
                    "close": float(bar["Close"]),
                    "days": 1, "alive": False,
                    "confidence": float(res.confidence or 0.0),
                    "divergence": bool(res.divergence),
                    "stop_loss": res.stop_loss,
                })
            prev = sig
        elif sig and out:
            out[-1]["days"] += 1

    if out and prev:                 # 最后一段仍在 ⇒ 该信号活到今天
        out[-1]["alive"] = True
    return out


def _marker_size(days: int) -> float:
    """存活天数 → 标记大小。1 天 ≈ 10px，≥8 天封顶 ≈ 23px。"""
    return 9.0 + min(days, 8) * 1.75


# ────────────────────────── 画图 ──────────────────────────
def _build_figure(ticker: str, sleeve: str, view: pd.DataFrame,
                  markers: List[dict], pivot: Optional[dict]):
    import plotly.graph_objects as go

    fig = go.Figure()

    # 中枢带 —— 没有它 b3（"回踩中枢上沿 ZG"）这个标记根本没法读
    if pivot and pivot.get("ZG") and pivot.get("ZD"):
        fig.add_hrect(
            y0=pivot["ZD"], y1=pivot["ZG"],
            fillcolor="#94a3b8", opacity=0.13, line_width=0, layer="below",
        )
        for key, dash, txt in (("ZG", "dash", "ZG 中枢上沿"), ("ZD", "dot", "ZD 中枢下沿")):
            fig.add_hline(
                y=pivot[key], line=dict(color="#64748b", width=1, dash=dash),
                annotation_text=f"{txt} {pivot[key]:.2f}",
                annotation_position="right",
                annotation_font=dict(size=10, color="#475569"),
            )

    fig.add_trace(go.Candlestick(
        x=view.index, open=view["Open"], high=view["High"],
        low=view["Low"], close=view["Close"], name="K 线",
        increasing=dict(line=dict(color=UP_COLOR, width=1), fillcolor=UP_COLOR),
        decreasing=dict(line=dict(color=DOWN_COLOR, width=1), fillcolor=DOWN_COLOR),
        hoverlabel=dict(namelength=0),
    ))

    for n, color, width in MA_STYLE:
        fig.add_trace(go.Scatter(
            x=view.index, y=view[f"MA{n}"], mode="lines", name=f"MA{n}",
            line=dict(color=color, width=width),
            hovertemplate=f"MA{n} %{{y:.2f}}<extra></extra>",
        ))

    # 买卖点：按类型分组，图例才能逐类开关
    for typ, (color, symbol, desc) in MARKER_STYLE.items():
        pts = [m for m in markers if m["type"] == typ]
        if not pts:
            continue
        fig.add_trace(go.Scatter(
            x=[m["date"] for m in pts],
            y=[m["low"] * 0.978 if m["kind"] == "buy" else m["high"] * 1.022 for m in pts],
            mode="markers", name=f"{typ}（{len(pts)}）",
            marker=dict(
                symbol=symbol, color=color,
                size=[_marker_size(m["days"]) for m in pts],
                # 仍然活着的信号给一圈深色描边；已消失的不描边
                line=dict(width=[2.0 if m["alive"] else 0 for m in pts], color="#0f172a"),
                opacity=0.92,
            ),
            customdata=[[m["type"], desc, m["days"],
                         "仍在" if m["alive"] else "已消失",
                         m["close"], m["confidence"],
                         "是" if m["divergence"] else "否"] for m in pts],
            hovertemplate=(
                "<b>%{customdata[0]}</b> · %{x|%Y-%m-%d}<br>"
                "%{customdata[1]}<br>"
                "收盘 %{customdata[4]:.2f}<br>"
                "存活 <b>%{customdata[2]} 日</b>（%{customdata[3]}）<br>"
                "结构置信度 %{customdata[5]:.2f} · 背驰 %{customdata[6]}"
                "<extra></extra>"
            ),
        ))

    fleeting = sum(1 for m in markers if m["days"] <= 2)
    label, lcolor = SLEEVE_LABEL[sleeve]
    subtitle = (f"{len(markers)} 次信号首现，其中 <b>{fleeting}</b> 次 ≤2 日内消失"
                if markers else "近三个月无缠论买卖点")

    fig.update_layout(
        title=dict(
            text=(f"<b>{ticker}</b>　<span style='font-size:12px;color:{lcolor}'>{label}</span>"
                  f"<br><span style='font-size:12px;color:#64748b'>{subtitle}"
                  f"　·　标记大小 = 信号存活天数　·　描边 = 该信号仍在</span>"),
            x=0.012, xanchor="left", font=dict(size=19, color="#0f172a"),
        ),
        height=640, template="plotly_white",
        margin=dict(l=56, r=140, t=88, b=44),
        xaxis=dict(
            rangeslider=dict(visible=False), showgrid=True,
            gridcolor="#f1f5f9", tickformat="%m-%d",
            # 去掉周末与休市日的空档，否则蜡烛之间全是断裂的白条
            rangebreaks=[dict(values=_missing_days(view.index))],
        ),
        yaxis=dict(title="价格 (USD)", showgrid=True, gridcolor="#f1f5f9",
                   side="right", tickformat=".2f"),
        legend=dict(orientation="h", yanchor="bottom", y=1.005,
                    xanchor="right", x=1, font=dict(size=11)),
        hovermode="x unified", plot_bgcolor="white", paper_bgcolor="white",
        font=dict(family="-apple-system, BlinkMacSystemFont, 'Segoe UI', "
                         "'PingFang SC', 'Microsoft YaHei', sans-serif", size=12),
    )
    return fig


def _missing_days(idx: pd.DatetimeIndex) -> List[str]:
    """视窗内所有非交易日（周末 + 休市），交给 rangebreaks 抹掉。"""
    if len(idx) < 2:
        return []
    full = pd.date_range(idx[0], idx[-1], freq="D")
    return [d.strftime("%Y-%m-%d") for d in full.difference(idx)]


# ────────────────────────── 入口 ──────────────────────────
def write_chan_charts(prices: Dict[str, pd.DataFrame], date_str: str,
                      output_dir: Path, pipeline=None) -> Optional[Path]:
    """为 MAG8 生成缠论 K 线图，返回 html 路径（失败返回 None）。

    `prices` 直接复用 `main.py` 已在内存里的那一份（同一份 800 天缓存）——
    **不重新下载**。重新取一次就等于给项目造出第三份价格真相，
    而这个项目已经因为"两条管线共用同一份缓存"吃过亏（NaN 尾行事故）。
    池轮动导致某只不在 `prices` 里时，才用同一个 `pipeline`（同 key、同窗口）补取。
    """
    try:
        import plotly.graph_objects as go       # noqa: F401
        import plotly.offline as pyo
    except ImportError:
        logger.warning("[Chart] 未安装 plotly，跳过缠论 K 线图")
        return None

    charts_dir = output_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    blocks, tabs, skipped = [], [], []
    for i, (ticker, sleeve) in enumerate(MAG8):
        df = prices.get(ticker)
        if (df is None or df.empty) and pipeline is not None:
            try:                                  # 池轮动兜底：同一个 cache，不新开数据源
                df = pipeline.get_price(ticker)
            except Exception as e:
                logger.warning(f"[Chart] {ticker} 补取价格失败: {e}")
        if df is None or df.empty:
            skipped.append(f"{ticker}(无价格)")
            continue
        if len(df) < MIN_BARS_CHAN:
            skipped.append(f"{ticker}(仅 {len(df)} 根 < {MIN_BARS_CHAN})")
            continue

        df = df.sort_index()
        for n, _, _ in MA_STYLE:                  # MA 在**全历史**上算，再切视窗，
            df[f"MA{n}"] = df["Close"].rolling(n).mean()   # 否则视窗左缘 MA20 全是 NaN

        cutoff = df.index[-1] - pd.DateOffset(months=VIEW_MONTHS)
        view   = df.loc[df.index >= cutoff]
        if view.empty:
            skipped.append(f"{ticker}(视窗为空)")
            continue

        markers = _asof_markers(ticker, df, view.index)
        try:
            pivot = compute_chan_signal(ticker, {ticker: df}).current_pivot
        except Exception:
            pivot = None

        fig = _build_figure(ticker, sleeve, view, markers, pivot)
        div_id = f"chart_{ticker}"
        blocks.append(
            f'<div class="pane" id="pane_{ticker}" style="display:'
            f'{"block" if i == 0 else "none"}">'
            + fig.to_html(full_html=False, include_plotlyjs=False,
                          div_id=div_id, config={"displaylogo": False,
                                                 "scrollZoom": True})
            + "</div>"
        )
        fleeting = sum(1 for m in markers if m["days"] <= 2)
        tabs.append((ticker, sleeve, len(markers), fleeting))
        logger.debug(f"[Chart] {ticker}: {len(markers)} 次首现 / {fleeting} 次 ≤2日")

    if not blocks:
        logger.warning(f"[Chart] 无可绘制标的，跳过（{', '.join(skipped) or '原因未知'}）")
        return None

    out = charts_dir / "mag8_chan.html"
    out.write_text(_render_shell(tabs, blocks, date_str, skipped, pyo),
                   encoding="utf-8")
    if skipped:
        logger.warning(f"[Chart] 跳过 {len(skipped)} 只: {', '.join(skipped)}")
    return out


def _render_shell(tabs, blocks, date_str, skipped, pyo) -> str:
    """把八张图装进一个自包含 html（plotly.js 只内嵌一份 ≈4.9MB，离线可用）。"""
    btns = "".join(
        f'<button class="tab{" on" if i == 0 else ""}" data-t="{t}" '
        f'onclick="sel(\'{t}\')"><span class="tk">{t}</span>'
        f'<span class="dot {s}"></span>'
        f'<span class="ct">{n}<i>/{f}</i></span></button>'
        for i, (t, s, n, f) in enumerate(tabs)
    )
    note = (f'<p class="skip">⚠️ 跳过：{"、".join(skipped)}</p>' if skipped else "")
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>七巨头缠论 K 线图 · {date_str}</title>
<script>{pyo.get_plotlyjs()}</script>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f8fafc;color:#0f172a;
 font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
header{{background:#fff;border-bottom:1px solid #e2e8f0;padding:18px 26px 0}}
h1{{margin:0 0 3px;font-size:19px;letter-spacing:.3px}}
.sub{{margin:0 0 14px;color:#64748b;font-size:12.5px}}
.sub b{{color:#0f172a}}
.tabs{{display:flex;gap:5px;flex-wrap:wrap}}
.tab{{display:flex;align-items:center;gap:7px;background:#f1f5f9;border:1px solid #e2e8f0;
 border-bottom:none;border-radius:8px 8px 0 0;padding:8px 13px;cursor:pointer;
 font:inherit;font-size:13px;color:#475569;transition:.12s}}
.tab:hover{{background:#e2e8f0}}
.tab.on{{background:#fff;color:#0f172a;font-weight:600;box-shadow:0 -2px 0 #0f172a inset}}
.tk{{letter-spacing:.4px}}
.dot{{width:7px;height:7px;border-radius:50%;flex:none}}
.dot.core{{background:#b45309}} .dot.index{{background:#4338ca}} .dot.tactical{{background:#0f766e}}
.ct{{font-size:11px;color:#94a3b8;font-variant-numeric:tabular-nums}}
.ct i{{font-style:normal;color:#cbd5e1}}
main{{padding:20px 26px 40px}}
.pane{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:6px;
 box-shadow:0 1px 3px rgba(15,23,42,.05)}}
.legend{{display:flex;gap:20px;flex-wrap:wrap;margin:16px 0 0;padding:14px 18px;
 background:#fff;border:1px solid #e2e8f0;border-radius:10px;font-size:12.5px;color:#475569}}
.legend b{{color:#0f172a}}
.legend div{{flex:1;min-width:230px}}
.skip{{color:#b45309;font-size:12.5px;margin:12px 0 0}}
code{{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body>
<header>
<h1>七巨头缠论 K 线图</h1>
<p class="sub">as-of <b>{date_str}</b> · 近三个月 · 蜡烛红涨绿跌 ·
 买卖点由 <code>compute_chan_signal</code> <b>逐日 as-of 重放</b>得出，
 <b>不是</b>用全历史几何回头标注（后者会抹掉被重画掉的失败笔 = R1.3 幸存者偏差）。
 页签上的数字是 <b>信号首现次数 / 其中 ≤2 日内消失的次数</b>。</p>
<div class="tabs">{btns}</div>
</header>
<main>{"".join(blocks)}
<div class="legend">
<div>🔵 <b>买点</b> b1 一买(底背驰) · b2 二买(中枢下沿回踩) · b3 三买(中枢上沿回踩)<br>
🟣 <b>卖点</b> s1 一卖(顶背驰) · s2 二卖 · s3 三卖</div>
<div><b>标记大小 = 信号存活天数</b>（小 = 次日即消失，右端结构不稳）<br>
<b>深色描边 = 该信号至今仍在</b>；无描边 = 已被后续 K 线重画掉</div>
<div><b>灰带 = 当前中枢</b> ZD~ZG。b3 的定义就是"回踩中枢上沿"，
 没有这条带子标记无法解读。</div>
<div>⚠️ 本图 <b>advisory / 只读</b>：不构成交易指令。核心 sleeve 的动作由三轴 AND 裁决给出，
 <b>缠论 s1/s2/s3 对底仓一律无效</b>。</div>
</div>{note}
</main>
<script>
function sel(t){{
  document.querySelectorAll('.pane').forEach(function(p){{p.style.display='none'}});
  document.getElementById('pane_'+t).style.display='block';
  document.querySelectorAll('.tab').forEach(function(b){{b.classList.toggle('on',b.dataset.t===t)}});
  window.dispatchEvent(new Event('resize'));
}}
</script></body></html>"""
