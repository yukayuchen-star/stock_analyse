"""
A 股持仓跟踪（个人实盘持仓体检）。

读取项目根目录 `holdings.txt`（每行：代码,买入价,买入日期[,股数]），
每天对每支持仓重算缠论信号，给出持有/减仓/卖出/止损诊断 + 浮动盈亏。
这是「从买入那天起，我的持仓现在该怎么办」的回答，与 mainA 的全市场选股区分开。

格式（# 注释、逗号分隔，股数可选）：
    600519,1680.0,2026-05-26
    000651,37.5,2026-05-28,1000

诊断规则（基于缠论结构，非固定百分比）：
- 触发卖点(s1/s2/s3) → 「卖出」
- 跌破结构止损价        → 「止损」
- 末笔向下未停顿 + 浮亏  → 「减仓观察」
- 其余                  → 「持有」
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from data.ashare_loader import load_one_csv, classify_board
from signals.chan.chan_signal_ashare import compute_chan_signal_ashare

_HOLDINGS_FILE = "holdings.txt"


@dataclass
class Holding:
    code: str
    buy_price: float
    buy_date: str
    shares: Optional[float] = None

    # 体检结果
    current_price: float = 0.0
    pnl_pct:       float = 0.0     # 浮动盈亏 %
    action:        str   = "持有"   # 持有/减仓观察/卖出/止损
    sell_point:    Optional[str] = None
    stop_loss:     float = 0.0
    reason:        str   = ""


def load_holdings(path_str: str = _HOLDINGS_FILE) -> List[Holding]:
    """解析 holdings.txt。每行：代码,买入价,买入日期[,股数]；# 注释、空行忽略。"""
    path = Path(path_str)
    if not path.exists():
        return []
    holdings: List[Holding] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        m = re.search(r"\d{6}", parts[0]) if parts else None
        if not m:
            logger.warning(f"[Holdings] 第{lineno}行无有效代码，跳过: {raw!r}")
            continue
        try:
            buy_price = float(parts[1])
        except (IndexError, ValueError):
            logger.warning(f"[Holdings] 第{lineno}行缺买入价，跳过: {raw!r}")
            continue
        buy_date = parts[2] if len(parts) > 2 else "?"
        shares = None
        if len(parts) > 3:
            try:
                shares = float(parts[3])
            except ValueError:
                shares = None
        holdings.append(Holding(code=m.group(0), buy_price=buy_price,
                                buy_date=buy_date, shares=shares))
    return holdings


def _diagnose(h: Holding, chan, price: float) -> None:
    """就地填充 h 的体检结果。"""
    h.current_price = price
    h.pnl_pct = (price - h.buy_price) / h.buy_price if h.buy_price > 0 else 0.0
    h.sell_point = chan.sell_point_type
    h.stop_loss = float(chan.stop_loss) if chan.stop_loss else 0.0

    if chan.sell_point_type:
        h.action = "卖出"
        h.reason = f"触发缠论卖点 {chan.sell_point_type}（{'顶背驰' if chan.sell_point_type=='s1' else '破位'}）"
    elif h.stop_loss and price < h.stop_loss:
        h.action = "止损"
        h.reason = f"现价 {price:.2f} < 结构止损 {h.stop_loss:.2f}"
    elif chan.last_stroke_direction == "down" and not chan.fractal_stop and h.pnl_pct < 0:
        h.action = "减仓观察"
        h.reason = "末笔向下未停顿且浮亏，结构走弱，控制风险"
    else:
        h.action = "持有"
        wk = chan.weekly_trend
        h.reason = f"无卖点、未破止损（周线{wk}，末笔{chan.last_stroke_direction}）"


def evaluate_holdings(folder: str = "processed_stocks_selected",
                      path_str: str = _HOLDINGS_FILE) -> List[Holding]:
    """逐支持仓重算缠论信号并体检。找不到数据/数据不足的持仓跳过并告警。"""
    holdings = load_holdings(path_str)
    if not holdings:
        return []

    base = Path(folder)
    result: List[Holding] = []
    for h in holdings:
        matches = sorted(base.glob(f"*{h.code}*.csv"))
        if not matches:
            logger.warning(f"[Holdings] {h.code} 未找到数据文件，跳过")
            continue
        df = load_one_csv(matches[0])
        if df is None or len(df) < 200:
            logger.warning(f"[Holdings] {h.code} 数据不足，跳过")
            continue
        board = classify_board(h.code)
        df.attrs["board"] = board
        chan = compute_chan_signal_ashare(h.code, df, board)
        price = float(df["Close"].iloc[-1])
        _diagnose(h, chan, price)
        result.append(h)
        logger.info(f"[Holdings] {h.code} {h.action} 浮盈{h.pnl_pct:+.1%} 现价{price:.2f}")
    return result
