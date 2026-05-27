"""
A 股侧配置：数据路径、回测调参、板块仓位上限。

板块涨跌停幅度的权威定义在 data/ashare_loader.py（classify_board/board_limit），
此处只放与策略/回测相关的可调参数，避免散落。
"""
from __future__ import annotations

from data.ashare_loader import board_limit

# 数据来源
ASHARE_DATA_DIR = "processed_stocks_selected"

# ── 回测调参（A 股波动大于美股，止损适度放宽）──────────────────
SL_PCT   = 0.09     # 主板止损基准（A 股默认 9%）
TP_MULT  = 2.0      # 止盈倍数（2:1 R/R）
WARMUP_BARS = 120   # A 股预热：每股仅 ~360TD，降到 120 以换取更长回测窗口
MIN_BACKTEST_BARS = 50

# 板块化止损：恒为该板块单日涨跌停的 0.9×，避免单日波动把仓位无意义扫出。
# main(±10%)→9% 与旧 SL_PCT 一致；chinext/star(±20%)→18%；bse(±30%)→27%。
# R 随之放大 → 仓位 RISK_BUDGET/R 自动缩小，单笔风险仍受控（利弗莫尔 2% 法则）。
_SL_LIMIT_FRAC = 0.90


def sl_pct_for_board(board: str) -> float:
    """按板块涨跌停缩放的止损比例。"""
    return round(board_limit(board) * _SL_LIMIT_FRAC, 4)


# ── 牛熊 regime 分桶（按入场日归桶，区间连续无缝）────────────────
# 用户指定六阶段验证「缠论能否穿越牛熊」；2024.09 后为 924 反弹，作补充行单列。
# 边界取各轮公认拐点：5178(15.06)/熔断底(16.02)/3587(18.01)/2440(19.01)/创业板顶(21.12)/924。
REGIME_PHASES = [
    ("P1", "①牛市",      "牛市",      "2014-07-01", "2015-06-15"),
    ("P2", "②熊市",      "熊市",      "2015-06-15", "2016-02-01"),
    ("P3", "③结构牛市",  "结构牛市",  "2016-02-01", "2018-01-29"),
    ("P4", "④熊市",      "熊市",      "2018-01-29", "2019-01-04"),
    ("P5", "⑤结构牛市",  "结构牛市",  "2019-01-04", "2021-12-13"),
    ("P6", "⑥熊市",      "熊市",      "2021-12-13", "2024-09-24"),
    ("P7", "⑦反弹(补)",  "反弹/震荡", "2024-09-24", "2027-01-01"),
]


def regime_of(ts) -> str:
    """入场日 → regime key（按入场日归桶）；落在所有区间外返回 'NA'。"""
    import pandas as pd
    t = pd.Timestamp(ts)
    for key, _label, _type, s, e in REGIME_PHASES:
        if pd.Timestamp(s) <= t < pd.Timestamp(e):
            return key
    return "NA"

# ── 板块仓位上限（创业/科创/北交波动更大 → 上限更低）─────────────
BOARD_POSITION_CAP = {
    "main":    1.00,
    "chinext": 0.70,
    "star":    0.70,
    "bse":     0.50,
}

# ── 评级阈值 ────────────────────────────────────────────────────
BUY_SCORE_MIN   = 0.50   # ≥ 此分且为 b2/b3 → Buy
WATCH_SCORE_MIN = 0.01   # > 0 且有买点 → Watch

# ── 风控（缠论.md 引用利弗莫尔 2% 风险法则）─────────────────────
RISK_BUDGET = 0.02   # 单笔最大风险占比（仓位 = RISK_BUDGET / R）
R_MAX       = 0.15   # 结构止损距入场 > 15% → 离支撑太远、R/R 差，降级为 Watch
