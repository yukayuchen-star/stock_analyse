---
name: core-holdings-research
description: R9 核心持仓（70% 长持 sleeve：NVDA/AAPL/GOOGL/MSFT/AMZN/META/QQQ）每日深度研究——读官方披露与财务趋势判 thesis、对齐公允价带、给底仓累积/增强层高抛低吸的可执行择时建议与目标价位。产出 output/{date}/核心持仓研究.md；战术侧已跑过时另出 统一操作指引.md（短线×长线合并执行单）。
---

# 核心持仓每日研究（R9.4）

为 70% 核心长持 sleeve 生成**每日决策支持报告**（advisory，用户真金手动执行）。
框架依据 `PRD.md` R9 章节；池与参数读 `config/stocks.py`（`CORE_HOLDINGS` /
`CORE_TARGET_FRAC=0.70` / `BASE_FLOOR_FRAC=0.70`）。

## 流程

### 1. 预取结构化输入

```bash
python main.py            # 战术 sleeve（短线）；已跑过当天可跳过
python core_research.py   # 核心 sleeve（长线）预取
```

**先跑 `main.py` 再跑 `core_research.py`**：前者会落一份 `output/{run_date}/tactical_snapshot.json`
（同一次运行的战术裁决的结构化副本），后者会自动找到它并挂进 `core_inputs.json` 的
`tactical` 块（见 §2c）。没跑也不会报错——`tactical` 块整块缺席、打
`TACTICAL_SNAPSHOT_MISSING`，报告退回纯长线口径并注明。

⚠️ **两个日期不是一回事，别拿目录名当 as-of**：`main.py` 的输出目录是**墙钟日**
（周末跑就是周末的日期），`core_inputs.json` 的 `asof` 是**最后一根有效 K 线日**。
两者差 1~3 天是正常的。真正要对齐的是 `tactical.bar_asof` 与 `asof`
（预取层已代码化为 `asof_aligned`）。

产出 `output/{date}/core_inputs.json`（date = 最新完成交易日）。读取整个 json。
新增三块（2026-08-28）：`portfolio.macro`（VIX 档位，见 §3.8）、
`holdings[t].technical` 的缠论结构位（`pivot`/`b3_ideal_entry`/`stop_loss`/`stroke_confirmed`）、
`holdings[t].consensus.revision_drift`（整季一致预期漂移）。**价位一律取自这些字段，
不得从日志手抄或目测**。
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
  `sub_one_share` 列出摊额**不足一股**的名字。
- **`fractional_shares` 决定怎么执行**（预取层从台账 `build_schedule` 透传）：
  - `true`（本项目 2026-08-28 起为 true，用户已确认券商支持碎股）→ **一律按 `shares_frac` 直接下单**，
    `shares_whole` 与整股两步法一概不用，报告也不必再提「需券商支持碎股」。
    此时 `sub_one_share` **只表示「摊额小」，不表示「执行不了」**，不构成任何提示或例外处理。
  - `false` / 缺席 → 回到整股口径：`sub_one_share` 的名字本月按 0 股处理、余额并入下一顺位
    （**不得**为了凑一股而超投，那会破坏分摊比例）。
  ⚠️ 整股两步法（纯向下取整 → 贪心补齐）**只在 `fractional_shares=false` 时才谈**，
  且必须同时报出它的两个代价：纯取整只投得出约 62~75% 预算；贪心补齐虽把利用率推到 ~98%，
  却会让被凑整的名字超投（实测最坏 +39.5%）——这正是 `proportional` 想避免的加权失真。
- **财报窗口的改投在本 skill 做，不在预取层**：预取层不认识 `next_earnings` 语义。某名 ≤5 个交易日内
  有财报 → 从 `baseline_plan` 摘掉该名，把它的 `alloc_usd` **按剩余各名的 `alloc_usd` 占比**摊回去，
  当月总投入额不变。报告须写明摘了谁、改投给了谁。
- 带 `POLICY:MTD_UNVERIFIABLE` 时，`mtd_invested_usd` 只统计了带 date 的成交，**不得**据此断言
  用户本月没投——报告须写「本月已投无法核算（存量 fill 无日期）」。
- 带 `POLICY:TARGET_SUM_MISMATCH` 时逐名目标之和 ≠ `core_target_usd`，两个口径会各说各话 →
  优先报此冲突并请用户校正，不要挑一个口径自行其是。

### 2c. 战术 sleeve 对账（`tactical` 块，缺席则退回纯长线口径）

`core_inputs.json` 的 `tactical` 块是**同一个交易日**战术侧裁决的结构化副本
（`main.py` 写的 `tactical_snapshot.json`）。它的存在只为一件事：让短线与长线
**在同一份输入上对账**，然后在同一张表里呈现。

#### ⚠️⚠️ 合并输入与呈现，**绝不合并裁决**

两个 sleeve 是**故意反向**的（CLAUDE.md R9.7），把它们平均掉就等于两条都废掉：

| | 战术 sleeve | 核心 sleeve |
|---|---|---|
| 持有期 | 数周 | 数年 |
| 结构 vs 基本面冲突 | 结构优先（0.70×chan） | **thesis 优先**（红旗否决买点） |
| VIX 升高 | **节流**（仓位上限下调、买点门槛收紧） | **加速**（恐慌是加仓窗口） |
| 宏观口径 | 35% `macro_score`（全池价格+桶强度） | VIX 四档（`portfolio.macro`） |
| 资金 | paper 模拟盘 $100k | 真金 `core_ledger.total_capital` |

因此**禁止**：把战术评级当成核心的加减仓依据；把 `macro_score` 代进核心；
造任何跨 sleeve 的合成分；用一侧的结论去改写另一侧。
**允许且必须做**的只有对账 + 并列呈现。

#### 六只核心名在战术侧的评级 = 分析结论，不是下单指令

`main.py` 把 `CORE_HOLDINGS` 排除出战术买入候选（防同名双重敞口），所以
`core_names[t].tactical_tradable` 恒为 `false`。它们的 `rating` / `final_score`
是**照跑出来供核心择时参考的分析**，报告须写明这一点——不得写成
「战术侧给 MSFT Hold，所以核心也观望」。核心的动作只能由 §3.8 三轴裁决给出。
（QQQ 是 benchmark 不是扫描池成员，通常不出现在 `core_names` 里，属正常。）

#### 两个标必须分开读：`tactical_tradable` vs `tactical_buyable`

| 票 | `tactical_tradable`<br>（在不在战术账本里） | `tactical_buyable`<br>（能不能买） | `no_buy_reason` |
|----|----|----|----|
| 六只核心名 | `false` | `false` | `"core-holding"` |
| 强制入池的持仓票 | **`true`** | `false` | `"held-forced-into-pool"` |
| 普通池成员 | `true` | `true` | `null` |

- `"core-holding"` —— paper 账本里压根没有它，买卖两侧都不是战术指令，如上。
- `"held-forced-into-pool"` —— paper 已持仓、但已被扫描池轮出的票。它是**为风控被拉回来的**
  （不入池则结构止损与卖点根本不会被评估），**只分析、不进买入候选**。

`tactical.actionable` 对这两类的处理**不同**：核心名整行不出现；强制入池的持仓票
**卖出行照常出现**（`rating` 为 Sell/Underweight 时），买入行才被挡掉。

对这类票，报告须把两件事分开写：
- ✅ **卖出侧照常生效**：`stop_loss` / 卖点 / 评级转 Sell 都会真的执行，且会出现在
  `tactical.actionable` 里（带 `no_buy_reason="held-forced-into-pool"`）——写进 §C 的持仓行。
- ❌ **买入侧一律不动**：即便 `rating` 是 Buy/Overweight（此时 `risk_flags` 含 `HELD_NO_ADD`），
  也**不得**在 §C 写成加仓建议。评级说的是「这只票现在什么状态」，不是「可以买」。

**为什么必须显式写**：评级说买、账本不买、报告不提，就是一次静默背离——
与「持仓票没有 Signal 所以止损从不被检验」是同一类缺陷，只是方向相反。

#### 四项交叉检查（逐条读，命中即写进报告）

1. **`asof_aligned`** —— `tactical.bar_asof` 是否等于 `asof`。
   `false` ⇒ 打 `TACTICAL_ASOF_MISMATCH`，**两侧价位与信号不可直接并列**，
   报告须写明各自是哪一天的收盘。
2. **`core_names[t].chan_agrees_with_core`** —— 六只核心名的缠论，两条管线各算了一遍。
   两侧**共用同一份价格缓存**（`get_price` 同 key、同 800 天窗口），
   所以正常必须完全一致；出现 `false`（附 `chan_diffs`）⇒ 打 `TACTICAL_CHAN_DISAGREE`
   ⇒ **先查清再引用任一侧的结构位**，不要挑一个看着顺眼的用。
3. **`TACTICAL_BAR_LAGGING` / `PRICE_BAR_OFFSET`** —— 这两条抓的是**对账抓不到的那类错**：
   两侧共用缓存，所以数据缺陷会**同时打中两边**，此时「两侧一致」恰恰是假安慰
   （2026-08-28 的 NaN 尾行事故即如此：MSFT 的 b3 与 META 的 s3 在两边一起消失，
   对账全绿）。命中即说明那些名的价位/末笔不是 as-of 当日的。
4. **`book.positions` ∩ `CORE_HOLDINGS`** —— 命中即 `TACTICAL_CORE_OVERLAP`：
   同名双重敞口，`main.py` 的核心名排除本该防住，须查历史遗留仓。

#### 两本账彼此独立（`book.independent_from_core = true`）

paper 的 `initial_capital` 与真金 `core_ledger.total_capital` **各自 $100k、互不相干**，
**合起来不是一本 70/30 的账**：战术的 `max_exposure_frac=0.30` 是 paper **自身权益**的 30%，
核心的 70% 是真金 `total_capital` 的 70%。报告里两侧仓位**必须分列并标注资金账户**，
**禁止**相加成一个「总仓位」——那个数没有对应任何一笔真实资金。

### 3. 逐核心股研究（QQQ 除外的 6 只）

对每只股票，依次产出**五要素**：

1. **披露摘要**：`filings` 列表（8-K/10-K/10-Q，含日期与 edgarUrl）。对最近 1-2 条
   8-K 与最近一期 10-Q/10-K 尝试 WebFetch 原文 URL；**本网络 sec.gov 大概率不可达**
   （TLS reset，见 json `degraded`）——失败则退而依据 form 类型 + title + 财务趋势判断，
   并标注「未读原文（网络降级）」。绝不编造 filing 内容。
   **判断 filings 来源看 `FILINGS_MIRROR` 标，不看 `EDGAR_UNREACHABLE`**：后者只在真正发起
   可达性探测时才写，filings 命中缓存时探测根本不会被调用（同一天的第二次运行会比第一次少一条）；
   前者从返回行的 `source` 字段直接判定，与缓存状态无关。带 `FILINGS_MIRROR` 即
   **filings 全部/部分来自 yfinance 镜像**，报告须写明「未读原文」。
2. **材料事件评估**：新 8-K 是否 material（高管变动/会计变更/重大合同/治理红旗）。
   thesis 红旗清单：营收/EPS 增速转负、毛利趋势下行（`financials` 年/季序列可算）、
   竞争地位受损迹象、治理/会计异常。
   **刚发过财报的名字看 `consensus`（不必等 `financials` 更新——报表要滞后数周）**：
   `last_report`（actual / estimate / `surprise_pct`）、`surprise_history`（近 8 季，看**趋势**：
   连续扩大的 beat 与逐季收窄的 beat 是两回事）、`next_quarter` 一致预期、`revisions_30d`
   与 `eps_estimate_trend`（分析师修正方向）。
   ⚠️⚠️⚠️ **`surprise_pct` 是混口径量，且混法逐票不同**：`epsEstimate` 恒为街面 non-GAAP，
   而 `epsActual` 有的票给 GAAP、有的给 non-GAAP → 看 `actual_basis` 字段：
   - `non_gaap` ⇒ 与预期同口径，**surprise 可用**；
   - `gaap` + 该票带 `EPS_ONEOFF_INFLATED` ⇒ 打 **`SURPRISE_MIXED_BASIS`**，
     **surprise 是口径假象，绝不可解读为超预期幅度**（实测 GOOGL +214.2% / AMZN +215.0%，
     全部来自 GAAP 里那半数一次性损益撞上 non-GAAP 预期）；
   - `unknown_gaap_quarter_missing` / `unknown` ⇒ 报表还没更新到该季（财报刚发时的常态），
     无法判定口径 ⇒ 预取层打 **`SURPRISE_BASIS_UNKNOWN`**，**surprise 一律不可解读**。
     ⚠️ 别把「没打 `SURPRISE_MIXED_BASIS`」读成「口径没问题」——财报后那几周恰恰是该护栏
     打不出来的盲区（GOOGL/AMZN 的 +200% 假象就诞生在这几周里），所以判不出时必须显式说判不出。
   ⚠️ 带 `CONSENSUS_THIN` 时该名季度 EPS 一致预期覆盖分析师过少（NVDA 仅 4 位），
   **不是真街面共识**，改用营收口径（`revenue_n` 通常 27+）。
   ⚠️ **一致预期不与 GAAP 的 `pe_now` / `eps_ttm` 做任何运算**——两套口径，混用即错。
   📌 **下季指引不在预取层**：指引是新闻稿里的散文（"$108.0 billion, plus or minus 2%"），
   机器端点拿不到。需要时**读官方新闻稿**（`nvidianews.nvidia.com` 实测可达 200；
   `investor.nvidia.com` 403、`sec.gov` TLS reset），再与 `next_quarter.revenue_avg` 相减
   得「指引 vs 一致预期」缺口——**这是财报后裁决的核心数字，不是财报前的预测依据**。
3. **公允价区间**：`valuation.band`（floor=P/E 25 分位 / mid=50 / ceiling=75 /
   extreme=90，trailing 口径）+ PEG 隐含价 + 分析师目标带三角互验。
   ⚠️⚠️⚠️ **先看一次性损益**：`pe_now` 用的是**报告** EPS，可被巨额非经营损益灌水。
   预取层已给出营业利润口径的 `eps_ttm_normalized` / `pe_now_normalized` /
   `oneoff_share` / `normalized_vs_mid`。**`oneoff_share` > 15% 时（带
   `EPS_ONEOFF_INFLATED` 标）一律以 `pe_now_normalized` 与 `normalized_vs_mid` 裁决，
   报告须并列两个口径并说明差异来源**。实测 2026-08-20：GOOGL 一次性占 50.9%
   （净利 $112.19B > 当季毛利 $73.85B），报告 P/E 17.1× → 正常化 34.8×，
   折价 40% **翻转为溢价 21.7%**；AMZN 同为 50.0%，−49.4% 翻转为 +1.2%。
   照报告口径行事会在溢价位补仓。`normalized_basis` 含营业利润/税率/股数可供复核
   （`quarters` = 正常化用的四季，`eps_window_end` = 报告 EPS 窗口末季）。
   ⚠️⚠️ **窗口错配（`NORMALIZATION_WINDOW_MISMATCH`）**：yfinance 各行季度覆盖可以不齐，
   报告 EPS 窗口与营业利润窗口可能错开若干季（AMZN 长期命中：EPS 到 26Q2、营业利润只到 26Q1）。
   命中时 `normalization_window_mismatch` 给出 `op_end` / `eps_end` / `offset_quarters`，
   并给同窗口重算的 `eps_ttm_reported_same_window` / `oneoff_share_same_window`。
   **报告须写明：`oneoff_share` 把「真一次性损益」与「错开那几季的增长」混在一起，
   真实污染度介于 `oneoff_share_same_window` 与 `oneoff_share` 之间**（AMZN 即 25.7%~50.0%）；
   盈利增长期内 `pe_now_normalized` 与 `normalized_vs_mid` **系统性偏高**（显得更贵）。
   **此时该名的估值裁决可靠度最低——两个口径都不可全信，不得据此触发加仓或减仓**，
   退回 PEG（若其增长率同样被灌水则一并弃用）+ 分析师带 + 缠论结构裁决。
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

### 3.7 财报前裁决表（earnings gates）——每名必备

`output/earnings_gates.json` 存每名在财报**之前**写死的可证伪判据；预取层把它挂到
`holdings[t].earnings_gate` 并做完整性校验（`integrity` / `days_to_earnings`）。

**⚠️ 先摆正它是什么**：这**不是股价预测**，也不产生任何 alpha 主张。
「猜财报后涨跌」在本框架内无解——一致预期、修正方向、上季指引全是公开信息，早已在价格里；
NVDA 连续四季 beat 且幅度单调扩大（3.5%→5.3%→5.5%→**6.2%**）本身就说明「猜 beat/miss」是错题，
市场定的是「beat 多少 + 指引多少」，而那两个数只在发布那一刻存在。
裁决表的**唯一**作用是：**防止事后为已发生的走势编理由**。数字出来后人会不自觉地把任何结果
解释成「早就看出来了」，写死的阈值让你没法这么干。它是纪律工具，不是预测工具，
**报告里不得给它安任何胜率/命中率主张**。

**它的价值来源只有一条：`written_at` 早于 `earnings_date`。** 因此：

- **写死后一字不改。** 就算后来觉得阈值定得不好，也只能在**下一季**的表里改，
  当季的表必须原样留着接受检验。META 的表是 2026-08-24 写的，`written_at` 即为该日，
  之后每次报告都**照抄**，不得随行情微调。
- 报告里必须**并列 `written_at` 与 `earnings_date`**，让读者自己核验先后。
- 预取层已把这条检查代码化，命中即打标，**不接受口头承诺**：
  - `GATES_POST_HOC` ⇒ `written_at` 不早于 `earnings_date` ⇒ **该表作废**，
    报告须写明「事后补写，无预测价值」，且**不得据其减仓**。
  - `GATES_STALE` ⇒ 表针对的季度**已经报过**（财报日已过，或下次财报比表上日期晚 45 天以上）
    ⇒ 本次运行须**为新一季写表**（写完 `written_at` 填当日）。
  - `GATES_DATE_DRIFT` ⇒ 财报日**估计值**漂移了几天（yfinance calendar 在公司正式确认前会动），
    但该季**尚未报过** ⇒ **表照旧有效，绝不重写**，只在报告里注一句实际日期以哪个为准。
    ⚠️ 这条与 `GATES_STALE` 的分界是本功能的命门：把几天的日期漂移当成过期去重写，
    `written_at` 就会被推到今天、还带上多看两个月行情后的阈值——**表唯一的价值来源当场被毁**。
    它不计入 `integrity`（仍显示 `ok`），因为它不影响 `written_at` 早于 `earnings_date`。
  - `GATES_MISSING` ⇒ 该名有已知财报日却没有表 ⇒ 本次运行须补写。
    ⚠️ **但 `days_to_earnings <= 0` 时不要为该季补表**——同日或事后写的表会被
    `GATES_POST_HOC` 判作废（代码按日期比，看不到盘中时刻，而美股大盘股盘后发布，
    「当天上午写的」无法自证事前）。这种情况直接为**下一季**写，并在报告里说明本季没有表。
  - `GATES_UNLOCKED` ⇒ `locked≠true`，表可能被事后改过，按作废处理。

**判据一律建在利润表科目上（营收 / 毛利率 / 营业利润率 / vs 一致预期），不建在 `eps surprise` 上**
——六只里四只的 `surprise_pct` 是 GAAP 实际撞 non-GAAP 预期的混口径量（GOOGL +214.2%、AMZN +215.0%
纯属假象，见 §3 要素 2）。只有 `actual_basis == "non_gaap"` 的名字（当前仅 MSFT）可把 surprise 作辅助参考。

**命中后怎么做（写死，避免届时临时发挥）：**

- 🔴 **任意两条命中 ⇒ thesis 破坏坐实 ⇒ 按政策减 1/3**，但 **`sell.max_sellable` 是硬上限**
  （底仓下限锚定 `built_peak_shares`，会先 binding——如 NVDA 20 股减 1/3 = 6 股，卖后恰好等于下限 14）。
  报告须附这条算术，不得只说「减 1/3」。
- 🔴 **只命中一条 ⇒ 不减仓**，写进下一季观察项（**两点连不成线**——与 META 26Q2 利润率塌陷同一把尺子）。
- 🟢 **命中不构成加仓理由。** 加速器只认「估值门 + 缠论买点」，
  **「财报好所以加仓」会把择时从后门放回来**，这一条必须守住。
- 财报窗口纪律不变：≤5TD 时该名从 `baseline_plan` 摘出、摊额按占比摊回其余名。

**下季指引不在预取层**（是新闻稿里的散文），需要时读官方新闻稿——实测可达性：
`nvidianews.nvidia.com` ✅200、`apple.com/newsroom` ✅200、`abc.xyz/investor` ✅200、
`press.aboutamazon.com` ✅200、`news.microsoft.com` ↪301；
`investor.nvidia.com` ❌403、`investor.atmeta.com` ❌403、`sec.gov` ❌TLS reset。
读到指引后与 `consensus.next_quarter.revenue_avg` 相减得「指引 vs 一致预期」缺口
——**这是财报后裁决的核心数字，不是财报前的预测依据**。

#### 指引采集 —— 每次财报后的**强制动作**（2026-08-28 制度化）

`guidance_captured = false` / 带 `GATES_NO_GUIDANCE` ⇒ 该名的裁决表缺**公司自己给的下季指引**。

**为什么这是最该补的一条**：指引是财报前信息量最高的单个数字（市场交易的正是
「beat 多少 + guide 多少」），而且**写表时它就已经可得**——它躺在上一季的新闻稿里。
2026-08-28 审计：六只里只有 NVDA 录了，另五只 7 月底就报过、指引一直在那儿没人取。
**这是流程缺口，不是数据缺口**，所以用流程补。

**动作**（每名每季一次，在该名财报后的第一次运行做）：
1. WebFetch 官方新闻稿——实测可达：`nvidianews.nvidia.com` ✅、`apple.com/newsroom` ✅、
   `abc.xyz/investor` ✅、`press.aboutamazon.com` ✅、`news.microsoft.com` ↪301；
   ❌ `investor.nvidia.com` 403、`investor.atmeta.com` 403、`sec.gov` TLS reset。
2. 取三样写进**下一季** gate 的 `basis`：`guidance_revenue_mid_usd` + `guidance_revenue_band_pct`、
   `guidance_gross_margin`（若给）、`guidance_source`（写明来源与「已读原文」）。
   顺带取**分部数**（NVDA Data Center / AMZN AWS / MSFT 云 / GOOGL Cloud / META RL）
   存进 `latest_quarter_official` —— 这些是 yfinance 合并利润表里**根本没有**的东西，
   而这六只的 thesis 恰恰全在分部上（NVDA 的 `data_center_qoq` 判据就只能靠它）。
3. 与 `consensus.next_quarter.revenue_avg` 相减得「**指引 vs 一致预期**」缺口。
   ⚠️ 财报刚发时一致预期**还没被修正过**，此缺口会偏大；只要每季都在同一时点测量，
   口径就是一致的——但报告须注明这一点，不得把它读成纯粹的「超预期幅度」。
4. ⚠️ **只补 `basis`，不动 `red`/`green`/`written_at`**。若该季的表已写死而当时没录指引，
   **不得**回填后假装当初就有——补录须写进**下一季**的表。

**若新闻稿抓不到**（改版 / 封锁 / 超时）：如实写「指引未取得（原因）」，
判据退回只挂一致预期，并明说少了最有信息量的一条。**绝不凭记忆或推测填指引数字。**

⚠️ **这是整套「深入分析」的单点故障**：指引与分部**只有新闻稿这一条路**
（EDGAR XBRL 本机 TLS 不通、investor 站 403）。它一断，能力直接归零——
报告须在 `degraded` 里如实反映，不得让读者以为分析深度没变。

**报告呈现**：§5 快照后加一张「财报前裁决表状态」总表（票 · 季度 · 财报日 · `days_to_earnings` ·
`written_at` · `integrity` · 🔴/🟢 条数 · `max_sellable`），逐股 §3 的要素 2 里展开该名的具体判据。
`days_to_earnings ≤ 10` 的名字须在快照里显著提示「进入财报窗口」。

### 3.8 三轴综合裁决 —— 买点 / 卖点 / 长持理由

核心 sleeve 的三根轴，各自回答**不同的问题**，任何一根都不能替另一根作答：

| 轴 | 回答的问题 | 数据来源（`core_inputs.json`） |
|---|---|---|
| **结构（缠论）** | **什么价位、什么时候进** | `technical`：`buy_point` / `pivot{ZD,ZG,mid}` / `b3_ideal_entry` / `stop_loss` / `r_ratio` / `stroke_confirmed` |
| **环境（VIX 档）** | **要不要加大力度** | `portfolio.macro`：`vix` / `regime` / `panic_accelerator` |
| **公司潜力** | **该不该持有这家公司** | `financials` 趋势 + `consensus.revision_drift` + `earnings_gate` + `valuation` |

#### ⚠️ 三轴是 AND，不是加权 —— 这一条最要紧

**不得**为核心 sleeve 造任何 `score = w1×结构 + w2×环境 + w3×基本面` 的合成分。
理由有两层，都必须守住：
1. **没有回测背书**。战术侧的 55/35/10 好歹来自（且已被 R1.3 大幅修正的）实证；
   核心侧一个数都没有。造权重等于凭空发明一个精度主张。
2. **加权会让一根轴把另一根买断**。估值便宜买不回一个坏 thesis；thesis 再好也不能让
   高位入场变成好入场。三个问题不同质，**必须各自独立通过**。

所以裁决一律写成「逐条过/不过 + 哪条否决」，**不写合成分**。

#### 🔴 与战术 sleeve **方向相反**的两条，务必分清

| | 战术 sleeve | **核心 sleeve** |
|---|---|---|
| 结构 vs 基本面冲突 | **结构优先**（背离规则 0.70×chan，结构 > 统计） | **thesis 优先**——基本面红旗**否决**缠论买点 |
| VIX 升高 | 节流（仓位上限下调、买点门槛收紧） | **加速**（恐慌是加仓窗口） |

为什么核心侧要反过来：持有期不同。战术持有数周，**入场点错了就是全部损失**；
核心持有数年，**入场差几个点可以被时间摊平，但 thesis 破了就是永久损失**。
把战术的「结构优先」搬进核心，等于用一个几周尺度的规则去管一个几年尺度的仓位。

⚠️ **`macro.chan_buy_allowed` 不适用于核心 sleeve**：那是战术侧 VIX 四档对买点类型的门控
（15–25 档只放行 b1/b2）。核心 sleeve 的 policy 明写扳机接受 **b1/b2/b3**，
且核心遇恐慌要**加速**而非收紧——拿战术的门去卡核心，方向正好反了。
该字段只作参照，**不得用来否决核心的加速器**。

#### 买点（加速器）：三条全过才成立

```
① 公司潜力过门  thesis 无红旗坐实 且 earnings_gate 未 🔴两条
② 估值过门      price ≤ mid（带 EPS_ONEOFF_INFLATED 时用 normalized_vs_mid ≤ 0）
③ 结构过门      buy_point ∈ {b1,b2,b3} 且 stroke_confirmed = true
────────────────────────────────────────────────
任一不过 ⇒ 加速器不成立（写明是哪条否决的，不得含糊成「综合看」）
VIX 是**加码器不是门**：panic_accelerator=true ⇒ tranche 可加大；false 不阻止任何一条
```

**价位怎么给**（全部来自 `technical`，不得手抄日志、不得目测）：
- `b3_ideal_entry` = 理想回踩区 [ZG×0.99, ZG×1.03]，`entry_band` = 实际建议区间。
- `b3_window_passed = true` ⇒ 必须写明「回踩窗口已过、现价高出上沿 `above_ideal_pct`」，
  这是**次优入场**；同时重申「等它跌回 ZG 再买」是错误逻辑（届时结构已变，b3 大概率消失）。
- `stop_loss` / `r_ratio` 只作**结构位置说明**——底仓不设止损，穿越回撤长持。
- ⚠️ `stroke_confirmed = false`（未定笔）⇒ **结构条不过，加速器一律不成立**。
  财报反应日必然落在这里，这正是右端护栏的设计目的：**结构上禁止在反应日交易**。

#### 卖点：底仓只有两个，且都不是结构给的

1. **thesis 破坏坐实**（gate 🔴 两条）⇒ 减 1/3，**但 `sell.max_sellable` 硬封顶**；
2. **`price > extreme`**（P/E > 历史 90 分位）⇒ 减 1/3。

**缠论 s1/s2/s3 对底仓一律无效**（`insight_backtest_exit_faithful`：长牛股 swing<hold 是结构必然）。
卖点只在**增强层**生效，且必须**估值必要 + 结构确认**双条件：
`price ≥ ceiling` **且** s1/s2 顶背驰；卖出股数 ≤ `enhancement_shares` 且不得跌破 `base_floor_shares`。

#### 长持理由：写不出证伪条件，就等于没有理由

每只票的 §3 要素 5 末尾必须给出**四段式长持理由**，缺一段即视为没有 thesis：

| 段 | 内容 | 取自 |
|---|---|---|
| **驱动** | 什么在增长 / 什么在扩张 | 营收 YoY、毛利率或 OM 轨迹、分部（若已录） |
| **证据** | 具体是哪个数字，以及它在往哪个方向走 | `financials` 逐季 + `consensus.revision_drift`（营收/EPS 预期漂移 + 上下修家数） |
| **证伪条件** | **什么情况下我不再持有** | 该名 `earnings_gate` 的 🔴 判据 + 财报日 |
| **可靠度** | 这套判断有多少水分 | 该名的 `degraded` 标（分位不可靠 / 一次性污染 / 窗口错配 / 未读原文 / 无指引） |

⚠️ **证伪条件不许写成「基本面恶化就卖」这类不可检验的话**——必须是 gate 里那种
「营收 < $X」「OM < Y%」的数字判据。**写不出数字，就说明这只票现在只是个仓位，不是个论点**，
报告须直说这句，不得用文字包装过去。

⚠️ **`revision_drift` 的用法**：它只描述**市场预期在往哪走**，不预测财报结果
（预期是公开信息、早在价格里）。带 `DRIFT_THIN` 时只列原始点、**不得判断方向**。
它的价值是给「驱动」段提供一个连续的外部对照：公司叙事在改善而分析师在下修，
两者背离本身就是要写进报告的事实。

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

快照须含**一行宏观**：`macro.vix` / `regime` / `panic_accelerator`
（核心 sleeve 的唯一宏观输入；**不是**战术侧的 35% macro_score，口径区别见 §3.8）。

配了 `policy` 时快照须**多一行本月基线状态**：`monthly_baseline_usd` / `mtd_invested_usd` /
`baseline_remaining_usd` / `baseline_allocation`，并附逐名 `target_usd`·`gap_usd`·`built_frac` 表
与 `baseline_plan` 的逐名摊额表（含碎股/整股与 `sub_one_share` 提示）。汇总表里基线动作与
加速器动作**必须分列标注**（层写「底仓·基线」/「底仓·加速器」），否则读者无法分辨哪笔是无条件
投入、哪笔是扳机触发——这正是本政策要防的混淆。

写入 `output/{date}/核心持仓研究.md`（date 与 core_inputs.json 相同）。

### 6. 统一操作指引（短线 + 长线合成一张表）

`tactical` 块存在时，**除 `核心持仓研究.md` 外另写一份**
`output/{asof}/统一操作指引.md`（asof 与 `core_inputs.json` 相同）。

它解决的问题：`今日操作.md`（短线）与 `核心持仓研究.md`（长线）此前是两份互不引用的
结论，读者要自己在脑子里合并——而两个 sleeve 恰恰对同一个 VIX、同一个缠论信号
**要求相反的动作**，靠人脑合并正是最容易出错的地方。这份文件把两侧摆到一张表上，
**每一行都钉死它属于哪个 sleeve、哪个持有期、哪本账**，合并的是呈现，不是判断。

**结构（五节，顺序固定）**：

- **§A 今日一句话** —— 两侧各一句：短线做什么、长线做什么。若两侧动作看似矛盾
  （如战术在减、核心在加），**必须显式说明这是设计使然并给出理由**（持有期不同），
  不得含糊过去。
- **§B 对账栏** —— 一个小表，四项交叉检查（§2c）逐条报 ✅/⚠️ + 命中详情：
  as-of 对齐 · 缠论一致 · 数据尾行 · 账户重叠。任一 ⚠️ 时，本文件后续所有并列
  比较都须带上这个限定，不得只在这里提一句就当没发生。
- **§C 短线（战术 sleeve · paper $100k）** —— **原样转录**
  `tactical.actionable` 与 `tactical.book`，不重新解释、不加自己的判断、不改评级。
  表列：票 · 评级 · final_score · 现价 · 入场区间 · 止损 · 止盈 · 建议仓位 · 风控标。
  下附 paper 持仓与 `max_exposure_frac`（注明「占 paper 自身权益」）。
  ⚠️ `tactical_buyable=false` 的票不出现在**加仓建议**里——六只核心名（`no_buy_reason=
  "core-holding"`）整行不出现；强制入池的持仓票（`"held-forced-into-pool"`）出现在
  **持仓与止损行**，若其在 `actionable` 里带 Sell/Underweight 则**必须原样写出那条离场**，
  但评级即便是 Buy 也不得写成加仓（见 §2c）。
- **§D 长线（核心 sleeve · 真金）** —— 逐名一行的三轴裁决结论（§3.8），
  层写「底仓·基线」/「底仓·加速器」/「增强层」，附 `baseline_plan` 的摊额与股数。
  每名后括注它在战术侧的评级 + 一句 **「（仅供参照，核心动作不由此决定）」**。
- **§E 合并仓位视图** —— **两本账分列，绝不相加**：
  | sleeve | 资金账户 | 已投/权益 | 目标 | 今日净动作 |
  表下必须有一句：**这两行不可相加**，paper 与真金是两笔独立的钱，
  30%/70% 各自对自己的本金而言。

**写作纪律**：
- 这份文件**不产生任何新结论**。每一个数字都必须能追回 `tactical_snapshot.json` 或
  `core_inputs.json` 的具体字段；短线一侧照抄，长线一侧引用 `核心持仓研究.md` 的裁决。
  若发现两侧结论有冲突，**报告冲突本身**（并注明这是设计使然还是数据问题），
  **不做仲裁、不取平均、不造合成评级**。
- 篇幅控制在一屏可读——深度分析留在 `核心持仓研究.md`，这份是执行单。
- `tactical` 块缺席时**不写这份文件**，只在 `核心持仓研究.md` 顶部注明
  「战术侧未运行（`TACTICAL_SNAPSHOT_MISSING`），本次仅长线口径」。

## 纪律（不可违背）

- **advisory only**：不改任何引擎代码、不碰 paper 组合、不改缠论 55% 本体。
- **诚实**：数据缺失/未读原文/样本不足一律如实标注；本框架**无回测背书**，价值在
  结构化纪律；不用「胜率/统计验证」类大词。
- **可回溯**：每条建议注明触发依据（哪条 filing / 哪个指标 / 哪个价位关系），非黑箱。
- swing<hold 教训（memory: insight_backtest_exit_faithful）：底仓绝不做波段；波段仅限
  增强层且底仓下限硬约束。
- **跨 sleeve 只对账不仲裁**：两个 sleeve 对 VIX 与「结构 vs 基本面」的方向**故意相反**，
  合并它们的裁决等于两条都废掉。可以并列呈现、可以互为参照、可以报告冲突，
  **不可加权、不可取平均、不可让一侧改写另一侧、不可把两本账的仓位相加**。
