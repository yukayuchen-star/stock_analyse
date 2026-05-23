# CLAUDE.md — 美股量化分析与智能投资系统

## 项目目标
构建融合缠论信号、多因子选股、ML决策的美股量化投研系统，
覆盖选股→回测→风控→策略生成全流程，支持VS Code本地部署运行。

## 双引擎架构

```
final_score = 0.40 × chan_score + 0.40 × quant_score + 0.20 × macro_score
```

| 引擎 | 权重 | 职责 |
|------|------|------|
| 缠论（chan） | 40% | 结构性择时：分型→笔→中枢→买卖点 |
| 量化（quant） | 40% | 统计性选股：五因子横截面评分 |
| 宏观（macro） | 20% | 风险门控：VIX 四档制度 + 利率 + 桶强度 |

**量化五因子**（quant_score = 0.15×fund + 0.25×trend + 0.30×mom + 0.20×rel + 0.10×vol）：
- 基本面 15%：Revenue/EPS Growth, ROE, Gross Margin, D/E, PEG（yfinance info）
- 趋势 25%：SMA20/60/200 + ADX14 + EMA斜率
- 动量 30%：ROC20/MACD/RSI14/KAMA + Pullback/Breakout（缠论买点前置信号）
- 相对强度 20%：vs QQQ/SPY 超额收益 + 桶内横截面 Z-score
- 量价 10%：OBV 趋势 + VWMA20 偏离

**背离规则**：缠论↑量化↓时，以缠论为准（结构 > 统计）。

## 接口契约

| 数据类 | 来源文件 |
|--------|---------|
| `ChanSignalResult` | `signals/chan/chan_signal.py` |
| `QuantSignalResult` | `signals/quant/factor_engine.py` |
| `MacroSignalResult` | `signals/macro/macro_signal.py` |
| `StockDecision` | `decision/strategy.py` |

## VIX 四档仓位门控

| VIX | 仓位上限 | 缠论买点门槛 |
|-----|---------|------------|
| <15 | 100% | 2买/3买 |
| 15–25 | 70% | 1买/2买 |
| 25–35 | 40% | 仅1买+多级共振 |
| >35 | 0% | 全部观望 |

## 开发四原则
1. 先思考再编码：多方案对比，不确定时主动提问
2. 极简优先：不写超前冗余逻辑
3. 精准修改：仅改动需求指定代码
4. 无前视偏差：close[t-1]、财报+2月延迟、笔完成后才触发信号

## Context7规则
涉及库文档、API用法、代码生成时自动调用Context7，无需显式指令。
