"""
笔的构建

规则（缠论原文 + Vibe-Trading 工程实践）：
  1. 相邻笔的端点分型类型必须交替（top→bottom 或 bottom→top）。
  2. 两端分型的处理K线索引间距 >= MIN_BARS（日K取4，防短噪声笔）。
  3. 同向相邻分型保留极值更大的那个（等效于 Vibe-Trading 的去噪合并）。

日K级别说明：
  处理K线索引间距 >= 4 ≈ 原始K线约5-8天，与"笔内至少含一根非端点K线"等价。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List

import pandas as pd

from signals.chan.fractal import Fractal


# 日K级别笔的最小处理K线索引间距（Vibe-Trading 建议5，日K适当放宽到4）
MIN_BARS: int = 4


@dataclass
class Stroke:
    """一笔（连接相邻顶底分型）"""
    start: Fractal       # 起点分型
    end:   Fractal       # 终点分型
    direction: str       # "up"（底→顶）| "down"（顶→底）

    @property
    def high(self) -> float:
        return max(self.start.pb.high, self.end.pb.high)

    @property
    def low(self) -> float:
        return min(self.start.pb.low, self.end.pb.low)

    @property
    def start_date(self) -> pd.Timestamp:
        return self.start.pb.date

    @property
    def end_date(self) -> pd.Timestamp:
        return self.end.pb.date


# ── 主函数 ────────────────────────────────────────────────────

def build_strokes(fractals: List[Fractal], extreme_first: bool = False) -> List[Stroke]:
    """
    从分型列表构建笔列表。

    先对分型做去噪清洗（同向取极值），再按间距规则连笔。

    extreme_first（2026-10-04 加，美股启用；A股/研究代码默认 False = 旧行为）：
    以「端点即极值」为第一不变量构笔，见 `_build_extreme_first`。旧清洗在间距不足时
    无条件 pop 已确立的分型，实测约半数笔端点不是笔内极值（CRDO 6/22 顶 308.67 被删）。
    预注册与 A/B：PREREG-stroke-v2.md。
    """
    if extreme_first:
        return _build_extreme_first(fractals)
    if len(fractals) < 2:
        return []

    # ── 1. 清洗：确保相邻分型类型严格交替，同向取更极端的 ──
    clean: List[Fractal] = [fractals[0]]
    for f in fractals[1:]:
        last = clean[-1]

        if f.kind == last.kind:
            # 同方向：保留更极端的分型
            if f.kind == "top" and f.pb.high >= last.pb.high:
                clean[-1] = f
            elif f.kind == "bottom" and f.pb.low <= last.pb.low:
                clean[-1] = f
            # else: 当前更极端，保持 last
        else:
            # 方向交替，检查索引间距
            if f.pbar_idx - last.pbar_idx >= MIN_BARS:
                clean.append(f)
            else:
                # 间距不足：尝试合并到前一个同向（用更极端的替换）
                if len(clean) >= 2:
                    # 去掉 last，重新检查 f 与 clean[-2] 的关系
                    prev_prev = clean[-2]
                    if f.kind == prev_prev.kind:
                        # f 与 clean[-2] 同向，取极值
                        if f.kind == "top" and f.pb.high >= prev_prev.pb.high:
                            clean[-2] = f
                        elif f.kind == "bottom" and f.pb.low <= prev_prev.pb.low:
                            clean[-2] = f
                        clean.pop()   # 去掉 last（间距不足的那个）

    # ── 2. 构建笔 ───────────────────────────────────────────────
    strokes: List[Stroke] = []
    for i in range(1, len(clean)):
        s, e = clean[i - 1], clean[i]
        if   s.kind == "bottom" and e.kind == "top":
            strokes.append(Stroke(start=s, end=e, direction="up"))
        elif s.kind == "top"    and e.kind == "bottom":
            strokes.append(Stroke(start=s, end=e, direction="down"))

    return strokes


# ── 端点即极值优先的构笔（extreme_first=True）──────────────────────

def _more_extreme(a: Fractal, b: Fractal) -> bool:
    """a 是否比同类分型 b 更极端（顶比高、底比低）。"""
    return a.pb.high > b.pb.high if a.kind == "top" else a.pb.low < b.pb.low


def _build_extreme_first(fractals: List[Fractal]) -> List[Stroke]:
    """
    原文 L81「顶分型的顶至底分型的底」⇒ 向上笔终点须是笔内最高、起点须是笔内最低。

    - 候选终点 = 起点之后**最极端**的反向分型；间距不足 MIN_BARS 的也参与比较，
      只是暂时不能当终点（否则一个较弱的后续分型会因间距够而顶替真极值）。
    - 笔未成立前出现更极端的同向分型，起点直接移过去（前一笔终点随之更新）。
    - 出现与候选终点间距足够的同向（起点类）分型才确认一笔，确认后从终点之后重扫。
    - 「最小间距」与「端点即极值」冲突时（有效回撤后反弹不足 MIN_BARS 即创新极值），
      **间距优先**（2026-10-04 用户裁定规则 A）：此时允许起点偏离极值，实测约 0.4% 的笔。
    - 候选终点 E 之后不足 MIN_BARS 就出现比起点更极端的同向分型 sx（悬置）：S→E 视为未走完，
      在「间距足够的同向分型到来」或「数据到右端」时**起点移到 sx 并从其后重扫**（同属规则 A）。
      不处理则构笔在 sx 未被突破前一直冻结、右端还会丢掉有效末笔（code review 2026-10-04 查出，
      实测 5.0% 的 as-of 日处于此态、最长连续 64 日）。
    """
    if len(fractals) < 2:
        return []
    ends: List[Fractal] = [fractals[0]]   # 已确认端点；ends[-1] = 当前笔起点
    j = 1
    while j < len(fractals):
        start = ends[-1]
        end = None; end_k = None   # 当前候选终点（与起点间距足够）
        blk = None                 # 起点之后最极端的反向分型（含间距不足的）—— 终点不得弱于它
        sx = None; sx_k = None     # 起点之后比起点更极端的同向分型（起点应移过去）
        committed = False
        for k in range(j, len(fractals)):
            f = fractals[k]
            if f.kind != start.kind:
                if blk is None or not _more_extreme(blk, f):
                    blk = f
                if f is blk:
                    if sx is not None and sx.pbar_idx < f.pbar_idx:
                        start = sx; ends[-1] = sx; sx = None
                    if f.pbar_idx - start.pbar_idx >= MIN_BARS:
                        end, end_k = f, k
                    else:
                        end, end_k = None, None
            else:
                if _more_extreme(f, start) and (sx is None or _more_extreme(f, sx)):
                    if end is None:
                        start = f; ends[-1] = f; blk = None; sx = None   # 笔未成，起点直接移过去
                        continue
                    sx, sx_k = f, k
                if end is not None and f.pbar_idx - end.pbar_idx >= MIN_BARS:
                    if sx is None or sx is f:
                        ends.append(end); j = end_k + 1; committed = True
                        break
                    sx_k_pending = sx_k   # 悬置的 sx 未被突破：起点移到 sx 重扫（否则在此冻结）
                    break
        else:
            sx_k_pending = None
            if sx is not None:            # 右端仍悬置：同样移到 sx，不丢有效末笔、不留陈旧末笔
                sx_k_pending = sx_k
        if committed:
            continue
        if sx_k_pending is not None:
            ends[-1] = fractals[sx_k_pending]; j = sx_k_pending + 1
            continue
        if end is not None:
            ends.append(end)   # 右端未确认的末笔（由 chan_signal 的定笔门把关）
        break

    strokes: List[Stroke] = []
    for s, e in zip(ends, ends[1:]):
        if s.kind != e.kind:
            strokes.append(Stroke(start=s, end=e, direction="up" if s.kind == "bottom" else "down"))
    return strokes
