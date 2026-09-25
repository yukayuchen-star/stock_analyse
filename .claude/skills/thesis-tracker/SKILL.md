---
name: thesis-tracker
description: 核心 sleeve 六只单票（NVDA/AAPL/GOOGL/MSFT/AMZN/META）的长持论点台账——把每日重写的四段式长持理由沉淀成跨日可追溯的 append-only 记录：支柱计分卡（只认财报裁决表里的数字判据）+ 带日期与来源的数据点日志。触发：「更新 X 的论点」「论点还成立吗」「thesis check」「记一条 X 的数据点」「复盘持仓论点」，以及 core-holdings-research 在财报后或出现新数据点时调用。
---

# 论点台账（thesis-tracker，项目改编版）

> 改编自 [anthropics/financial-services](https://github.com/anthropics/financial-services)
> `plugins/vertical-plugins/equity-research/skills/thesis-tracker`（Apache License 2.0，
> http://www.apache.org/licenses/LICENSE-2.0），**已按本项目纪律修改**，差异见下表。

## 为什么需要它

`core-holdings-research` 每天重写一遍四段式长持理由（驱动 / 证据 / 证伪条件 / 可靠度），
**写完就丢**——第二天从零再写。于是跨日才看得出的东西没人记得：
META「股价涨、EPS 一致预期降」的背离从 9/21 延续到 9/24 且在扩大，
这件事只存在于两份散文报告的字里行间。
本台账只做一件事：**让论点有记忆，且记忆不可事后修改**。

## 与原版的冲突与取舍（改编的全部理由）

| 原版 | 本项目 | 理由 |
|---|---|---|
| Target price / valuation | **删除** | 估值只有 `valuation.band` 一个来源，且带 `PE_PCTL_UNRELIABLE`；目标价是精度主张 |
| Conviction High/Medium/Low | **改为「可靠度」** = 该名的 `degraded` 标 | 信心等级是无回测背书的主观分；可靠度是可核验的数据缺陷清单 |
| Action: Increase position / Trim / Exit | **只允许规则产出的动作** | 🟢 命中不构成加仓理由（skill §3.7）；减仓只来自 gate 🔴两条或 `price>extreme` |
| Stop-loss trigger | **删除** | 核心底仓**不设止损**，穿越回撤长持；证伪条件≠止损价 |
| Key pillars（文字） | **支柱 = gate 里的数字判据** | 写不出数字的支柱不是支柱（skill §3.8「写不出数字就只是个仓位」） |
| 可随时更新论点 | **append-only**，历史条目一字不改 | 与财报裁决表同构：价值来源是「写在事前」 |
| Morning meeting / Word doc 输出 | markdown，存 `output/thesis/` | 单人真金台账，gitignored |

## 存储

`output/thesis/{TICKER}.md`（`output/` 已 gitignore —— 远端仓库公开，**不得挪到被跟踪的目录**）。
每个文件三节，顺序固定：

```markdown
# {TICKER} 论点台账

## 一、论点（按季度版本化）
### v{N} · {季度} · 写于 {YYYY-MM-DD}（财报日 {earnings_date}）
- 驱动：……
- 证伪条件：逐条抄 earnings_gates.json 该季 red 判据（id + 指标 + 阈值），不改写
- 🔴 条数：N（< 3 条时标「⚠️ 与门容错低」；若估计不存在两条能同时现实命中 ⇒ 标「惰性」）

## 二、支柱计分卡（最新一季）
| 支柱(gate id) | 阈值 | 上季实际 | 当前外部读数 | 方向 |

## 三、数据点日志（append-only，最新在下）
| 日期 | 数据点 | 来源 | 触及支柱 | 影响 | 规则产出的动作 |
```

## 流程

### 1. 读输入（只读）
- `output/{asof}/core_inputs.json` 的 `holdings[t]`：`earnings_gate`（red/green/written_at/earnings_date）、
  `financials`、`consensus.revision_drift`、`valuation`、`degraded`。
- 已有的 `output/thesis/{t}.md`（没有则进入步骤 2 建档）。

### 2. 建档 / 换季（每名每季一次）
- **建档或 gate 换季**（`earnings_gate.quarter` 与台账最新版本不同）⇒ 在第一节**追加**新版本 `v{N+1}`，
  **旧版本原样保留**。证伪条件逐条**抄**当季 gate 的 red，不得意译、不得增删。
- 🔴 < 3 条时，按 `insight_meta_gate_inert` 的检查写一行：逐条估命中概率，
  「是否存在两条能同时现实命中」—— 否 ⇒ 标**惰性**，并直说「该名当前是一个仓位，不是一个可证伪的论点」。

### 3. 追加数据点（只在**有新信息**时，不是每天）
一条数据点必须同时满足：**有日期 · 有来源 · 能指向至少一条支柱**。
- ✅ 该记：财报实际值（对 gate 逐条判）、指引、分部数、`revision_drift` 方向变化或修正家数跳变、
  material 8-K（**注明「未读原文」若只有镜像元数据**）。
- ❌ 不记：**股价涨跌本身**（价格不是支柱；META +11% 不是论点证据）、分析师目标价、
  媒体观点、缠论买卖点（那是结构轴，不是潜力轴）。
- 「影响」只写 `强化 / 削弱 / 中性 / 无法判断`，针对**具体某条支柱**；
  反面证据与正面证据**同等篇幅**记录。
- 「规则产出的动作」只能是：`无动作` ／ `🔴 X 条命中 → 按 gate 减 ≤ max_sellable 股`（附当日重算算术）／
  `price > extreme → 减 1/3`。**不得**写「加仓」「提高信心」。

### 4. 财报日（强制）
该名财报后第一次运行：对当季 gate 的每条 red / green **逐条**写命中与否 + 实际值 + 来源，
作为一条日志；再按 skill `core-holdings-research` §3.7 的指引采集规则补下一季。
**命中结论照 gate 走，本台账不另做裁决。**

### 5. 呈现
被 `core-holdings-research` 调用时，把「支柱计分卡 + 最近 3 条日志」引用进报告的四段式长持理由；
**报告里的「证据」段应引用台账日志的日期**，使跨日背离可追溯。

## 纪律

- **append-only**：日志与旧版本论点一字不改。写错了追加一条更正，注明更正哪一条。
- 仅六只单票。QQQ 无 thesis；战术 sleeve 的票持有期数周、结构优先，**不建论点档**。
- 无回测背书：不写胜率、命中率、「论点正确率」。
- 真金数据（股数、成本）**不写进本台账**——那归 `core_ledger.json`；本台账只记论点。
