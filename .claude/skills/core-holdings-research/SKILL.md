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

`output/core_ledger.json` 是用户维护的真金台账。两级检查：
- `portfolio.ledger_filled == false`（一笔都没填）→ 顶部显著提示补录，跳过全部成本类结论。
- `portfolio.cost_basis_complete == false`（填了股数但 `price: null`，`cost_pending_shares` 报待补股数）
  → 股数/市值/底仓下限照常，进度只报**配置口径** `core_weight_frac`（市值占目标比例），但**平均成本、
  浮盈、vs 成本折溢价、增强层配对节省、`capital_remaining_usd` 一律跳过**并注明「成本待补」。

两个进度口径不可混用，报告须写明用的是哪个：`core_built_frac`=已投入**资金**占目标比例
（决定还要投多少钱，配 `capital_remaining_usd`）；`core_weight_frac`=当前**市值**占目标比例
（配置占位，持仓上涨会推高它但并未多投一分钱）。

成本有两个口径，报告须并列：`avg_cost`（会计口径，卖出不影响）与 `effective_avg_cost`
（= (invested − realized_pnl)/shares，已实现盈亏冲抵后的实际成本）。**判断增强层高抛低吸有没有
真正「降低成本」只看后者**——一轮做对它下降、追高回补它上升。

记录成交：买入 `shares` 正数、卖出负数，按时间顺序追加。均价用移动加权成本法（卖出不改 avg_cost），
`base_floor_shares` 锚定 `built_peak_shares`（历史最高已建股数）而非当前股数——**不得**按当前股数
重算底仓下限，否则每轮高抛都会把下限再降一档、底仓被合法蚕食。
**无成本不给成本建议**——不得用现价、区间中值或任何猜测值代替真实成本。
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
   ⚠️⚠️⚠️ **先看一次性损益**：`pe_now` 用的是**报告** EPS，可被巨额非经营损益灌水。
   预取层已给出营业利润口径的 `eps_ttm_normalized` / `pe_now_normalized` /
   `oneoff_share` / `normalized_vs_mid`。**`oneoff_share` > 15% 时（带
   `EPS_ONEOFF_INFLATED` 标）一律以 `pe_now_normalized` 与 `normalized_vs_mid` 裁决，
   报告须并列两个口径并说明差异来源**。实测 2026-08-20：GOOGL 一次性占 50.9%
   （净利 $112.19B > 当季毛利 $73.85B），报告 P/E 17.1× → 正常化 34.8×，
   折价 40% **翻转为溢价 21.7%**；AMZN 同为 50.0%，−49.4% 翻转为 +1.2%。
   照报告口径行事会在溢价位补仓。`normalized_basis` 含营业利润/税率/股数可供复核。
   ⚠️ **估值压缩陷阱**：`pe_percentile_now < 10` 时须区分「真低估」vs「成长股结构性
   P/E 压缩」（EPS 高增长追上价格属正常成熟化，历史分位带会系统性偏高）——用 PEG
   隐含价与分析师带交叉裁决，写明判断理由。
   ⚠️⚠️ **分位本身的样本极薄**：`pe_window_days` 是天数，不是自由度；真实自由度是
   `eps_points`（yfinance 只回 5 季 + 4 年 ≈ **6 个 TTM 观测点**）。带 `PE_PCTL_UNRELIABLE`
   降级标时（观测 <8 点或窗口内 EPS 增长 >2×），**`band`/`premium_vs_mid` 不得作为
   减仓或加仓的独立依据**，只能当作一个弱信号，由 PEG 隐含价 + 分析师带 + 缠论结构裁决；
   报告须直说「估值带样本不足，本次以交叉验证为准」，不得把 −40% 折价读成「便宜四成」。
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

无公司 filings、无 thesis 减仓。仅估值带择时：`core_weighted_premium`（成分折溢价，
已优先用成分的 `normalized_vs_mid`，`core_premium_normalized_n` 报有几只走了正常化口径）
+ `dev_200dma_percentile` + 代理带。建议口径同底仓累积/极端高估 trim。

### 5. 组合层汇总 + 落盘

报告开头给组合快照：Core 目标 70%（`core_target_usd`）/ 已建比例（成本完整时用
`core_built_frac` + `capital_remaining_usd`，成本待补时用 `core_weight_frac` 并注明配置口径）/ 剩余
额度 / 本日各名建议汇总表（票·层·动作·价位·股数）/ `degraded` 数据降级清单。

写入 `output/{date}/核心持仓研究.md`（date 与 core_inputs.json 相同）。

## 纪律（不可违背）

- **advisory only**：不改任何引擎代码、不碰 paper 组合、不改缠论 55% 本体。
- **诚实**：数据缺失/未读原文/样本不足一律如实标注；本框架**无回测背书**，价值在
  结构化纪律；不用「胜率/统计验证」类大词。
- **可回溯**：每条建议注明触发依据（哪条 filing / 哪个指标 / 哪个价位关系），非黑箱。
- swing<hold 教训（memory: insight_backtest_exit_faithful）：底仓绝不做波段；波段仅限
  增强层且底仓下限硬约束。
