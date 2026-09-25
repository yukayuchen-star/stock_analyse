---
name: catalyst-calendar
description: 覆盖核心七名 + paper 持仓 + 战术前五的事件日历（财报 / 宏观数据 / 公司事件），用于风险知情而非事前押注——标出哪些持仓的结构止损会被事件跳空越过、哪些核心名进入 ≤5TD 财报窗口。触发：「催化剂日历」「接下来有什么事件」「财报日历」「catalyst」「这周要注意什么」；建议每周一运行一次。
---

# 事件日历（catalyst-calendar，项目改编版）

> 改编自 [anthropics/financial-services](https://github.com/anthropics/financial-services)
> `plugins/vertical-plugins/equity-research/skills/catalyst-calendar`（Apache License 2.0，
> http://www.apache.org/licenses/LICENSE-2.0），**已按本项目纪律修改**，差异见下表。

## 为什么需要它

战术 sleeve 的止损是**收盘价结构止损**，财报隔夜跳空可以直接越过它；而 2026-09-25 之前
战术管线**完全不看财报日**（已补 `EARNINGS_SOON` 旗标，见 `main.py:_annotate_earnings`）。
旗标只管单只票的 10TD 窗口；本日历给的是**跨名、跨 sleeve 的整体视图**，外加旗标看不到的宏观与公司事件。

## 与原版的冲突与取舍

| 原版 | 本项目 | 理由 |
|---|---|---|
| Our Positioning（Long/Short） / pre-positioning recommended | **删除** | 事前押注事件 = 猜 beat/miss，是本框架明确不做的错题（skill §3.7） |
| Consensus vs **our estimate** | 只列一致预期 | 我们没有、也不造自己的盈利预测 |
| Impact H/M/L（主观） | **改为可核验列**：所属 sleeve · 距今 TD · 结构止损距离 | 主观影响分无背书；止损距离是可复现的数 |
| 数据源 Bloomberg / FactSet / IR | yfinance calendar + 官方页面**当日抓取** | 付费源不可得；**日期凭记忆填 = 编造** |
| Excel / Google Calendar 输出 | `output/{date}/事件日历.md` | 单人项目 |
| Archive outcomes → pattern recognition | 保留归档，**不做模式主张** | 一次性事件样本 n 极小，「规律」即挑数据 |

## 覆盖范围（每次运行现算，不写死）

1. 核心七名：`config/stocks.py:CORE_HOLDINGS` + QQQ
2. paper 持仓：`output/us_portfolio.json` 的 `positions`
3. 战术前五：最新 `output/{run_date}/tactical_snapshot.json` 按 `final_score` 排序、排除核心名与 benchmark

## 流程

### 1. 财报日（机器源）
- 战术侧直接读 `tactical_snapshot.json` 的 `next_earnings` / `days_to_earnings`（`main.py` 已填，yfinance calendar **估计日**）。
- 核心侧读 `core_inputs.json` 的 `holdings[t].next_earnings`，并与 `earnings_gate.earnings_date` 对照：
  不一致只注明「以哪个为准」，**绝不因日期漂移重写裁决表**（`GATES_DATE_DRIFT` ≠ `GATES_STALE`）。
- `days_to_earnings < 0` = calendar 未更新（如 WDAY 曾返回已过的 8/27）⇒ 写「未取得」，不推算。

### 2. 宏观事件（必须当日抓官方源）
FOMC 议息、CPI、非农：**每次运行时**从官方日程页抓取（federalreserve.gov FOMC calendar、bls.gov release schedule），
表里附来源 URL。抓不到 ⇒ 该行写「未取得（原因）」。**不得凭记忆填日期**——
一个看着正常的错日期比空格危险（CLAUDE.md「取不到的数如实留空」）。
⚠️ **实测（2026-09-25）**：`www.federalreserve.gov` 与 `www.bls.gov` 本机 **TLS 握手被重置**
（WebFetch 与 curl 均失败，与 sec.gov 同类硬约束）⇒ 宏观行目前**恒为「未取得」**。
这是一条已知的单点缺口，报告须如实写，**不得**拿记忆里的 FOMC/CPI 日期补上。

### 3. 公司事件（可选，须有来源）
产品发布、开发者大会、监管裁决等：只收**有具体来源链接**的；来源为媒体/搜索结果时标「外部未核验」。

### 4. 生成日历表（未来 4 周，按日期升序）

| 日期 | 距今TD | 事件 | 票 | sleeve · 账户 | 与仓位的关系 | 来源 |
|---|---|---|---|---|---|---|

「与仓位的关系」只写可核验的事实：
- **战术持仓**：`结构止损 X，距现价 −Y%`（隔夜跳空超过 Y% 即越过止损，以缺口价成交）。
- **战术前五（未持仓）**：`若此前入场，持有期将跨越该事件`。
- **核心名**：`≤5TD ⇒ 按 core-holdings-research §2b 从 baseline_plan 摘出、摊额改投`；否则「无动作」。
- **宏观事件**：对核心 sleeve 的唯一接口是 VIX 档（`panic_accelerator`），**不因事件预判加减仓**。

### 5. 归档
上一期日历里已发生的事件，在本期末尾追加「实际结果」一行（财报对 gate 的判定引用 thesis-tracker 日志）。
**只记录，不据此归纳「某类事件后通常涨/跌」**。

## 纪律
- 日历是**风险知情工具**，不产生任何买卖信号，不进 `final_score`、不进三轴门。
- 两本账分列：战术行标 paper，核心行标真金，不合并仓位。
- 无回测背书，不给事件影响任何概率或幅度预测。
