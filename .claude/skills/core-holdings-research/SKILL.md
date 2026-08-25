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
用户在对话中报成交时，帮其追加进台账 json（layer: `base`=底仓 / `enh`=增强层），
**务必带上 `date`**——无 date 的 fill 无法归月，会让 `mtd_invested_usd` 漏计、基线永远显示未完成。

### 2b. 建仓政策（`portfolio.policy`，缺席则退回旧口径）

台账 `policy` 是**用户的配置意图，不是回测结论**；预取层只做算术，判断在本 skill。缺 `policy`
键时整块缺席 → 退回「估值门为入场闸门」的旧口径，并在报告注明政策未配置。存在时：
- `per_name[t].target_usd / gap_usd / built_frac`：逐名目标与缺口（成本口径），同时并入各名 `ledger`。
- `largest_gaps`：缺口最大的三只（纯算术排序，供快照参考）。
- `monthly_baseline_usd` / `mtd_invested_usd` / `baseline_remaining_usd` / `baseline_met`：当月基线执行情况。
- **`baseline_plan`：当月基线的逐名分摊，直接照它下单**，不要自己另算一套。
  `baseline_allocation` 决定分摊方式：`proportional` 按**当前**缺口占比摊给全部有缺口的名字
  （缺口每月重算，补满的名字自动退出，自我校正）；`largest_gap` 全额给缺口最大的一只。
  每名给 `alloc_usd` / `price` / `shares_frac`（碎股）/ `shares_whole`（整股向下取整）。
  `sub_one_share` 列出摊额**不足一股**的名字——报告须提示需券商支持碎股，否则该名本月按 0 股处理、
  余额并入下一顺位（**不得**为了凑一股而超投，那会破坏分摊比例）。
- **财报窗口的改投在本 skill 做，不在预取层**：预取层不认识 `next_earnings` 语义。某名 ≤5 个交易日内
  有财报 → 从 `baseline_plan` 摘掉该名，把它的 `alloc_usd` **按剩余各名的 `alloc_usd` 占比**摊回去，
  当月总投入额不变。报告须写明摘了谁、改投给了谁。
- 带 `POLICY:MTD_UNVERIFIABLE` 时，`mtd_invested_usd` 只统计了带 date 的成交，**不得**据此断言
  用户本月没投——报告须写「本月已投无法核算（存量 fill 无日期）」。
- 带 `POLICY:TARGET_SUM_MISMATCH` 时逐名目标之和 ≠ `core_target_usd`，两个口径会各说各话 →
  优先报此冲突并请用户校正，不要挑一个口径自行其是。

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
   - **底仓累积**（服务补满 `target_usd` + 摊低成本）。**配了 `policy` 时用「基线 + 加速器」，
     估值门与缠论扳机不再是入场闸门**：
     - **基线（无条件）**：当月须投满 `monthly_baseline_usd`，不受估值门/结构扳机约束。
       理由：Core 的职责是**持有**不是择时；拿现金等扳机本身就是一个无证据的择时下注，
       而本框架无回测背书，不该让它去阻断长期配置（`policy.build_schedule.rule` 记有原义）。
       基线资金**按 `baseline_plan` 逐名分摊执行**（分摊方式见 §2b，勿自行改口径）。
     - **加速器（有条件）**：估值门 `price ≤ mid`（带 `EPS_ONEOFF_INFLATED` 时用
       `normalized_vs_mid`）**且** 缠论 b1/b2/b3 或回踩 200DMA 或 VIX≥25 恐慌回撤 →
       在基线之上**额外**加一个 `trigger_extra_tranche_usd`（`≤ floor` 可再加大）。
     - **不重复投**：当月已因加速器投入 ≥ 基线（`baseline_met=true`）则基线视为已满足；
       未用完的基线**不累积**到下月（否则又变相回到攒钱等抄底）。
     - **财报窗口**：`next_earnings` 距今 ≤5 个交易日 → 该名**推迟**，其 `alloc_usd` 按 §2b 的规则
       摊回给其余各名，**不是**削减当月总投入额——基线是无条件的，只调分配对象。
     - 未配 `policy` 时才回到旧口径：估值门 `price ≤ mid` 才建议加仓（`≤ floor` 加大 tranche），
       缠论 b1/b2/b3 或回踩 200DMA 为扳机，VIX≥25 为加速窗口。
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
QQQ 无 thesis 风险、无一次性损益问题、无估值分位样本问题，是**基线资金最省判断的载体**
——`policy` 里它的 `target_usd` 通常最大正是这个理由（`weighting_rationale` 记有原义）；
它在 `baseline_plan` 里的摊额通常也最大，但那只是缺口占比的算术结果，不构成额外看多；
但报告须同时提示它与六只单票高度重叠（占其约四成），是集中押注的放大器而非分散化工具。

### 5. 组合层汇总 + 落盘

报告开头给组合快照：Core 目标 70%（`core_target_usd`）/ 已建比例（成本完整时用
`core_built_frac` + `capital_remaining_usd`，成本待补时用 `core_weight_frac` 并注明配置口径）/ 剩余
额度 / 本日各名建议汇总表（票·层·动作·价位·股数）/ `degraded` 数据降级清单。

配了 `policy` 时快照须**多一行本月基线状态**：`monthly_baseline_usd` / `mtd_invested_usd` /
`baseline_remaining_usd` / `baseline_allocation`，并附逐名 `target_usd`·`gap_usd`·`built_frac` 表
与 `baseline_plan` 的逐名摊额表（含碎股/整股与 `sub_one_share` 提示）。汇总表里基线动作与
加速器动作**必须分列标注**（层写「底仓·基线」/「底仓·加速器」），否则读者无法分辨哪笔是无条件
投入、哪笔是扳机触发——这正是本政策要防的混淆。

写入 `output/{date}/核心持仓研究.md`（date 与 core_inputs.json 相同）。

## 纪律（不可违背）

- **advisory only**：不改任何引擎代码、不碰 paper 组合、不改缠论 55% 本体。
- **诚实**：数据缺失/未读原文/样本不足一律如实标注；本框架**无回测背书**，价值在
  结构化纪律；不用「胜率/统计验证」类大词。
- **可回溯**：每条建议注明触发依据（哪条 filing / 哪个指标 / 哪个价位关系），非黑箱。
- swing<hold 教训（memory: insight_backtest_exit_faithful）：底仓绝不做波段；波段仅限
  增强层且底仓下限硬约束。
