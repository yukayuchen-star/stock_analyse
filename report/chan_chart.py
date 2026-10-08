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

代价：视窗 ~128 个交易日 × 8 只 ≈ 1000 次缠论计算（2026-09-11 视窗改为**近六个月**）。

📌 **中枢也走同一趟重放**（2026-09-11 加）：`current_pivot` 顺手收下来，
按 ±1.2% 聚类成中枢带。**必须用发信号那一个中枢**，不能另用 `build_all_pivots`
重算 —— 实测 MSFT 后者给 388.3~411.4、而发信号用的
`find_latest_pivot(strokes, lookback=12)` 给 478.5~512.8，两者差一个量级，
混用会让「b3 = 回踩中枢上沿 ZG」这句话在图上对不上。

⚠️ **顺带查出的引擎行为**：`find_latest_pivot` 的中枢在相邻两日之间**会整个跳到
另一个候选**，而不是缓慢延伸 —— AAPL 128 根 K 里换了 39 段，在
200.53~214.74 与 256.20~279.27 之间反复横跳（lookback=12 的搜索窗随笔数平移，
找到的是另一个中枢）。所以这里必须聚类 + 按活跃天数过滤，否则图上是几十个闪烁的框。

📌 **实测副产品（2026-09-10，八只 / 近三个月）**：绝大多数信号**只活 1~2 天**
（31 次首现里 22 次 ≈71% 在 ≤2 日内消失）
（META 三个月里出了五次 s3，全是短命的；NVDA 七次首现里多次 ×1d）。
所以本图把**信号存活天数编码成标记大小** —— 一眼就能看出某只票的结构是"稳"还是"天天翻脸"，
这比单纯标一个三角形有用得多。它同时是 `insight_chan_right_edge` 那条记忆的可视化。

📌 另一条实测结论：`compute_chan_signal` **只在末笔已定笔时才发买卖点**，
所以重放出来的每一个标记天然都是"定笔✓"。图上不再重复标注这个恒真的字段
（`stroke_confirmed` 的护栏作用发生在上游，不在这里）。

—— 本模块 **advisory / 只读**：不改缠论本体、不产生任何交易指令、不写台账。
"""
from __future__ import annotations

import sys
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

VIEW_MONTHS   = 6      # 视窗：近六个月
MIN_BARS_CHAN = 200    # compute_chan_signal 自身的硬下限

# ── 中枢带的聚类与筛选 ────────────────────────────────────────
# as-of 重放出来的 current_pivot **不是缓慢延伸，而是在两三个候选之间来回跳**
# （实测 AAPL 128 根 K 里换了 39 段，在 200.53~214.74 与 256.20~279.27 之间反复横跳）——
# 因为 `find_latest_pivot(strokes, lookback=12)` 的搜索窗随笔数变化而整体平移，
# 找到的是**另一个**中枢而非同一个的延伸。逐段画会得到几十个闪烁的矩形。
# 故按价位聚类：ZD 与 ZG 都在 ±PIVOT_TOL 内视为同一个中枢。
PIVOT_TOL       = 0.012   # ±1.2%
MIN_PIVOT_DAYS  = 5       # 活跃不足此天数的中枢不画（长尾全是 1~2 天的抖动）
MAX_PIVOT_BANDS = 6       # 最多画几条，避免糊成一团；今日活跃的那条**永远**入选

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
def _asof_replay(ticker: str, df: pd.DataFrame,
                 view_dates: pd.DatetimeIndex) -> tuple[List[dict], List[dict]]:
    """对视窗内每一天用 `df.loc[:t]` 重算缠论，返回 (买卖点首现, 中枢聚类)。

    **一趟重放同时拿到两样东西**，中枢是顺手收的，不额外算一遍：
    信号与中枽都来自同一次 `compute_chan_signal`，所以图上「b3 = 回踩中枢上沿 ZG」
    这句话一定对得上 —— 若中枢改用 `build_all_pivots` 另算一遍就对不上了
    （实测 MSFT：`build_all_pivots` 给 388.3~411.4，而发信号用的
    `find_latest_pivot(lookback=12)` 给 478.5~512.8，差了一个量级）。

    信号会连续存在多天（结构没变就一直是那个 b3），逐日画会得到一串一样的三角形。
    这里只记**首现日**（= 你当天才会看到它、才可能据此动手的那一天），
    并把它此后连续存在了几天记进 `days`：

      - `days` 大 ⇒ 结构稳，信号扛住了后续 K 线的重画；
      - `days` = 1 ⇒ 它第二天就不见了 —— 图上会画成一个很小的标记。

    `alive` = 该信号是否一直活到视窗最后一根 K。
    """
    out: List[dict] = []
    clusters: List[dict] = []
    prev: Optional[str] = None
    today_key: Optional[tuple] = None

    for d in view_dates:
        sub = df.loc[:d]
        if len(sub) < MIN_BARS_CHAN:
            continue
        try:
            res = compute_chan_signal(ticker, {ticker: sub})
        except Exception as e:                       # 单日失败不该毁掉整张图
            logger.debug(f"[Chart] {ticker} {d.date()} 缠论重算失败: {e}")
            continue

        # ── 中枢：按价位聚类（理由见 PIVOT_TOL 处的注释）──────────
        cp = res.current_pivot
        if cp and cp.get("ZD") and cp.get("ZG"):
            zd, zg = float(cp["ZD"]), float(cp["ZG"])
            today_key = (zd, zg)
            hit = next((c for c in clusters
                        if abs(c["zd"] - zd) / zd < PIVOT_TOL
                        and abs(c["zg"] - zg) / zg < PIVOT_TOL), None)
            if hit:
                hit["days"] += 1
                hit["last"] = d
                hit["zd"], hit["zg"] = min(hit["zd"], zd), max(hit["zg"], zg)
            else:
                clusters.append({"zd": zd, "zg": zg, "days": 1,
                                 "first": d, "last": d})

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
                    # 发出该信号那一刻的中枢 —— hover 里给出，便于核对
                    # 「b3 是不是真的贴着当时的 ZG」
                    "pv": (round(cp["ZD"], 2), round(cp["ZG"], 2)) if cp else None,
                })
            prev = sig
        elif sig and out:
            out[-1]["days"] += 1

    if out and prev:                 # 最后一段仍在 ⇒ 该信号活到今天
        out[-1]["alive"] = True

    # 标出今日活跃的那个中枢（= 今天发信号所依据的那个）
    for c in clusters:
        c["current"] = bool(
            today_key
            and abs(c["zd"] - today_key[0]) / today_key[0] < PIVOT_TOL
            and abs(c["zg"] - today_key[1]) / today_key[1] < PIVOT_TOL
        )
    # 长尾全是 1~2 天的抖动，画出来只会糊；但今日活跃的那条**永远**保留，
    # 哪怕它只活跃了一天（TSLA 实测就是这种情况）。
    keep = [c for c in clusters if c["days"] >= MIN_PIVOT_DAYS or c["current"]]
    keep.sort(key=lambda c: (not c["current"], -c["days"]))
    keep = keep[:MAX_PIVOT_BANDS]

    # ── 今日中枢的**真实构成笔跨度**（2026-09-15 修）──────────────
    # `current_pivot` 只给 {ZD,ZG,mid,strokes计数}，**没有日期**，所以此前无从知道
    # 这个中枢的构成笔到底什么时候就结束了 —— 于是图上一律画到右缘，把一个
    # 早已不再纳入新笔的中枢标成「今日活跃」。实测 AAPL 的构成笔结束于
    # 2025-12-09，却被画满到 2026-09-14：**虚画 279 天**。
    # 故照 `compute_chan_signal` 的同一套管线（含 lookback=12）重算一次取 end_date。
    cur = next((c for c in keep if c["current"]), None)
    if cur is not None:
        try:
            from signals.chan.fractal import process_bars, detect_fractals
            from signals.chan.stroke import build_strokes
            from signals.chan.pivot import find_latest_pivot
            from signals.chan.chan_signal import STROKE_EXTREME_FIRST
            sub = df.loc[:view_dates[-1]]
            pv = find_latest_pivot(
                build_strokes(detect_fractals(process_bars(sub)),
                              extreme_first=STROKE_EXTREME_FIRST), lookback=12)
            # ⚠️ 必须校验是同一个中枢再用它的日期。重算与重放若因任何原因分歧，
            # **宁可不画延伸，也不要把别的中枢的日期安在这条带子上**。
            if (pv is not None and pv.end_date is not None
                    and abs(pv.zd - cur["zd"]) / cur["zd"] < PIVOT_TOL
                    and abs(pv.zg - cur["zg"]) / cur["zg"] < PIVOT_TOL):
                cur["stroke_end"] = pv.end_date
                cur["stroke_start"] = pv.start_date
        except Exception as e:                    # 取不到就不画延伸，不猜
            logger.debug(f"[Chart] {ticker} 中枢跨度重算失败: {e}")
    return out, keep


def _marker_size(days: int) -> float:
    """存活天数 → 标记大小。1 天 ≈ 10px，≥8 天封顶 ≈ 23px。"""
    return 9.0 + min(days, 8) * 1.75


# ────────────────────────── 画图 ──────────────────────────
def _build_figure(ticker: str, sleeve: str, view: pd.DataFrame,
                  markers: List[dict], pivots: List[dict]):
    import plotly.graph_objects as go

    fig = go.Figure()
    x0v, x1v = view.index[0], view.index[-1]

    # y 轴锁死在价格区间上。必须锁：中枢可能远在价格之外
    # （实测 AAPL 现价 315，而今日操作中枢是 200.53~214.74），
    # 不锁的话一条中枢带就能把所有蜡烛压成一条线。
    lo, hi = float(view["Low"].min()), float(view["High"].max())
    if markers:   # 标记画在 low×0.978 / high×1.022，且下方/上方还要留字，一并纳入
        lo = min(lo, min(m["low"] * 0.978 for m in markers))
        hi = max(hi, max(m["high"] * 1.022 for m in markers))
    pad = (hi - lo) * 0.07
    yrange = [lo - pad, hi + pad]

    # ── 中枢带（虚线边框 + 阴影）──────────────────────────────
    # 画成 Scatter 而非 shape：这样可 hover、可在图例里整组开关。
    offscreen, hist_legend_done = [], False
    for p in sorted(pivots, key=lambda c: c["current"]):
        cur = p["current"]
        # ⚠️ 今日中枢用**构成笔的真实跨度** [stroke_start, stroke_end]，
        # 而不是 first/last（那是「它在重放里作为最新中枢存在的那段日子」——
        # 完全是另一回事，且 first 可能**晚于** stroke_end：中枢的笔先走完，
        # 之后才因为迟迟形不成新中枢而"成为最新"。用 first 会画出零宽度带子）。
        # 历史带没有重算跨度，仍用 first/last 的「操作窗口」口径，hover 里写明。
        stale_from = p.get("stroke_end") if cur else None
        if stale_from is not None:
            a = max(p.get("stroke_start", p["first"]), x0v)
            b = max(min(stale_from, x1v), a)
        else:
            a = max(p["first"], x0v)
            b = x1v if cur else max(p["last"], a)
        if p["zg"] < yrange[0] or p["zd"] > yrange[1]:  # 整条在视窗价格区间之外
            offscreen.append(p)
            continue
        fig.add_trace(go.Scatter(
            x=[a, b, b, a, a],
            y=[p["zd"], p["zd"], p["zg"], p["zg"], p["zd"]],
            mode="lines", fill="toself",
            fillcolor="rgba(180,83,9,.13)" if cur else "rgba(100,116,139,.07)",
            line=dict(color="#b45309" if cur else "#94a3b8",
                      width=1.6 if cur else 1, dash="dash"),
            name=("中枢·今日活跃" if cur else "中枢·历史"),
            legendgroup="pivot_cur" if cur else "pivot_hist",
            # 图例只给每组第一条**真正画出来的**band —— 绑在下标上会在
            # 恰好该条离屏时把整组图例弄丢
            showlegend=cur or not hist_legend_done,
            hovertemplate=(
                f"<b>中枢</b> {'（今日最新）' if cur else '（历史）'}<br>"
                f"ZG 上沿 {p['zg']:.2f}<br>ZD 下沿 {p['zd']:.2f}<br>"
                + (f"构成笔跨度 {a:%Y-%m-%d} → {b:%Y-%m-%d}"
                   if stale_from is not None else
                   f"作为最新中枢存在 {p['days']} 天 · {p['first']:%m-%d}→{p['last']:%m-%d}"
                   "<br><i>（历史带为「操作窗口」口径，非构成笔跨度）</i>")
                + "<extra></extra>"
            ),
        ))
        hist_legend_done = hist_legend_done or not cur

        # 构成笔结束之后那一段：淡色点线，明示「仍是最新中枢，但已无新笔并入」
        if stale_from is not None and stale_from < x1v:
            stale_days = int((x1v - stale_from).days)
            fig.add_trace(go.Scatter(
                x=[stale_from, x1v, x1v, stale_from, stale_from],
                y=[p["zd"], p["zd"], p["zg"], p["zg"], p["zd"]],
                mode="lines", fill="toself",
                fillcolor="rgba(180,83,9,.04)",
                line=dict(color="#b45309", width=1, dash="dot"),
                name="中枢·已无新笔并入",
                legendgroup="pivot_stale", showlegend=True,
                hovertemplate=(
                    "<b>中枢已停更</b><br>"
                    f"构成笔止于 {stale_from:%Y-%m-%d}<br>"
                    f"此后 <b>{stale_days} 天</b>没有新笔并入<br>"
                    "<i>它仍是 find_latest_pivot 返回的「最新中枢」，"
                    "买卖点仍按它判定</i><extra></extra>"
                ),
            ))

    # 今日活跃中枢的 ZG/ZD 拉成全幅虚线 + 右侧价格标注（这两个价位是可操作的）
    cur_p = next((p for p in pivots if p["current"]), None)
    if cur_p and not any(p is cur_p for p in offscreen):
        for key, dash, txt in (("zg", "dash", "ZG 上沿"), ("zd", "dot", "ZD 下沿")):
            if yrange[0] <= cur_p[key] <= yrange[1]:
                fig.add_hline(
                    y=cur_p[key], line=dict(color="#b45309", width=1, dash=dash),
                    annotation_text=f"{txt} {cur_p[key]:.2f}",
                    annotation_position="right",
                    annotation_font=dict(size=10, color="#b45309"),
                )

    # ── 离屏的今日中枢：贴边提示（2026-09-15 加）────────────────────
    # y 轴锁在价格区间上，中枢落在区间外就整条不画 —— 于是**最该被看见的那个反而
    # 没有任何图形**：AAPL 的中枢 200.53~214.74 已 279 天无新笔并入，而现价 332，
    # 它被裁掉后图上一片干净，看不出「买卖点正按一个九个月前的中枢在判」。
    # 故贴着视窗上/下沿画一条提示带 + 箭头文字，把它拉回视野。
    if cur_p is not None and any(p is cur_p for p in offscreen):
        below = cur_p["zg"] < yrange[0]
        px_now = float(view["Close"].iloc[-1])
        edge = cur_p["zg"] if below else cur_p["zd"]
        gap = abs(px_now - edge) / px_now * 100
        se = cur_p.get("stroke_end")
        stale = (f"，已 {int((x1v - se).days)} 天无新笔并入"
                 if se is not None and se < x1v else "")
        fig.add_annotation(
            xref="paper", yref="paper",
            x=0.5, y=(0.0 if below else 1.0),
            xanchor="center", yanchor=("bottom" if below else "top"),
            showarrow=False,
            text=(f"{'↓' if below else '↑'} 今日中枢 "
                  f"<b>{cur_p['zd']:.2f} ~ {cur_p['zg']:.2f}</b> 在视窗"
                  f"{'下方' if below else '上方'}（距现价 {gap:.0f}%）{stale}"
                  f"　—— 未画出，但<b>买卖点仍按它判定</b>"),
            font=dict(size=11, color="#7c2d12"),
            bgcolor="rgba(254,243,199,.92)",
            bordercolor="#b45309", borderwidth=1, borderpad=4,
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
            mode="markers+text", name=f"{typ}（{len(pts)}）",
            # 三角形本身不够醒目 —— 每个标记旁边直接写出 b1/b2/b3 · s1/s2/s3
            text=[m["type"] for m in pts],
            textposition="bottom center" if symbol.endswith("up") else "top center",
            textfont=dict(size=11, color=color,
                          family="ui-monospace, SFMono-Regular, Menlo, monospace"),
            cliponaxis=False,          # 贴边的标签不要被坐标轴裁掉
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
                         "是" if m["divergence"] else "否",
                         f"{m['pv'][0]:.2f}~{m['pv'][1]:.2f}" if m.get("pv") else "—",
                         ] for m in pts],
            hovertemplate=(
                "<b>%{customdata[0]}</b> · %{x|%Y-%m-%d}<br>"
                "%{customdata[1]}<br>"
                "收盘 %{customdata[4]:.2f}<br>"
                "存活 <b>%{customdata[2]} 日</b>（%{customdata[3]}）<br>"
                "结构置信度 %{customdata[5]:.2f} · 背驰 %{customdata[6]}<br>"
                "<i>当时中枢 %{customdata[7]}</i>"
                "<extra></extra>"
            ),
        ))

    fleeting = sum(1 for m in markers if m["days"] <= 2)
    label, lcolor = SLEEVE_LABEL[sleeve]
    subtitle = (f"{len(markers)} 次信号首现，其中 <b>{fleeting}</b> 次 ≤2 日内消失"
                if markers else "近六个月无缠论买卖点")
    if cur_p:
        pos = ("价<b>在中枢之上</b>" if float(view["Close"].iloc[-1]) > cur_p["zg"]
               else "价<b>在中枢之下</b>" if float(view["Close"].iloc[-1]) < cur_p["zd"]
               else "价<b>在中枢之内</b>")
        subtitle += (f"　·　今日中枢 {cur_p['zd']:.2f}~{cur_p['zg']:.2f}（{pos}）")
        se = cur_p.get("stroke_end")
        if se is not None and se < x1v:
            # 停更天数直接写进标题 —— 靠 hover 才看得见等于看不见，
            # 而「这个中枢还新不新」恰恰决定了买卖点值不值得信。
            subtitle += (f"，<span style='color:#b45309'>构成笔止于 {se:%m-%d}，"
                         f"已 <b>{int((x1v - se).days)} 天</b>无新笔并入</span>")
    if offscreen:
        subtitle += (f"　·　<span style='color:#b45309'>{len(offscreen)} 条中枢在"
                     f"视窗价格区间之外未画</span>")

    fig.update_layout(
        title=dict(
            text=(f"<b>{ticker}</b>　<span style='font-size:12px;color:{lcolor}'>{label}</span>"
                  f"<br><span style='font-size:12px;color:#64748b'>{subtitle}"
                  f"　·　标记大小 = 信号存活天数　·　描边 = 该信号仍在</span>"),
            x=0.012, xanchor="left", font=dict(size=19, color="#0f172a"),
        ),
        height=680, template="plotly_white",
        margin=dict(l=56, r=140, t=88, b=44),
        xaxis=dict(
            rangeslider=dict(visible=False), showgrid=True,
            gridcolor="#f1f5f9", tickformat="%m-%d",
            # 去掉周末与休市日的空档，否则蜡烛之间全是断裂的白条
            rangebreaks=[dict(values=_missing_days(view.index))],
        ),
        yaxis=dict(title="价格 (USD)", showgrid=True, gridcolor="#f1f5f9",
                   side="right", tickformat=".2f", range=yrange),
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


# ────────────────────────── 信号年龄 / 预备 b3 ──────────────────────────
# 2026-10-08 探索性诊断（scratchpad，未预注册；29 只钉死价格 × 2022-01-03~2026-09-08，
# 逐日标签与 prereg NEW2 记录对拍 32,439 天 0 差异）。用户问「能否预判谁要出买点、别再错过」：
#   · b3 首现日收盘中位已在 ZG 之上 **+11.4%**、距回踩低点 +4.9%（低点→标签 3TD）——
#     「错过」主要是标签按 15 个日历日新鲜度**在价格跑远后仍挂着**，不是发现得晚；
#   · 预判**能**做到：proto-b3（下方定义）8TD 内转正式 b3 **57.7%**（173/300）；
#   · 但**按预判提前下单更差**：proto 日买 f20 中位 −0.07%、胜 48.0%；在 ZG×1.03 挂限价等回踩
#     −0.83%、胜 48.2%（回到 ZG 的多半是失败回踩）；确认日买 +0.46%、胜 52.2%；随机 +1.61%、胜 56.1%。
# ⇒ 这两样只做**呈现**（用户 2026-10-08 批准）：不进任何打分、不给入场价、不是买入扳机。
PROTO_STATS = dict(n=300, conv=173, window_td=8)
PROTO_MAX_LOOKBACK = 10        # 往回数「已在该状态几天」最多数到这里


def _signal_age_html(markers: List[dict], view: pd.DataFrame) -> str:
    """今日仍活着的买点：首现日、距今几个交易日、首现后涨了多少、现价距发信号时的 ZG 多远。

    排行只看「今天有没有 b3」，看不见它是今天刚出的还是一周前出的、价格已跑远 ——
    这一行就是把这两者分开。"""
    live = next((m for m in reversed(markers) if m["alive"] and m["kind"] == "buy"), None)
    if live is None:
        return ""
    px = float(view["Close"].iloc[-1])
    ago = int((view.index > live["date"]).sum())
    s = (f"信号年龄：{live['type']} 首现于 {live['date']:%Y-%m-%d}（{ago} 个交易日前，"
         f"首现收盘 {live['close']:.2f}），至今 {px / live['close'] - 1:+.1%}")
    if live.get("pv"):
        s += f"；现价距发信号时的 ZG {live['pv'][1]:.2f} {px / live['pv'][1] - 1:+.1%}"
    stale = ago >= 3 or px / live["close"] - 1 >= 0.03
    return f'<p class="note">{"<span class=warn>" if stale else ""}{s}{"</span>" if stale else ""}</p>'


def _proto_b3(df: pd.DataFrame) -> Optional[dict]:
    """几何上已满足 b3、但尚未出 b3 标签（=末笔未定笔 / 分型未停顿 / 已过新鲜度）。

    定义与 2026-10-08 诊断**逐字相同**（改它就不能再引用 57.7% 那个数）：
    有中枢 · 末笔 down · 前一笔 up 且高点 > ZG · ZG×0.99 ≤ 末笔低点 ≤ ZG×1.20 ·
    现价 ≥ ZG×0.99 · 今日 buy_point ≠ b3。
    """
    from signals.chan.fractal import process_bars, detect_fractals
    from signals.chan.stroke import build_strokes
    from signals.chan.pivot import find_latest_pivot
    from signals.chan.chan_signal import STROKE_EXTREME_FIRST

    st = build_strokes(detect_fractals(process_bars(df)), extreme_first=STROKE_EXTREME_FIRST)
    if len(st) < 3:
        return None
    pv = find_latest_pivot(st, lookback=12)
    last, prev, px = st[-1], st[-2], float(df["Close"].iloc[-1])
    if not (pv and last.direction == "down" and prev.direction == "up" and prev.high > pv.zg
            and pv.zg * 0.99 <= last.low <= pv.zg * 1.20 and px >= pv.zg * 0.99):
        return None
    if compute_chan_signal("_", {"_": df}).buy_point_type == "b3":
        return None
    return dict(zg=pv.zg, zd=pv.zd, low=last.low, low_date=last.end_date, price=px)


def proto_b3_watchlist(decisions: Dict, prices: Dict[str, pd.DataFrame],
                       exclude: set, pipeline=None) -> List[dict]:
    """全部战术可选名里处于 proto-b3 的票 + 已在该状态几天（诊断的转化率是从**进入**状态那天量的）。"""
    rows = []
    for t, d in decisions.items():
        if t in exclude:
            continue
        df = prices.get(t)
        if (df is None or df.empty) and pipeline is not None:
            try:
                df = pipeline.get_price(t)
            except Exception:
                df = None
        if df is None or len(df) < MIN_BARS_CHAN + PROTO_MAX_LOOKBACK:
            continue
        df = df.sort_index()
        try:
            now = _proto_b3(df)
            if now is None:
                continue
            days = 1
            while days <= PROTO_MAX_LOOKBACK and _proto_b3(df.iloc[:-days]) is not None:
                days += 1
        except Exception as e:
            logger.debug(f"[Chart] {t} proto-b3 判定失败: {e}")
            continue
        rows.append(dict(ticker=t, rating=d.rating, days=days, **now))
    rows.sort(key=lambda r: (r["days"], r["ticker"]))
    return rows


def _proto_b3_html(rows: List[dict]) -> str:
    s = PROTO_STATS
    head = (f'<div class="watch"><h2>预备 b3 关注名单（{len(rows)} 只）—— 关注名单，<b>不是买入信号</b></h2>'
            f'<p>几何上已满足三买（离开中枢后回踩、低点守在 ZG 之上），只差定笔 / 分型停顿。'
            f'历史上从<b>进入此状态那天</b>起 {s["window_td"]} 个交易日内转成正式 b3 的比例 '
            f'<b>{s["conv"] / s["n"]:.1%}</b>（{s["conv"]}/{s["n"]}）。'
            f'<span class="warn">但在这一步就买，样本里 20 日中位 −0.07%、胜率 48%，'
            f'不如等正式 b3（+0.46% / 52%）；挂在 ZG 附近等回踩更差（−0.83% / 48%）'
            f'——提前知道是为了设提醒、备好方案，不是提前下单。</span>'
            f'<br><small>出处：2026-10-08 探索性诊断，29 只 × 2022-01~2026-09，未预注册、无止损与成本、'
            f'置信区间含 0；三种做法都不优于随机（+1.61% / 56%）。'
            f'「已在状态」超过 {s["window_td"]} 天的，上面的转化率对它没有样本支撑。</small></p>')
    if not rows:
        return head + "<p>今日没有处于该状态的战术可选名。</p></div>"
    body = "".join(
        f'<tr><td><b>{r["ticker"]}</b></td><td>{r["rating"]}</td>'
        f'<td>{">" if r["days"] > PROTO_MAX_LOOKBACK else ""}{min(r["days"], PROTO_MAX_LOOKBACK)}'
        f'{" ⚠️" if r["days"] > s["window_td"] else ""}</td>'
        f'<td>{r["price"]:.2f}</td><td>{r["zd"]:.2f} ~ {r["zg"]:.2f}</td>'
        f'<td>{r["low"]:.2f}（{r["low_date"]:%m-%d}）</td><td>{r["low"] / r["zg"] - 1:+.1%}</td>'
        f'<td>收盘 &lt; {r["zg"] * 0.99:.2f}</td></tr>'
        for r in rows)
    return (head + '<table><tr><th>票</th><th>今日评级</th><th>已在状态(TD)</th><th>现价</th>'
            '<th>中枢 ZD~ZG</th><th>回踩低点</th><th>低点距 ZG</th><th>失效</th></tr>'
            + body + "</table></div>")


# ────────────────────────── 入口 ──────────────────────────
def write_chan_charts(prices: Dict[str, pd.DataFrame], date_str: str,
                      output_dir: Path, pipeline=None,
                      names: Optional[List[tuple[str, str]]] = None,
                      filename: str = "mag8_chan.html",
                      title: str = "七巨头缠论 K 线图",
                      notes: Optional[Dict[str, str]] = None,
                      signal_age: bool = False,
                      extra_html: str = "") -> Optional[Path]:
    """为 `names`（默认 MAG8）生成缠论 K 线图，返回 html 路径（失败返回 None）。

    `notes`：可选，每只票一行 HTML，画在该页图的上方（战术前五用它写评级/风控标）。
    `signal_age`：在 notes 下再写一行今日活跃买点的年龄（来自同一趟 as-of 重放，不另算）。
    `extra_html`：插在图与图例之间的整块（战术前五用它放预备 b3 名单）。

    `prices` 直接复用 `main.py` 已在内存里的那一份（同一份 800 天缓存）——
    **不重新下载**。重新取一次就等于给项目造出第三份价格真相，
    而这个项目已经因为"两条管线共用同一份缓存"吃过亏（NaN 尾行事故）。
    池轮动导致某只不在 `prices` 里时，才用同一个 `pipeline`（同 key、同窗口）补取。
    """
    try:
        import plotly.graph_objects as go       # noqa: F401
        import plotly.offline as pyo
    except ImportError:
        # ⚠️ ERROR 而非 WARNING（2026-09-14 升级）：这条曾以 WARNING 静默了两天 ——
        # 09-12/09-14 的 main.py 都没出图，而日志看着像一次无害的 skip。
        # 「静默降级」正是本项目反复吃亏的那类缺陷，图没出来就该像失败一样响。
        #
        # 而且原文案「未安装 plotly」**本身就在误导**：当时 plotly 装着（conda 3.13 里有
        # 6.9.0），只是 main.py 跑在 .venv 3.12 上而那里没有。真正的根因是**解释器不是
        # 你以为的那个**，所以这里必须把 sys.executable 打出来 —— 有这一行，两个环境的
        # 问题当场就能看见，不必去翻日志猜。
        logger.error(
            f"[Chart] 缠论 K 线图未生成：当前解释器 {sys.executable} 里没有 plotly。"
            f"（注意这不等于「没装过」——很可能装在了另一个解释器里。）"
            f" 修复：uv pip install --python {sys.executable} 'plotly>=6.0'"
        )
        return None

    charts_dir = output_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    blocks, tabs, skipped = [], [], []
    for i, (ticker, sleeve) in enumerate(names or MAG8):
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

        markers, pivots = _asof_replay(ticker, df, view.index)
        fig = _build_figure(ticker, sleeve, view, markers, pivots)
        div_id = f"chart_{ticker}"
        note_html = (f'<p class="note">{notes[ticker]}</p>'
                     if notes and notes.get(ticker) else "")
        if signal_age:
            note_html += _signal_age_html(markers, view)
        blocks.append(
            f'<div class="pane" id="pane_{ticker}" style="display:'
            f'{"block" if not blocks else "none"}">' + note_html
            + fig.to_html(full_html=False, include_plotlyjs=False,
                          div_id=div_id, config={"displaylogo": False,
                                                 "scrollZoom": True})
            + "</div>"
        )
        fleeting = sum(1 for m in markers if m["days"] <= 2)
        tabs.append((ticker, sleeve, len(markers), fleeting))
        logger.debug(f"[Chart] {ticker}: {len(markers)} 次首现 / {fleeting} 次 ≤2日"
                     f" / {len(pivots)} 条中枢带")

    if not blocks:
        logger.warning(f"[Chart] 无可绘制标的，跳过（{', '.join(skipped) or '原因未知'}）")
        return None

    out = charts_dir / filename
    out.write_text(_render_shell(tabs, blocks, date_str, skipped, pyo, title, extra_html),
                   encoding="utf-8")
    if skipped:
        logger.warning(f"[Chart] 跳过 {len(skipped)} 只: {', '.join(skipped)}")
    return out


TOP_N = 5


def write_top_charts(decisions: Dict, prices: Dict[str, pd.DataFrame], date_str: str,
                     output_dir: Path, pipeline=None, n: int = TOP_N) -> Optional[Path]:
    """`daily_summary.md`「综合评级排行」里前 n 只**战术可选**名的缠论 K 线图。

    产出 `output/{date}/charts/top5_chan.html`，画法与 mag8 完全相同（as-of 重放）。

    **为什么剔除核心名与基准**：核心六只 + QQQ 在战术侧 `tactical_buyable=false`，
    评级是分析结论不是下单指令，且它们已在 mag8 图里 —— 留着只会挤掉真正可选的短线名。
    故这里的「第 k 名」是**剔除后**的名次，每页上方同时写出它在原排行里的名次。

    **每页上方一行是必读的**：排行按 final_score（55% 缠论）排，而缠论分只看买点类型，
    看不见「这个买点还能不能下手」—— R_MAX_EXCEEDED（止损太远，已降 Hold 清零）、
    B3_WINDOW_PASSED（回踩窗口已过）都会让一只高分票**当下不可执行**。
    量化的趋势/动量两个分项与缠论捕捉的是相反的边（insight_chan_vs_ml），
    并列写出来是为了让两者分歧一眼可见，**不是**给它们重新加权。
    """
    from config.stocks import BENCHMARKS, CORE_HOLDINGS, EARNINGS_WINDOW_TD

    ranked = sorted(decisions.values(), key=lambda d: d.final_score, reverse=True)
    excluded = set(CORE_HOLDINGS) | set(BENCHMARKS)
    picks = [(i + 1, d) for i, d in enumerate(ranked) if d.ticker not in excluded][:n]
    if not picks:
        logger.warning("[Chart] 战术前五：排行里没有可选名，跳过")
        return None

    notes = {}
    for k, (orig, d) in enumerate(picks, 1):
        c, q = d.chan_signal, d.quant_signal
        bp = (c.buy_point_type or "—") if c else "—"
        sp = (c.sell_point_type or "") if c else ""
        weekly = getattr(c, "weekly_trend", "") if c else ""
        r = (d.current_price - d.stop_loss) / d.current_price if d.current_price > 0 else None
        parts = [f"<b>#{k}</b>（原排行 #{orig}）",
                 f"<b>{d.rating}</b> {d.final_score:+.3f}",
                 f"缠论 {bp}{'/' + sp if sp else ''} · 周线 {weekly or '—'}"]
        if q is not None:
            parts.append(f"量化 {q.score:+.2f}（趋势 {q.trend_score:+.2f} · 动量 {q.momentum_score:+.2f}）")
        # 无缠论买点时 risk_overlay 的止损是 VIX 档百分比兜底，不是结构位 ——
        # 那时写 R 就是把一个固定百分比冒充成结构风险，故只在有买点时给 R。
        if c and c.buy_point_type:
            parts.append(f"入场 {d.entry_price_range[0]:.2f}~{d.entry_price_range[1]:.2f} · "
                         f"结构止损 {d.stop_loss:.2f}" + (f" · R {r:.1%}" if r is not None else ""))
        else:
            parts.append(f"无结构买点（止损 {d.stop_loss:.2f} 为百分比兜底，非结构位）")
        warn = [f.split(":")[0] for f in d.risk_flags]      # 旗标格式 "NAME: 说明"
        warn = [w for w in warn if w in ("R_MAX_EXCEEDED", "B3_WINDOW_PASSED", "HIGH_VOL",
                                         "WEEKLY_DOWN", "MACRO_HEADWIND")]
        # 前五是「考虑入场」的名单，评级多为 Hold、拿不到 EARNINGS_SOON 旗标，故按天数直接判
        dte = d.days_to_earnings
        if d.next_earnings and dte is not None and dte >= 0:
            parts.append(f"下次财报 {d.next_earnings}（{dte}TD）")
            if dte <= EARNINGS_WINDOW_TD:
                warn.append("EARNINGS_SOON")
        if warn:
            parts.append('<span class="warn">' + " · ".join(warn) + "</span>")
        notes[d.ticker] = "　|　".join(parts)

    watch = proto_b3_watchlist(decisions, prices, excluded, pipeline=pipeline)
    logger.info(f"[Chart] 预备 b3 关注名单 {len(watch)} 只: "
                f"{', '.join(r['ticker'] for r in watch) or '—'}")
    return write_chan_charts(prices, date_str, output_dir, pipeline=pipeline,
                             names=[(d.ticker, "tactical") for _, d in picks],
                             filename="top5_chan.html",
                             title=f"战术前{len(picks)}名缠论 K 线图", notes=notes,
                             signal_age=True, extra_html=_proto_b3_html(watch))


def _render_shell(tabs, blocks, date_str, skipped, pyo,
                  title: str = "七巨头缠论 K 线图", extra: str = "") -> str:
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
<title>{title} · {date_str}</title>
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
.note{{margin:6px 10px 2px;font-size:13px;color:#334155}}
.note .warn{{color:#b45309;font-weight:600}}
.watch{{margin:16px 0 0;padding:14px 18px;background:#fff;border:1px solid #e2e8f0;
 border-radius:10px;font-size:13px;color:#334155}}
.watch h2{{margin:0 0 6px;font-size:15px}}
.watch .warn{{color:#b45309}}
.watch table{{border-collapse:collapse;margin-top:8px;font-variant-numeric:tabular-nums}}
.watch th,.watch td{{border-bottom:1px solid #e2e8f0;padding:4px 10px;text-align:left}}
code{{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body>
<header>
<h1>{title}</h1>
<p class="sub">as-of <b>{date_str}</b> · 近六个月 · 蜡烛红涨绿跌 ·
 买卖点由 <code>compute_chan_signal</code> <b>逐日 as-of 重放</b>得出，
 <b>不是</b>用全历史几何回头标注（后者会抹掉被重画掉的失败笔 = R1.3 幸存者偏差）。
 页签上的数字是 <b>信号首现次数 / 其中 ≤2 日内消失的次数</b>。</p>
<div class="tabs">{btns}</div>
</header>
<main>{"".join(blocks)}{extra}
<div class="legend">
<div>🔵 <b>买点</b> b1 一买(底背驰) · b2 二买(中枢下沿回踩) · b3 三买(中枢上沿回踩)<br>
🟣 <b>卖点</b> s1 一卖(顶背驰) · s2 二卖 · s3 三卖</div>
<div><b>标记大小 = 信号存活天数</b>（小 = 次日即消失，右端结构不稳）<br>
<b>深色描边 = 该信号至今仍在</b>；无描边 = 已被后续 K 线重画掉</div>
<div><b>虚线框 + 阴影 = 中枢</b>（ZD 下沿 ~ ZG 上沿）。<b style="color:#b45309">橙色 = 今日活跃</b>
 （其 ZG/ZD 另拉全幅虚线并标价），灰色 = 视窗内出现过的历史中枢。
 b3 的定义就是「回踩中枢上沿 ZG」，没有这条带子标记无法解读。</div>
<div>⚠️ 中枢同样是 <b>as-of 重放</b>得到、并按 ±1.2% 聚类的：
 <code>find_latest_pivot</code> 在相邻两日之间会<b>整个跳到另一个中枢</b>
 （AAPL 实测 128 根 K 换了 39 段），不聚类会得到几十个闪烁的框。
 活跃 &lt;5 天的抖动不画，<b>但今日活跃的那条永远画</b>。
 y 轴锁定在价格区间，落在区间外的中枢不画（标题会注明几条）。</div>
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
