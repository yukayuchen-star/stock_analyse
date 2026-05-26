"""
A 股侧配置：数据路径、回测调参、板块仓位上限。

板块涨跌停幅度的权威定义在 data/ashare_loader.py（classify_board/board_limit），
此处只放与策略/回测相关的可调参数，避免散落。
"""
from __future__ import annotations

# 数据来源
ASHARE_DATA_DIR = "processed_stocks_selected"

# ── 回测调参（A 股波动大于美股，止损适度放宽）──────────────────
SL_PCT   = 0.09     # 止损比例（A 股默认 9%）
TP_MULT  = 2.0      # 止盈倍数（2:1 R/R）
WARMUP_BARS = 120   # A 股预热：每股仅 ~360TD，降到 120 以换取更长回测窗口
MIN_BACKTEST_BARS = 50

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
