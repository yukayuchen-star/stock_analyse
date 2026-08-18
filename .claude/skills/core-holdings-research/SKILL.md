---
name: core-holdings-research
description: R9 核心持仓（70% 长持 sleeve：NVDA/AAPL/GOOGL/MSFT/AMZN/META/QQQ）每日深度研究——读官方披露与财务趋势判 thesis、对齐公允价带、给底仓累积/增强层高抛低吸的可执行择时建议与目标价位。产出 output/{date}/核心持仓研究.md。
---

# 核心持仓每日研究（R9.4）

为 70% 核心长持 sleeve 生成**每日决策支持报告**（advisory，用户真金手动执行）。
框架依据 `PRD.md` R9 章节；池与参数读 `config/stocks.py`（`CORE_HOLDINGS` /
`CORE_TARGET_FRAC=0.70` / `BASE_FLOOR_FRAC=0.70`）。

## 流程

### 1. 预取结构化输入

```bash
python core_research.py
```

产出 `output/{date}/core_inputs.json`（date = 最新完成交易日）。读取整个 json。
若报错或产物缺失，如实报告，不臆造数据。

### 2. 台账检查（一切成本类结论的锚）

`output/core_ledger.json` 是用户维护的真金台账。若 `portfolio.ledger_filled == false`：
在报告顶部显著提示用户补录真实成交（每笔 date/price/shares/layer），并跳过所有
成本类结论（平均成本、折溢价 vs 成本、建仓进度、增强层配对）——**无台账不给成本建议**。
用户在对话中报成交时，帮其追加进台账 json（layer: `base`=底仓 / `enh`=增强层）。

### 3. 逐核心股研究（QQQ 除外的 6 只）

对每只股票，依次产出**五要素**：

1. **披露摘要**：`filings` 列表（8-K/10-K/10-Q，含日期与 edgarUrl）。对最近 1-2 条
   8-K 与最近一期 10-Q/10-K 尝试 WebFetch 原文 URL；**本网络 sec.gov 大概率不可达**
   （TLS reset，见 json `degraded`）——失败则退而依据 form 类型 + title + 财务趋势判断，
   并标注「未读原文（网络降级）」。绝不编造 filing 内容。
2. **材料事件评估**：新 8-K 是否 material（高管变动/会计变更/重大合同/治理红旗）。
   thesis 红旗清单：营收/EPS 增速转负、毛利趋势下行（`financials` 年/季序列可算）、
   竞争地位受损迹象、治理/会计异常。
3. **公允价区间**：`valuation.band`（floor=P/E 25 分位 / mid=50 / ceiling=75 /
   extreme=90，trailing 口径）+ PEG 隐含价 + 分析师目标带三角互验。
   ⚠️ **估值压缩陷阱**：`pe_percentile_now < 10` 时须区分「真低估」vs「成长股结构性
   P/E 压缩」（EPS 高增长追上价格属正常成熟化，历史分位带会系统性偏高）——用 PEG
   隐含价与分析师带交叉裁决，写明判断理由。
4. **当前折溢价**：`premium_vs_mid`、`pe_percentile_now`、（有台账时）现价 vs 平均成本。
5. **今日择时建议 + 目标价位**（分两层，给到具体价位与股数）：
   - **底仓累积**（服务 20%→70% 补满 + 摊低成本）：估值门 `price ≤ mid` 才建议加仓
     （`≤ floor` 加大 tranche）；缠论 b1/b2/b3 或回踩 200DMA 为择时扳机（`technical`）；
     VIX≥25 恐慌回撤 = 加速累积窗口（Core 与 Tactical 对 VIX 方向相反）。
     **财报窗口**：`next_earnings` 距今 ≤5 个交易日 → tranche 建议减半或推迟，注明。
   - **底仓减仓（仅两触发）**：thesis 破坏（要素 2 红旗坐实）→ 减 1/3 或退出；
     `price > extreme`（P/E > 历史 90 分位）→ 减 1/3。否则底仓穿越回撤长持。
   - **增强层高抛低吸**（仅在该名已有持仓且台账可算层量时）：
     高抛 = `price ≥ ceiling 区` **且** 缠论 s1/s2 顶背驰确认（估值必要 + 结构确认）；
     卖出股数 ≤ `ledger.enhancement_shares`（**不得跌破 `base_floor_shares`**，报告须
     附此校验）；低吸回补 = 回落 `≤ mid` / b2/b3 / 高 VIX（与底仓累积同一套触发）；
     配对纪律：回补价 ≥ 高抛价即建议放弃本轮（记 abandoned），**禁止追高回补**；
     报告 `open_enhancement_rounds` 状态与累计节省。

### 4. QQQ（指数特例）

无公司 filings、无 thesis 减仓。仅估值带择时：`core_weighted_premium`（成分折溢价）
+ `dev_200dma_percentile` + 代理带。建议口径同底仓累积/极端高估 trim。

### 5. 组合层汇总 + 落盘

报告开头给组合快照：Core 目标 70% / 已建比例（`portfolio.core_built_frac`）/ 剩余
额度 / 本日各名建议汇总表（票·层·动作·价位·股数）/ `degraded` 数据降级清单。

写入 `output/{date}/核心持仓研究.md`（date 与 core_inputs.json 相同）。

## 纪律（不可违背）

- **advisory only**：不改任何引擎代码、不碰 paper 组合、不改缠论 55% 本体。
- **诚实**：数据缺失/未读原文/样本不足一律如实标注；本框架**无回测背书**，价值在
  结构化纪律；不用「胜率/统计验证」类大词。
- **可回溯**：每条建议注明触发依据（哪条 filing / 哪个指标 / 哪个价位关系），非黑箱。
- swing<hold 教训（memory: insight_backtest_exit_faithful）：底仓绝不做波段；波段仅限
  增强层且底仓下限硬约束。
