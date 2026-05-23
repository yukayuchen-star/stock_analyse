"""
走势中枢（Pivot / Hub）识别

定义（缠论原文）：
  某级别走势中，被连续三笔（或线段）所重叠的部分。
  ZG = min(各笔最高价)   中枢上沿
  ZD = max(各笔最低价)   中枢下沿
  有效中枢：ZG > ZD

延伸：第4笔起，若仍在 [ZD, ZG] 内有重叠，则中枢延伸，同时窄化 ZG/ZD。
突破：超出 [ZD, ZG] 后不再延伸，该中枢结束。

实现策略：
  find_latest_pivot() 从最近 lookback 根笔中反向搜索最新有效中枢，
  兼顾中枢延伸（将后续仍在范围内的笔纳入），供买卖点检测使用。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional

from signals.chan.stroke import Stroke


@dataclass
class Pivot:
    """走势中枢"""
    zd: float                             # 中枢下沿
    zg: float                             # 中枢上沿
    strokes: List[Stroke] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.zg > self.zd

    @property
    def mid(self) -> float:
        return (self.zd + self.zg) / 2

    @property
    def start_date(self):
        return self.strokes[0].start_date if self.strokes else None

    @property
    def end_date(self):
        return self.strokes[-1].end_date if self.strokes else None

    def price_in_pivot(self, price: float, tol: float = 0.01) -> bool:
        """价格是否在中枢内（含容差 tol=1%）"""
        return self.zd * (1 - tol) <= price <= self.zg * (1 + tol)

    def price_above(self, price: float, tol: float = 0.0) -> bool:
        return price > self.zg * (1 - tol)

    def price_below(self, price: float, tol: float = 0.0) -> bool:
        return price < self.zd * (1 + tol)


# ── 主函数 ────────────────────────────────────────────────────

def find_latest_pivot(strokes: List[Stroke], lookback: int = 10) -> Optional[Pivot]:
    """
    从最近 lookback 根笔中，反向搜索最新有效中枢（含延伸）。

    返回最近形成的有效中枢，若无则返回 None。
    """
    if len(strokes) < 3:
        return None

    n = min(lookback, len(strokes))
    recent = strokes[-n:]

    # 从最近端反向扫描（i 是3笔窗口的起点，从尾部往前）
    for start in range(len(recent) - 3, -1, -1):
        s1, s2, s3 = recent[start], recent[start + 1], recent[start + 2]

        zg = min(s1.high, s2.high, s3.high)
        zd = max(s1.low,  s2.low,  s3.low)

        if zg <= zd:
            continue

        # 找到有效中枢基础，尝试向后延伸
        pivot_strokes = [s1, s2, s3]
        j = start + 3
        while j < len(recent):
            sj = recent[j]
            # 延伸条件：笔的价格区间与当前中枢有重叠
            new_zg = min(zg, sj.high)
            new_zd = max(zd, sj.low)
            if new_zg > new_zd and sj.low <= zg and sj.high >= zd:
                zg, zd = new_zg, new_zd
                pivot_strokes.append(sj)
                j += 1
            else:
                break

        return Pivot(zd=round(zd, 4), zg=round(zg, 4), strokes=pivot_strokes)

    return None


def build_all_pivots(strokes: List[Stroke]) -> List[Pivot]:
    """
    构建完整中枢序列（供多中枢历史分析使用，当前 P4 主要用 find_latest_pivot）。
    """
    if len(strokes) < 3:
        return []

    pivots: List[Pivot] = []
    i = 0

    while i <= len(strokes) - 3:
        s1, s2, s3 = strokes[i], strokes[i + 1], strokes[i + 2]
        zg = min(s1.high, s2.high, s3.high)
        zd = max(s1.low,  s2.low,  s3.low)

        if zg <= zd:
            i += 1
            continue

        pivot_strokes = [s1, s2, s3]
        j = i + 3
        while j < len(strokes):
            sj = strokes[j]
            new_zg = min(zg, sj.high)
            new_zd = max(zd, sj.low)
            if new_zg > new_zd and sj.low <= zg and sj.high >= zd:
                zg, zd = new_zg, new_zd
                pivot_strokes.append(sj)
                j += 1
            else:
                break

        pivots.append(Pivot(zd=round(zd, 4), zg=round(zg, 4), strokes=pivot_strokes))
        i = j if j > i + 3 else i + 2

    return pivots
