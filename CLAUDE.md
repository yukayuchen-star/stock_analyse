# 美股量化分析系统

每日运行：`python main.py` → 输出 9 只股票 Markdown 研报至 `output/YYYY-MM-DD/`

## 核心设计

**双引擎架构**：缠论（择时）× 量化（选股），宏观制度做过滤门控

```
final_score = 0.40 × chan_score + 0.40 × quant_score + 0.20 × macro_score
```

| 引擎 | 权重 | 职责 | 核心指标 |
|------|------|------|---------|
| **缠论** | 40% | 择时——识别市场结构，捕捉买卖点 | 分型/笔/中枢/买卖点/多级共振 |
| **量化** | 40% | 选股——系统化多因子横截面打分 | 趋势/动量/相对强度/量价因子 |
| **宏观** | 20% | 过滤——制度识别，仓位门控 | VIX四档/利率/桶相对强度 |

三维同向 → 强信号 ｜ 两维同向 → 普通信号 ｜ 背离 → 观望

## 关键约束

**无前视偏差（强制）**：财报延迟 2 个月（Q1 03-31 → 06-01）；技术/量化信号仅用 t-1 收盘；缠论笔/中枢完成后才触发。

**接口契约（不得改签名）**：
- `ChanSignalResult` ← `signals/chan/chan_signal.py`
- `QuantSignalResult` ← `signals/quant/factor_engine.py`
- `MacroSignalResult` ← `signals/macro/macro_signal.py`
- `StockDecision` ← `decision/strategy.py`

**VIX 仓位门控**：<15 满仓 / 15–25 七成 / 25–35 四成 / >35 空仓

## 开发原则

1. 极简优先——不写超前逻辑，不随意重构无关代码
2. 精准修改——仅改动需求指定处
3. 可核验——每步有明确验证标准
4. 缠论模块（`signals/chan/`）等待用户提供精髓后填充

## 技术栈

`yfinance` · `pandas-ta` · `bt` · `pydantic-settings` · `loguru` · `SQLite`

详细架构见 `README.md`。涉及库文档/API 用法时自动调用 Context7。
