"""
包含关系处理 + 顶底分型识别

工程参考：HKUDS/Vibe-Trading 的处理流程
  1. 将原始K线合并包含关系 → 处理后K线（PBar）
  2. 在处理后K线上识别顶底分型

包含关系合并规则（缠论原文）：
  上涨趋势：GG 原则（取两者 high 最高、low 最高）
  下跌趋势：DD 原则（取两者 high 最低、low 最低）
  趋势由前两根处理K线的相对高低决定。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List

import pandas as pd


@dataclass
class PBar:
    """处理后K线（合并包含关系后的标准单元）"""
    raw_idx: int          # 原始 DataFrame integer position（取最后一根原始K线）
    date: pd.Timestamp
    high: float
    low: float


@dataclass
class Fractal:
    """顶底分型（建立在处理后K线上）"""
    pb: PBar              # 分型中间那根处理K线
    kind: str             # "top" | "bottom"
    pbar_idx: int         # 在 PBar 列表中的位置（用于计算笔的间距）


# ── 内部工具 ──────────────────────────────────────────────────

def _merge(a: PBar, b: PBar, up: bool) -> PBar:
    """合并两根有包含关系的K线（GG/DD 原则）。"""
    if up:
        high = max(a.high, b.high)
        low  = max(a.low,  b.low)
        date = a.date if a.high >= b.high else b.date
    else:
        high = min(a.high, b.high)
        low  = min(a.low,  b.low)
        date = a.date if a.low <= b.low else b.date
    return PBar(raw_idx=max(a.raw_idx, b.raw_idx), date=date, high=high, low=low)


# ── 主函数 ────────────────────────────────────────────────────

def process_bars(df: pd.DataFrame) -> List[PBar]:
    """
    原始 OHLCV DataFrame → 处理后K线列表。

    df 必须包含 High / Low 列，index 为 pd.DatetimeIndex。
    """
    if df.empty or len(df) < 2:
        return []

    raw: List[PBar] = [
        PBar(raw_idx=i, date=idx, high=float(row.High), low=float(row.Low))
        for i, (idx, row) in enumerate(df.iterrows())
    ]

    result: List[PBar] = [raw[0]]

    for cur in raw[1:]:
        prev = result[-1]

        # 判断包含关系：A 包含 B 或 B 包含 A
        contained = (
            (prev.high >= cur.high and prev.low <= cur.low) or
            (cur.high  >= prev.high and cur.low  <= prev.low)
        )

        if contained:
            # 合并方向：看前两根处理K线决定上涨/下跌
            up = len(result) < 2 or result[-2].high < prev.high
            result[-1] = _merge(prev, cur, up)
        else:
            result.append(cur)

    return result


def detect_fractals(pbars: List[PBar]) -> List[Fractal]:
    """
    在处理后K线上识别所有顶底分型。

    顶分型：中间K线高点最高 且 低点也最高。
    底分型：中间K线低点最低 且 高点也最低。
    相邻三根 K 线之间不存在包含关系（已由 process_bars 保证）。
    """
    fractals: List[Fractal] = []

    for i in range(1, len(pbars) - 1):
        L, M, R = pbars[i - 1], pbars[i], pbars[i + 1]

        if (M.high > L.high and M.high > R.high and
                M.low > L.low and M.low > R.low):
            fractals.append(Fractal(pb=M, kind="top",    pbar_idx=i))

        elif (M.low < L.low and M.low < R.low and
              M.high < L.high and M.high < R.high):
            fractals.append(Fractal(pb=M, kind="bottom", pbar_idx=i))

    return fractals
