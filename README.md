# US Stock Quant Analysis & Smart Investment System
# 美股量化分析与智能投资系统

**Dual-Engine Design | 双引擎设计**：  
Chan Theory (Timing) × Quant Multi-Factor (Stock Selection), Macro Regime (Risk Gate)  
缠论（择时）× 量化多因子（选股），宏观制度做风险门控

## Quick Start | 快速开始

```bash
cp .env.example .env   # Fill in API Keys | 填入 API Keys
python main.py         # Output report to output/YYYY-MM-DD/ | 输出报告到 output/YYYY-MM-DD/
```

## Stock Pool (3 Buckets) | 股票池（3 桶）

| Bucket | Tickers | 桶 | 标的 |
|--------|---------|-----|------|
| Mega-Tech | GOOGL AAPL NVDA MSFT META | Mega科技 | GOOGL AAPL NVDA MSFT META |
| Consumer | AMZN TSLA | 消费 | AMZN TSLA |
| Hardware | SNDK VRT | 硬件 | SNDK VRT |
| Benchmark | QQQ SPY ^VIX ^TNX | 基准 | QQQ SPY ^VIX ^TNX |

---

## Four-Layer Architecture | 四层架构

```
Layer 4  Report      output/YYYY-MM-DD/{TICKER}.md + daily_summary.md
         报告输出    output/YYYY-MM-DD/{TICKER}.md + daily_summary.md
             ▲
Layer 3  Decision    Dual-engine resonance scoring → 5-tier rating + VIX position control + bucket filter
         决策层      双引擎共振打分 → 5档评级 + VIX仓位门控 + 3桶过滤
             ▲
Layer 2  Signals
         信号层
         ├── Chan Theory (40%)    Timing engine: Fractal→Stroke→Pivot→Trade Points, multi-level resonance
         ├── 缠论 (40%)           择时引擎：分型→笔→中枢→买卖点，多级别共振
         ├── Quant (40%)          Stock selection: trend+momentum+relative strength+volume
         ├── 量化 (40%)           选股引擎：趋势+动量+相对强度+量价因子
         └── Macro (20%)          Risk gate: VIX regime + rate environment + bucket strength
         └── 宏观 (20%)           风险门控：VIX制度+利率环境+桶强度排名
             ▲
Layer 1  Data        yfinance + Alpha Vantage + Finnhub + FRED + SQLite cache
         数据底座    yfinance + Alpha Vantage + Finnhub + FRED + SQLite 缓存
```

---

## Scoring Formula (Core) | 打分公式（核心）

```
final_score = 0.40 × chan_score + 0.40 × quant_score + 0.20 × macro_score
```

### Chan Theory Score (chan_score) | 缠论得分（chan_score）

| Signal | Score | Note | 信号 | 得分 | 说明 |
|--------|-------|------|------|------|------|
| 1st Buy + 3-level resonance | +1.0 | Strongest entry | 1买+三级共振 | +1.0 | 最强入场信号 |
| 1st Buy + 2-level resonance | +0.8 | Strong | 1买+二级共振 | +0.8 | 强信号 |
| 2nd Buy | +0.5 | Standard | 2买 | +0.5 | 标准追多 |
| 3rd Buy | +0.3 | Trial | 3买 | +0.3 | 试仓 |
| Neutral (No signal) | 0.0 | Watch | 中性（无信号） | 0.0 | 观望 |
| Sell points (1/2/3) | Negative | Exit | 卖点对称 | 负值 | 离场信号 |

### Quant Score (quant_score) | 量化得分（quant_score）

Quant factors in five groups for fundamental selection → trend confirmation → momentum entry points → fine timing:  
量化因子分五组，对应 quant.md 五层架构（先用基本面找赢家，趋势确认方向，动量找买点，缠论精细择时）：

| Factor Group | Weight | Metrics | Layer | 子因子组 | 权重 | 具体指标 | 对应层 |
|--------------|--------|---------|-------|---------|------|---------|-------|
| **Fundamentals** | 15% | Rev/EPS Growth, ROE, Gross Margin, D/E, PEG | Layer1 | **基本面** | 15% | Revenue/EPS增长率、ROE、毛利率、债权比、PEG | 长期筛选 |
| **Trend** | 25% | SMA20/60/200 arrangement; ADX14; EMA20 slope | Layer2 | **趋势因子** | 25% | SMA20/60/200位置排列、ADX14、EMA20斜率 | 方向确认 |
| **Momentum** | 30% | ROC20; MACD histogram; RSI14; KAMA; Pullback/Breakout | Layer3 | **动量因子** | 30% | ROC20、MACD柱、RSI14、KAMA、回调/突破信号 | 买点（缠论前置）|
| **Relative Strength** | 20% | Excess return vs QQQ/SPY; Cross-sectional Z-score | Cross-sec | **相对强度** | 20% | vs QQQ/SPY超额收益、桶内横截面Z-score | 选股 |
| **Volume** | 10% | OBV trend; VWMA20 deviation | Confirm | **量价因子** | 10% | OBV趋势、VWMA20偏离 | 确认 |

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

### Macro Score (macro_score) | 宏观得分（macro_score）

| Indicator | Purpose | 指标 | 作用 |
|-----------|---------|------|------|
| VIX 4-Tier Regime | Position control (see below) | VIX 四档制度 | 仓位门控（见下） |
| 10Y - 2Y Yield Spread | Positive spread (+), inversion (−) | 10Y - 2Y 利差 | 正利差为正，倒挂为负 |
| Bucket IR vs QQQ | Stronger buckets score higher | 桶相对 QQQ 的 IR | 强桶得分更高 |

---

## Three Core Design Principles | 三个原创设计

### 1. Dual-Engine Resonance Logic | 双引擎共振逻辑

```
Chan ↑ + Quant ↑ + Macro ↑ → All three aligned, full position
缠论 ↑ + 量化 ↑ + 宏观 ↑ → 三引擎同向，满仓信号

Chan ↑ + Quant ↑, Macro neutral → Two engines aligned, standard position
缠论 ↑ + 量化 ↑，宏观中性 → 两引擎同向，标准仓位

Chan ↑, Quant ↓ → Divergence, trial or watch
缠论 ↑，量化 ↓ → 背离，试仓或观望

Chan ↓, Quant ↑ → Divergence, await chan confirmation
缠论 ↓，量化 ↑ → 背离，等缠论确认
```

**When Chan and Quant diverge, prioritize Chan (structural > statistical judgement).**  
**缠论与量化背离时，以缠论为准（缠论是结构性判断，量化是统计性判断）。**

### 2. Multi-Level Chan Resonance | 缠论多级别共振

| Level Combination | Buy Level | Action | 级别组合 | 买点级别 | 操作 |
|-------------------|-----------|--------|---------|---------|------|
| Daily + 60min + 30min | Major 1st Buy | Heavy position | 日线 + 60min + 30min | 大级别 1 买点 | 重仓 |
| Daily + 60min | Mid 2nd Buy | Standard position | 日线 + 60min | 中级别 2 买点 | 标准仓位 |
| Daily only | Minor 3rd Buy | Trial position | 仅日线 | 小级别 3 买点 | 试仓 |

### 3. VIX 4-Tier Position Control | VIX 四档制度仓位

| VIX | Regime | Position Limit | Chan Entry Threshold | VIX | 制度 | 仓位上限 | 缠论买点门槛 |
|-----|--------|------------------|----------------------|-----|------|---------|------------|
| < 15 | Calm | 100% | Accept 2/3 Buy | < 15 | 平静 | 100% | 接受 2买/3买 |
| 15–25 | Neutral | 70% | Accept 1/2 Buy | 15–25 | 中性 | 70% | 接受 1买/2买 |
| 25–35 | Tense | 40% | 1st Buy + multi-level only | 25–35 | 紧张 | 40% | 仅接受 1买+多级共振 |
| > 35 | Panic | 0% | Sit on sidelines | > 35 | 恐慌 | 0% | 全部观望 |

---

## 5-Tier Rating Map | 5 档评级映射

| final_score | Rating | Position Guidance | final_score | 评级 | 建议仓位 |
|------------|--------|------------------|------------|------|---------|
| ≥ 0.60 | **Buy** | position_limit × 100% | ≥ 0.60 | **买入** | position_limit × 100% |
| 0.30–0.60 | **Overweight** | position_limit × 70% | 0.30–0.60 | **增仓** | position_limit × 70% |
| −0.30–0.30 | **Hold** | Hold current position | −0.30–0.30 | **持仓** | 持仓不变 |
| −0.60–−0.30 | **Underweight** | Reduce to half | −0.60–−0.30 | **减仓** | 减半仓 |
| < −0.60 | **Sell** | Clear position | < −0.60 | **清仓** | 清仓 |

---

## A-Share Chan System | A股缠论系统

Independent A-share track that reuses the market-agnostic Chan engine (fractal→stroke→pivot→trade points). Selection and backtest entries are separate from the US side.
独立 A 股轴，复用市场无关的缠论结构引擎（分型→笔→中枢→买卖点），选股与回测入口与美股侧分离。

```bash
python mainA.py               # A-share selection → output/ashare/{date}/ | A股选股
python run_ashare_backtest.py # Chan win-rate by b1/b2/b3 | 缠论分类型回测胜率
```

| Aspect | US (main.py) | A-Share (mainA.py) | 维度 | 美股 | A股 |
|--------|--------------|--------------------|------|------|-----|
| Data | yfinance live | local CSV `processed_stocks_selected/` | 数据 | yfinance 实时 | 本地 CSV |
| Axes | Chan×Macro×Quant | Chan-only (no macro/quant data) | 轴 | 三轴 | 纯缠论 |
| MACD divergence | recomputed | precomputed `macd` column | 背驰 | 重算 | 用预计算列 |
| Indicators | — | KDJ/RSI divergence + CCI/BOLL strength (confirm only) | 指标 | — | KDJ/RSI背离+CCI/BOLL力度（仅确认） |
| Gating | VIX 4-tier | conservative: b2/b3 priority, b1 strictly gated | 门控 | VIX四档 | 保守：二/三买为主，一买严格门控 |
| Limits | none | board ±10/20/30% modeled in backtest | 涨跌停 | 无 | 回测按板块建模 |
| Risk | — | Livermore 2%: position = RISK_BUDGET / R | 风控 | — | 利弗莫尔2%：仓位=风险预算/R |

**Boards | 板块**: `300/301`→创业板(±20%), `688/689`→科创板(±20%), `8/4`→北交所(±30%), else 主板(±10%). Code is normalized to the 6-digit number from the filename (strips `sh/sz` prefixes).

**Core principle | 核心原则**: 缠论结构（走势形态+中枢）为信号本体；MACD/KDJ/RSI/CCI/BOLL 仅调整 score/confidence 与门控，**绝不独立产生买卖点**。

---

## Directory Structure | 目录结构

```
stock_analyse/
├── main.py                        # 美股选股入口 | US selection entry
├── mainA.py                       # A股选股入口 | A-share selection entry
├── run_ml_backtest.py             # 美股 ML 回测 | US ML backtest
├── run_ashare_backtest.py         # A股缠论历史回测 | A-share Chan backtest
├── config/
│   ├── settings.py
│   ├── stocks.py                  # 美股股票池 | US pool
│   ├── stocks_ashare.py           # A股回测/风控参数 | A-share params
│   └── pool_manager.py
├── data/
│   ├── base.py                    # DataSource Protocol
│   ├── yfinance_source.py
│   ├── alpha_vantage_source.py
│   ├── finnhub_source.py
│   ├── fred_source.py
│   ├── universe.py
│   ├── cache.py
│   ├── pipeline.py
│   └── ashare_loader.py           # A股本地CSV装载 | A-share CSV loader
├── signals/
│   ├── screening.py
│   ├── chan/                      # 缠论引擎（市场无关核心）| Chan engine (core)
│   │   ├── fractal.py             # 分型
│   │   ├── stroke.py              # 笔
│   │   ├── pivot.py               # 中枢
│   │   ├── chan_signal.py         # 美股缠论 → ChanSignalResult
│   │   └── chan_signal_ashare.py  # A股缠论（预计算MACD+指标确认+保守门控）
│   ├── quant/                     # 量化选股引擎
│   │   ├── fundamental.py         # 基本面因子
│   │   ├── trend.py               # 趋势因子：SMA/EMA/ADX
│   │   ├── momentum.py            # 动量因子：ROC/MACD/RSI/KAMA
│   │   ├── relative.py            # 相对强度：vs QQQ / 桶内 Z-score
│   │   ├── volume.py              # 量价因子：OBV/VWMA
│   │   └── factor_engine.py       # → QuantSignalResult
│   └── macro/                     # 宏观风险门控
│       ├── regime.py              # VIX 四档制度
│       ├── external_factors.py    # 利率/油价/美元/通胀异动
│       ├── sector_strength.py
│       └── macro_signal.py        # → MacroSignalResult
├── decision/
│   ├── scorer.py                  # 双主轴共振打分
│   ├── risk_overlay.py            # 仓位 + 止损
│   ├── rating.py                  # 5 档评级
│   ├── strategy.py                # 美股 → StockDecision
│   └── strategy_ashare.py         # A股纯缠论 → AShareDecision
├── backtest/
│   ├── engine.py                  # 美股回测撮合
│   ├── engine_ashare.py           # A股回测（涨跌停/跳空/A股调参）
│   ├── ml_backtest.py             # ML 回测（去泄漏）
│   ├── forward_tracker.py         # 前向跟踪
│   └── report.py
├── report/
│   └── report_writer.py
├── utils/
│   ├── logger.py
│   ├── time_utils.py
│   └── housekeeping.py
├── output/                        # git ignored
├── cache/                         # git ignored
└── logs/
```

---

## Data Source Budget | 数据源预算

| API | Limit | Avg Daily Calls | Primary Use | API | 限额 | 日均用量 | 主要用途 |
|-----|-------|-----------------|------------|-----|------|---------|---------|
| yfinance | Unlimited | ~30 | OHLCV / Indices / News | yfinance | 无限 | ~30 次 | OHLCV / 指数 / 新闻 |
| Alpha Vantage | 500/day | ~5 | Financials (quarterly cache) | Alpha Vantage | 500/天 | ~5 次 | 财报（季度缓存） |
| Finnhub | 60/min | ~20 | News sentiment / Earnings calendar | Finnhub | 60/分钟 | ~20 次 | 新闻情绪 / 盈利日历 |
| FRED | Unlimited | ~15 | Fed rates / CPI / VIX | FRED | 无限 | ~15 次 | Fed利率 / CPI / VIX |

---

## Development Phases | 开发阶段

| Phase | Content | Status | 阶段 | 内容 | 状态 |
|-------|---------|--------|------|------|------|
| P0 | Framework + config + logger | ✅ Complete | P0 | 骨架 + config + logger | ✅ 完成 |
| P1 | Data layer 4 sources + SQLite | ✅ Complete | P1 | 数据层 4 源 + SQLite | ✅ 完成 |
| P2 | Quant signals (fund/trend/mom/rel/vol) | ✅ Complete | P2 | 量化信号层（fundamental/trend/momentum/relative/volume） | ✅ 完成 |
| P3 | Macro signals (VIX regime + bucket strength) | ⏳ In Progress | P3 | 宏观信号层（VIX制度 + 桶强度） | ⏳ 进行中 |
| P4 | **Chan signals** (fractal/stroke/pivot/trade points) | ✅ Complete | P4 | **缠论信号层**（分型/笔/中枢/买卖点） | ✅ 完成 |
| P5 | Decision layer (dual-engine scoring + risk control) | ⏳ In Progress | P5 | 决策层（双引擎打分 + 风控） | ⏳ 进行中 |
| P6 | Report layer + main.py end-to-end | ✅ Complete | P6 | 报告层 + main.py 端到端 | ✅ 完成 |
| P7+ | LightGBM enhancement / LLM polish (optional) | 📋 Planned | P7+ | LightGBM 增强 / LLM 润色（可选） | 📋 规划中 |

---

## No Forward-Bias Rules | 无前视偏差规则

- **Financials | 财报**: datadate + 2-month lag (Q1→06-01, Q2→09-01, Q3→12-01, Q4→next year 03-01)
  
  **财报**：datadate + 2 个月延迟（Q1→06-01, Q2→09-01, Q3→12-01, Q4→次年 03-01）

- **Quant Factors | 量化因子**: Use only close[t-1]; compute after market close; apply next day
  
  **量化因子**：仅用 close[t-1]，当日收盘后计算，隔日应用

- **Chan Signals | 缠论**: Trigger only after stroke/pivot confirmation; do not anticipate in-progress structures
  
  **缠论**：笔/中枢完成确认后才触发信号，不预测进行中结构
