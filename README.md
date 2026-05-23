# 美股量化分析与智能投资系统

**双引擎设计**：缠论（择时）× 量化多因子（选股），宏观制度做风险门控。

## 快速开始

```bash
cp .env.example .env   # 填入 API Keys
python main.py         # 输出报告到 output/YYYY-MM-DD/
```

## 股票池（3 桶）

| 桶 | 标的 |
|----|------|
| Mega-Tech | GOOGL AAPL NVDA MSFT META |
| Consumer | AMZN TSLA |
| Hardware | SNDK VRT |
| 基准 | QQQ SPY ^VIX ^TNX |

---

## 四层架构

```
Layer 4  报告输出    output/YYYY-MM-DD/{TICKER}.md + daily_summary.md
             ▲
Layer 3  决策层      双引擎共振打分 → 5 档评级 + VIX 仓位门控 + 3 桶过滤
             ▲
Layer 2  信号层
         ├── 缠论 (40%)  择时引擎：分型→笔→中枢→买卖点，多级别共振
         ├── 量化 (40%)  选股引擎：趋势+动量+相对强度+量价因子
         └── 宏观 (20%)  风险门控：VIX制度+利率环境+桶强度排名
             ▲
Layer 1  数据底座    yfinance + Alpha Vantage + Finnhub + FRED + SQLite 缓存
```

---

## 打分公式（核心）

```
final_score = 0.40 × chan_score + 0.40 × quant_score + 0.20 × macro_score
```

### 缠论得分（chan_score）

| 信号 | 得分 | 说明 |
|------|------|------|
| 1 买点 + 三级共振 | +1.0 | 最强入场信号 |
| 1 买点 + 二级共振 | +0.8 | 强信号 |
| 2 买点 | +0.5 | 标准追多 |
| 3 买点 | +0.3 | 试仓 |
| 中性（无信号） | 0.0 | 观望 |
| 1/2/3 卖点对称 | 负值 | 离场信号 |

### 量化得分（quant_score）

量化因子分五组，对应 quant.md 五层架构（先用基本面找赢家，趋势确认方向，动量找买点，缠论精细择时）：

| 子因子组 | 权重 | 具体指标 | 对应层 |
|---------|------|---------|-------|
| **基本面** | 15% | Revenue/EPS Growth, ROE, Gross Margin, D/E, PEG | Layer1 长期筛选 |
| **趋势因子** | 25% | SMA20/60/200 位置排列；ADX14；EMA20 斜率 | Layer2 方向确认 |
| **动量因子** | 30% | ROC20；MACD 柱；RSI14；KAMA；Pullback/Breakout 信号 | Layer3 买点（缠论前置接口）|
| **相对强度** | 20% | vs QQQ/SPY 超额收益；桶内横截面 Z-score | 横截面选股 |
| **量价因子** | 10% | OBV 趋势；VWMA20 偏离 | 量价确认 |

```python
quant_score = (
    0.15 × fundamental_score
  + 0.25 × trend_score
  + 0.30 × momentum_score       # 动量层内置 Pullback/Breakout，是缠论买点的量化前置
  + 0.20 × relative_strength_score
  + 0.10 × volume_score
)
```

> **Pullback/Breakout 信号**（momentum 内置）：
> - 回调买入：上升趋势中价格接触 EMA20（±3%） → 附加 +0.30（对应缠论二买/三买入场区）
> - 突破信号：价格在 52W 高点 3% 以内 → 附加 +0.20（趋势延续买点）

### 宏观得分（macro_score）

| 指标 | 作用 |
|------|------|
| VIX 四档制度 | 仓位门控（见下） |
| 10Y - 2Y 利差 | 正利差为正，倒挂为负 |
| 桶相对 QQQ 的 IR | 强桶得分更高 |

---

## 三个原创设计

### 1. 双引擎共振逻辑

```
缠论 ↑ + 量化 ↑ + 宏观 ↑ → 三引擎同向，满仓信号
缠论 ↑ + 量化 ↑，宏观中性 → 两引擎同向，标准仓位
缠论 ↑，量化 ↓            → 背离，试仓或观望
缠论 ↓，量化 ↑            → 背离，等缠论确认
```

**缠论与量化背离时，以缠论为准（缠论是结构性判断，量化是统计性判断）。**

### 2. 缠论多级别共振（等精髓输入后实现）

| 级别组合 | 买点级别 | 操作 |
|---------|---------|------|
| 日线 + 60min + 30min | 大级别 1 买点 | 重仓 |
| 日线 + 60min | 中级别 2 买点 | 标准仓位 |
| 仅日线 | 小级别 3 买点 | 试仓 |

### 3. VIX 四档制度仓位

| VIX | 制度 | 仓位上限 | 缠论买点门槛 |
|-----|------|---------|------------|
| < 15 | 平静 | 100% | 接受 2买/3买 |
| 15–25 | 中性 | 70% | 接受 1买/2买 |
| 25–35 | 紧张 | 40% | 仅接受 1买+多级共振 |
| > 35 | 恐慌 | 0% | 全部观望 |

---

## 5 档评级映射

| final_score | 评级 | 建议仓位 |
|------------|------|---------|
| ≥ 0.60 | **Buy** | position_limit × 100% |
| 0.30–0.60 | **Overweight** | position_limit × 70% |
| −0.30–0.30 | **Hold** | 持仓不变 |
| −0.60–−0.30 | **Underweight** | 减半仓 |
| < −0.60 | **Sell** | 清仓 |

---

## 目录结构

```
stock_analyse/
├── main.py
├── config/
│   ├── settings.py
│   └── stocks.py
├── data/
│   ├── base.py              # DataSource Protocol
│   ├── yfinance_source.py
│   ├── alpha_vantage_source.py
│   ├── finnhub_source.py
│   ├── fred_source.py
│   ├── cache.py
│   └── pipeline.py
├── signals/
│   ├── chan/                 # 缠论择时引擎
│   │   ├── fractal.py       # 分型
│   │   ├── stroke.py        # 笔
│   │   ├── segment.py       # 线段
│   │   ├── pivot.py         # 中枢
│   │   ├── trade_point.py   # 买卖点
│   │   ├── multi_level.py   # 多级别共振
│   │   └── chan_signal.py   # → ChanSignalResult
│   ├── quant/               # 量化选股引擎（原 technical/）
│   │   ├── trend.py         # 趋势因子：SMA/EMA/ADX
│   │   ├── momentum.py      # 动量因子：ROC/MACD/RSI/KAMA
│   │   ├── relative.py      # 相对强度：vs QQQ / 桶内 Z-score
│   │   ├── volume.py        # 量价因子：OBV/VWMA
│   │   └── factor_engine.py # → QuantSignalResult
│   └── macro/               # 宏观风险门控
│       ├── regime.py        # VIX 四档制度
│       ├── sector_strength.py
│       └── macro_signal.py  # → MacroSignalResult
├── decision/
│   ├── scorer.py            # 双引擎共振打分
│   ├── risk_overlay.py      # 仓位 + 止损
│   ├── rating.py            # 5 档评级
│   └── strategy.py          # → StockDecision
├── report/
│   ├── templates.py
│   ├── stock_report.py
│   └── daily_report.py
├── utils/
│   ├── logger.py
│   ├── time_utils.py
│   └── exceptions.py
├── output/                  # git ignored
├── cache/                   # git ignored
└── logs/
```

---

## 数据源预算

| API | 限额 | 日均用量 | 主要用途 |
|-----|------|---------|---------|
| yfinance | 无限 | ~30 次 | OHLCV / 指数 / 新闻 |
| Alpha Vantage | 500/天 | ~5 次 | 财报（季度缓存） |
| Finnhub | 60/分钟 | ~20 次 | 新闻情绪 / 盈利日历 |
| FRED | 无限 | ~15 次 | Fed利率 / CPI / VIX |

---

## 开发阶段

| Phase | 内容 | 状态 |
|-------|------|------|
| P0 | 骨架 + config + logger | ✅ 完成 |
| P1 | 数据层 4 源 + SQLite | ✅ 完成 |
| P2 | 量化信号层（fundamental/trend/momentum/relative/volume） | ✅ 完成 |
| P3 | 宏观信号层（VIX制度 + 桶强度） | 待开始 |
| P4 | **缠论信号层**（等用户提供精髓） | 阻塞中 |
| P5 | 决策层（双引擎打分 + 风控） | 待开始 |
| P6 | 报告层 + main.py 端到端 | 待开始 |
| P7+ | LightGBM 增强 / LLM 润色（可选） | 规划中 |

---

## 无前视偏差规则

- **财报**：datadate + 2 个月延迟（Q1→06-01, Q2→09-01, Q3→12-01, Q4→次年 03-01）
- **量化因子**：仅用 close[t-1]，当日收盘后计算，隔日应用
- **缠论**：笔/中枢完成确认后才触发信号，不预测进行中结构
