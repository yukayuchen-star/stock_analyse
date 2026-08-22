---
name: ashare-chan
description: A股缠论侧（mainA.py / run_ashare_backtest.py）的适配规则、牛短熊长门控、lb2 Watch-only 纪律、板块涨跌停、利弗莫尔风控与接口契约。改动任何 A股相关文件（mainA.py / run_ashare_backtest.py / *_ashare.py / config/stocks_ashare.py / data/ashare_loader.py）或讨论 A股选股回测前必读。
---

## A股缠论侧（mainA.py / run_ashare_backtest.py）

A 股无宏观/量化数据，独立为**纯缠论轴**，复用市场无关的结构引擎
（fractal/stroke/pivot + chan_signal 的买卖点原语），仅做三处适配：
1. **背驰用预计算 MACD**：直接喂 CSV 的 `macd` 列（=2×(dif−dea)），面积比值法
   对缩放不敏感，与用户软件口径一致；缺列时回退 close 重算。
2. **指标确认层（仅辅助，绝不造信号）**：KDJ/RSI 底背离辅助背驰、CCI+BOLL 力度
   确认，只调 score/confidence 与门控。
3. **牛短熊长保守门控 + 右端防护**（仅选股侧）：二买/三买为主；并叠加
   - 一买需「周线非向下 + 底背离」双确认，否则丢弃；
   - **b1 上涨中继护栏**：周线 up 且现价 > 中枢 ZG → 是上涨回调非下跌末端一买，弃；
   - **b2/b3 中枢新鲜度护栏**（`STALE_PIVOT_TD=25TD`）：中枢末笔距今过久=旧结构，弃；
   - **A 定笔 + C' 波动率 + B 迟滞**（定义见 CLAUDE.md「缠论信号精髓与右端稳定性」节，A股已全部具备）。

**类二买 lb2（右侧）**：上涨中枢震荡「极度缩量 + BOLL收口 + 快速突破 ZG + 不追高」。
回测仅 ~42% 胜率（远弱于 b1/b2/b3），故定为 **Watch-only：检测可见但不进 Buy、不进回测**
（仅供人工观察；如要真正交易需重做入场为「突破后缩量回踩 ZG」再回测验证）。

**选股输出「次日执行计划」**（日线最精确）：现价(可市价) / **不追上限(=止损/(1−R_MAX)，超过即放弃追高)** /
止损 / 第一止盈 / 仓位，皆确定价位非区间。

**职责分离**：`mainA.py` 只做选股（→ `output/ashare/{date}/`，含 Buy/Watch + 次日执行计划），
`run_ashare_backtest.py` 历史回测（**仅 b1/b2/b3 撮合**，lb2 不交易；按类型 + 牛熊六阶段胜率）。
回测实证（⚠️ 均基于旧提取器，作废待重测）：旧口径六阶段胜率全 >50%、全量 1331 只
~68.7%——该提取器有幸存者偏差（被重画的失败笔从统计中消失，胜率虚高）。
**✅ 2026-07-15 已移植美股 R1.3 修复**：`extract_chan_events_ashare` 改为逐日
as-of 重放（三重发射门：is_fresh 12交易日 + 分型停顿 + A定笔/C'波动率），
验证：低波动名旧事件 100% 同日复现、高波动名延后 2~6 天（C' 门一致），事件数
1.5~3.3 倍（失败信号回归）。**68.7% 与六阶段结论待 `processed_stocks_selected/`
数据放回后重跑 `run_ashare_backtest.py` 重测**；重测前 A股买点分值维持原标定。

**板块涨跌停**（决定仓位上限与回测建模）：300/301→创业板±20%、688/689→科创板±20%、
8/4→北交所±30%、其余主板±10%。代码从文件名提取 6 位（剥离 sh/sz 前后缀）。

**风控（利弗莫尔 2% 法则）**：仓位 = RISK_BUDGET(2%) / R；结构止损距入场 > R_MAX(15%)
→ 降级为 Watch。回测止损 SL_PCT≈9%、TP 2:1、预热 120TD。

**A股口径**：`chan_signal_ashare` 复用 `ChanSignalResult`；CSV 列名小写→大写 OHLCV。
