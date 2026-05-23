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

def build_strokes(fractals: List[Fractal]) -> List[Stroke]:
    """
    从分型列表构建笔列表。

    先对分型做去噪清洗（同向取极值），再按间距规则连笔。
    """
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
