# 美股量化核心策略

系统目标：

> **寻找基本面优秀且具成长潜力的美股，利用量化方法优化买卖点。**

适用于：

**免费数据 + T-1收盘数据 + 日频策略 + 后续缠论叠加。**

---

# Layer1：Fundamental Alpha（长期筛选）

解决：

> **买什么。**

建立 **Fundamental Score**。

核心维度仅保留最有效因子。

### Growth（成长）

关注：

* Revenue Growth
* EPS Growth
* FCF Growth

寻找：

持续成长企业。

---

### Quality（质量）

关注：

* ROE / ROIC
* Gross Margin
* Operating Margin
* FCF Quality

寻找：

盈利能力强、商业模式健康公司。

---

### Financial Safety（安全）

关注：

* Debt Level
* Cash Position
* Interest Coverage

避免：

高杠杆风险股。

---

### Valuation（估值）

采用成长适配估值。

例如：

* PEG
* EV/Sales
* EV/EBITDA

避免单纯低PE逻辑。

---

输出：

**Fundamental Score**

用于：

长期观察池筛选。

---

# Layer2：Trend Alpha（趋势过滤）

解决：

> **好公司是否值得当前参与。**

趋势层不负责买点。

只负责：

**允许做多 / 禁止做多。**

核心指标：

### Long Trend

* 200DMA
* 120DMA

识别长期趋势。

---

### Mid Trend

* 20D momentum
* 60D momentum
* Relative Strength vs SPY

识别中期强度。

---

### Trend Strength

* ADX
* Trend slope
* Breakout strength

确认趋势质量。

---

输出：

**Trend State**

状态：

* Strong Uptrend
* Neutral
* Weak
* Breakdown

---

# Layer3：Buy Signal（核心买点层）

这是未来与缠论结合的重点。

建议仅保留三类高价值信号。

---

## A. Pullback Buy（核心）

优先级最高。

逻辑：

> **好公司 + 强趋势 + 回调买入。**

条件：

* Fundamental强
* Trend向上
* 中期回调
* 接近支撑区域

适合作为：

**缠论二买/三买融合入口。**

---

## B. Relative Strength Entry

逻辑：

> 市场弱，但目标股更强。

关注：

Relative Strength vs SPY。

寻找：

逆势强势股。

适合：

机构风格成长股。

---

## C. Breakout Entry

逻辑：

突破确认。

观察：

* 52w High
* Consolidation breakout
* Volume confirmation

适合作为：

趋势延续买点。

未来可对应：

缠论突破确认。

---

输出：

**Buy Score**

---

# Layer4：Macro Regime（制度过滤）

解决：

> 当前市场环境是否允许激进做多。

核心输入：

* VIX
* SPY Trend
* Yield
* Market Breadth

输出：

* Bull
* Neutral
* Risk-Off

影响：

* 仓位
* 风险预算
* 信号阈值

---

# Layer5：Risk Overlay（风险覆盖）

独立模块。


核心：

### Position Sizing

基于：

* Volatility
* Trend Strength
* Conviction Score

动态仓位。

---

### Drawdown Guard

回撤保护。

---

### Exposure Control

市场恶化时自动降仓。

---

### ATR / Structure Stop

用于退出。


---

关键：

> **先用基本面找到未来赢家，用趋势确认方向，用量化信号寻找回调/突破买点，最终由缠论完成精细择时。**


