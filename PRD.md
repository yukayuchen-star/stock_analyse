# PRD — 美股量化选股与智能投资系统（缠论 × 宏观 × 量化）

> **Product Requirements Document — US Stock Selection & Intelligent Investment System**
>
> 版本 v1.0 ｜ 撰写日期 2026-07-09 ｜ 基线 commit `7b0c609`
> 本文档基于对全库的完整代码审计（Chan/Quant/Macro 三引擎 + 决策/风控/迟滞/模拟组合/回测全链路逐行核验）撰写。
> 覆盖范围：**仅美股主线（`main.py`）**；A股（`mainA.py`）仅在共用模块受影响时提及。
> **本文档为需求与审计结论，未随附任何源码修改。**

---

## 目录 Table of Contents

1. [概述 Overview](#1-概述-overview)
2. [系统现状架构 Current Architecture](#2-系统现状架构-current-architecture)
3. [审计结论：有效性验证 Audit: Validity Verification](#3-审计结论有效性验证-audit-validity-verification)
4. [缺陷与风险清单 Defects & Risks](#4-缺陷与风险清单-defects--risks)
5. [优化需求 Improvement Requirements](#5-优化需求-improvement-requirements)
6. [非功能需求 Non-Functional Requirements](#6-非功能需求-non-functional-requirements)
7. [验收与回归 Acceptance & Regression](#7-验收与回归-acceptance--regression)
8. [附录 Appendix](#8-附录-appendix)

---

## 1. 概述 Overview

### 1.1 产品定位

本系统是一个**每日运行**的美股量化投研引擎：以缠论结构择时为主轴一（55%）、宏观制度门控为主轴二（35%）、
量化五因子横截面排序为配角（10%），对股票池逐票产出五档评级（Buy/Overweight/Hold/Underweight/Sell）、
建议仓位、结构止损/止盈、入场区间，并驱动一个跨日结转的模拟组合（paper-trading），
最终输出**当日可交易的美股候选清单**（`output/{date}/daily_summary.md` + 分票报告 + `portfolio.md`）。

### 1.2 诚实边界（继承 CLAUDE.md，本 PRD 不做任何 overclaim）

- ✅ 忠实实现：包含关系、顶底分型、笔、中枢(ZG/ZD+延伸)、背驰（**创新极值 + MACD 面积衰竭**双条件）、
  一/二/三类买卖点、分型停顿法、走势类型、R 比率风控、右端三层防护（定笔/迟滞/波动率）。
- ❌ 按数据约束简化（US 只有日线数据，硬约束）：**线段**（以「笔→中枢」近似）、**级别递归/区间套**
  （仅日线单级别 + 周线 SMA 过滤）、一买的次级别背驰确认。
- ⚠️ 近似语义：中枢方向未计算；b2 实为「中枢下沿回踩」启发式。

### 1.3 本次审计的总体结论（TL;DR）

**架构与实现总体忠实于设计文档**：三引擎权重、背离分支、VIX 四档门控、背驰双条件、右端三层防护、
结构止损与 R_MAX 降级均与 CLAUDE.md 一致，历史 `price×(1−VIX%)` 止损坐标 bug 已彻底清除，
实盘路径无前视偏差（现价严格为 t-1 已完成 K 线）。

**但存在 3 个正确性缺陷（P0）、3 个阻断「每日自动运行」目标的工程缺口（P1）、
以及一批静默降级 / 校准漂移问题（P2/P3）**，详见第 4 章；对应修复需求按 R1→R4 分期，详见第 5 章。

---

## 2. 系统现状架构 Current Architecture

### 2.1 数据流全景

```mermaid
flowchart TD
    subgraph DATA[数据层 data/]
        YF[yfinance_source<br/>价格800d/info 7d TTL] --> PIPE[pipeline.fetch_all]
        FRED[fred_source<br/>7个FRED序列 24h TTL] --> PIPE
        FINN[finnhub_source 新闻/财报日历] --> PIPE
        CACHE[(SQLite cache<br/>market_data.db)] -.- YF & FRED & FINN
    end
    subgraph POOL[股票池]
        CORE[STOCK_POOL 9票核心] --> MERGE[core ∪ dynamic]
        DYN[load_dynamic_pool<br/>最新 stock_pool.json] --> MERGE
        UNIV[get_universe<br/>S&P500 ∪ NDX Top30] --> SCREEN[screening<br/>adds≥0.40 / removes≤-0.20]
        SCREEN -->|仅 TTY 交互采纳| MERGE
    end
    PIPE --> CHAN[缠论引擎 55%<br/>signals/chan/chan_signal.py]
    PIPE --> QUANT[量化引擎 10%<br/>signals/quant/factor_engine.py]
    PIPE --> MACRO[宏观引擎 35%<br/>signals/macro/macro_signal.py]
    CHAN & QUANT & MACRO --> SCORER[scorer.compute_final_score<br/>0.55/0.35/0.10 或背离 0.70/0.20/0.10]
    SCORER --> RATING[rating.score_to_rating<br/>五档 + VIX 评级上限]
    RATING --> RISK[risk_overlay<br/>VIX仓位门控/结构止损/R_MAX/入场区间]
    RISK --> HYST[hysteresis 迟滞B<br/>昨多→今出需连续2天]
    HYST --> PORT[portfolio_core 模拟组合<br/>output/us_portfolio.json]
    HYST --> REPORT[report_writer<br/>output/date/*.md]
    PIPE --> BT[P7 回测 engine.py<br/>extract_chan_events]
    PIPE --> FWD[P8 前向验证 forward_tracker]
```

### 2.2 三引擎与决策链职责

| 层 | 模块 | 职责 | 输出契约 |
|----|------|------|---------|
| 缠论（主轴一 55%） | `signals/chan/chan_signal.py` + `fractal.py`/`stroke.py`/`pivot.py` | 分型→笔→中枢→b1/b2/b3/s1/s2/s3 + 结构止损/R | `ChanSignalResult` |
| 宏观（主轴二 35%） | `signals/macro/macro_signal.py` + `regime.py`/`external_factors.py`/`sector_strength.py` | VIX 四档 + 利差 + 油/加息/美元/通胀 + 桶 IR | `MacroSignalResult` |
| 量化（配角 10%） | `signals/quant/factor_engine.py` + 五因子模块 | 基本面15/趋势25/动量30/相对20/量价10 | `QuantSignalResult` |
| 合成 | `decision/scorer.py` | 权重合成 + 背离分支 + 共振/逆风标记 | `ScorerOutput` |
| 评级 | `decision/rating.py` | 五档阈值 + VIX 评级上限 | `str` |
| 风控 | `decision/risk_overlay.py` | 仓位=min(max(0,score), VIX上限)、结构止损、R_MAX、入场区间 | `RiskOverlay` |
| 迟滞 | `decision/hysteresis.py` + `hysteresis_core.py` | 昨多→今出需连续 CONFIRM_DAYS=2 天 | 就地改 `StockDecision` |
| 组合 | `decision/portfolio_core.py` + `main.py:_run_portfolio` | Buy/Overweight 建仓、卖点/止损清仓、跨日结转 | `output/us_portfolio.json` |

### 2.3 每日输出物

| 产物 | 路径 | 说明 |
|------|------|------|
| 分票报告 | `output/{date}/{TICKER}.md` | 三引擎明细 + 决策 |
| 当日汇总 | `output/{date}/daily_summary.md` | 可交易候选排名 |
| 模拟组合 | `output/{date}/portfolio.md` + `output/us_portfolio.json` | 权益/持仓/成交 |
| 池快照 | `output/{date}/stock_pool.json` + `pool_history.jsonl` | 动态池演化 |
| 回测/前向 | 回测报告 + `forward_validation.md` | 信号 ≥5TD 后评估 |
| 状态 | `output/signal_state.json` | 迟滞状态机 |

---

## 3. 审计结论：有效性验证 Audit: Validity Verification

> 本章为「验证通过」项，全部经逐行核验，含证据位置。第 4 章为发现的问题。

### 3.1 缠论引擎 Chan Engine — **结构与信号逻辑忠实于缠论.md 核心精髓**

| # | 验证项 | 结论 | 证据 |
|---|--------|------|------|
| C1 | 包含关系处理：上合并取 max(high)/max(low)（GG），下合并取 min/min（DD），方向由前两根处理K高点决定 | ✅ 正确 | `fractal.py:39-49,74-82` |
| C2 | 顶/底分型：严格三元组，中间 K 高低点同时为极值（严格不等） | ✅ 正确 | `fractal.py:89-110` |
| C3 | 笔：端点分型间隔 ≥ MIN_BARS=4 根处理K，同类分型保留更极端者，交替性强制 | ✅ 正确 | `stroke.py:22,61-97` |
| C4 | 中枢：ZG=min(三笔高)、ZD=max(三笔低)、ZG>ZD 才成立；第 4 笔起重叠则延伸收窄 | ✅ 正确 | `pivot.py:66-95` |
| C5 | **背驰双条件**：b1 必须 `last.low < prev_down.low`（创新低）**且** MACD 柱面积 `< prev_area × 0.8`；s1 对称要求创新高 + 面积衰竭。仅面积衰减而未创新极值不触发 | ✅ 与「创新极值+力度衰竭」精髓一致 | `chan_signal.py:326-333,361-369` |
| C6 | MACD：标准 12/26/9，`ewm(adjust=False)`，面积=笔区间内柱绝对值之和 | ✅ 正确 | `chan_signal.py:180-190` |
| C7 | 分型停顿法：底分型后收盘站上「第三根处理K」高点才确认（顶对称） | ✅ 符合缠论第四章 | `chan_signal.py:196-217` |
| C8 | 右端防护 A（定笔）：末笔终点分型须再过 `STROKE_CONFIRM_BARS=2` 根处理K；C'（波动率）：`atr_pct≥6%` 追加 +2 根并打 HIGH_VOL | ✅ 与 CLAUDE.md 一致 | `chan_signal.py:31-34,430-440` |
| C9 | 右端防护 B（迟滞）：昨多→今出需连续 2 天；`fresh_prior` >5 天旧态重置；VIX panic 放行即时离场 | ✅ 正确 | `hysteresis.py:27-60`, `hysteresis_core.py:20-21,43-52` |
| C10 | 信号发射三重门：`is_fresh(15天内) AND fractal_stop AND stroke_confirmed` 同时满足才检测买卖点 | ✅ 正确 | `chan_signal.py:446` |
| C11 | 结构止损：b1/b2=末笔低×0.99、b3/lb2=ZG×0.99、s1/s2=末笔高×1.01、s3=ZD×1.01；R=\|entry−stop\|/entry | ✅ 与 CLAUDE.md 止损设计一致 | `chan_signal.py:242-263` |
| C12 | 周线过滤：SMA20W ±2% 三态；周线 down 且多头信号 → score×0.5；共振 res=2 需周线 up | ✅ 正确 | `chan_signal.py:268-284,458-465` |
| C13 | 走势类型：中枢数 ≥2=trend、=1=consolidation；趋势背驰×1.15 / 盘整背驰×0.85 仅作用于 b1/s1 | ✅ 符合「趋势背驰更可靠」 | `chan_signal.py:221-236,455-456` |
| C14 | 历史遗留 bug「止损用 price×(1−VIX%) 导致 stop>entry」 | ✅ **已彻底清除**，全库 grep 无残留；现优先结构止损 | `risk_overlay.py:99-112` |

### 3.2 量化引擎 Quant Engine — **五因子公式与权重与设计一致**

| # | 验证项 | 结论 | 证据 |
|---|--------|------|------|
| Q1 | 顶层权重 0.15 fund / 0.25 trend / 0.30 mom / 0.20 rel / 0.10 vol，凸组合后 clip[-1,1] | ✅ | `factor_engine.py:17-21,92-97` |
| Q2 | 基本面：revenueGrowth/earningsGrowth/ROE/grossMargins/D-E/PEG + Piotroski-lite 质量分；缺失字段按可用权重重归一（不填零），暴露 coverage 指标 | ✅ 设计合理 | `fundamental.py:89-142` |
| Q3 | 趋势：0.35×MA位置(200/60/20 分层退化) + 0.25×多头排列 + 0.25×EMA20 五日斜率 + 0.15×ADX14（Wilder EWM） | ✅ | `trend.py:7-34,65-108` |
| Q4 | 动量：0.28×ROC20 + 0.28×MACD(20日σ归一) + 0.24×RSI14 + 0.20×KAMA 斜率；Pullback/Breakout 作为非线性加分 `special×(1−|base|)` 防越界 | ✅ | `momentum.py:58-117` |
| Q5 | 相对强度：0.50×vs QQQ + 0.35×桶内百分位 + 0.15×vs SPY，超额 6% → ±1 | ✅ | `relative.py:29-66` |
| Q6 | 量价：0.60×OBV 10日变动(2σ归一) + 0.40×VWMA20 偏离(×15) | ✅ | `volume.py:24-44` |
| Q7 | 无前视（实盘路径）：所有 EMA/KAMA 递归式 `adjust=False`，仅用已完成K线 | ✅ | 各因子模块 |

### 3.3 宏观引擎 Macro Engine — **VIX 门控与制度分类正确**

| # | 验证项 | 结论 | 证据 |
|---|--------|------|------|
| M1 | VIX 四档：<15 calm(100%/+0.5)、15-25 neutral(70%/0)、25-35 tense(40%/−0.5)、>35 panic(0%/−1)，与 CLAUDE.md 门控表逐项一致 | ✅ | `regime.py:13-28` |
| M2 | 宏观合成：0.35×VIX + 0.20×利差(10Y−2Y, ±1.5%→±1) + 0.30×外部因子 + 0.15×桶IR，clip[-1,1] | ✅ | `macro_signal.py:14-23,107-113` |
| M3 | 外部因子：油(−ret20d/0.15)、加息预期(−(DGS2−FEDFUNDS)/1.5)、美元(−ret20d/0.05)、通胀(−(T10YIE−2.5)/0.5)；复合仅对**数据可得**因子求均值（0 视为有效中性，不剔除）——两个历史 bug（anomaly 未计入、中性因子被剔除）已修复并有注释存证 | ✅ | `external_factors.py:128-235,278-289` |
| M4 | 异动检测：油/美元 252 日窗口 Z-score，\|z\|≥2 告警，`anomaly_score=max(−0.3, −0.1×n)` 计入复合 | ✅ 机制存在（单边性见缺陷 #15） | `external_factors.py:86-96,281-289` |
| M5 | VIX 评级上限：panic→最高 Hold、tense→最高 Overweight、calm/neutral 不限；仅向下压不抬升 | ✅ | `rating.py:26-44` |
| M6 | VIX tense 缠论门槛：非 b1 或共振<2 → 仓位×0.5 并打 VIX_TENSE_CHAN | ✅ 与「25-35 仅1买+多级共振」对应（以降半仓实现，非一票否决——语义弱于门控表，见缺陷 #12b） | `risk_overlay.py:57-64` |

### 3.4 决策与风控链 Decision Chain

| # | 验证项 | 结论 | 证据 |
|---|--------|------|------|
| D1 | 标准权重 0.55/0.35/0.10；背离（chan≥+0.30 且 quant≤−0.10）→ 0.70/0.20/0.10「结构>统计」 | ✅ | `scorer.py:27-37,64-73` |
| D2 | 共振（chan≥+0.30 且 macro≥0）/ 逆风（chan≥+0.30 且 macro≤−0.15）仅打标记不重复计分 | ✅ | `scorer.py:76-84` |
| D3 | R_MAX：缠论买点 r_ratio>15% → R_MAX_EXCEEDED + 仓位清零 + strategy 联动降级 Hold | ✅ | `risk_overlay.py:70-81`, `strategy.py:70-72` |
| D4 | B3 入场窗口：理想 ZG×0.99~1.03；现价高于上界 → B3_WINDOW_PASSED + 改用现价±0.5%（不诱导「等跌回 ZG」） | ✅ 与 CLAUDE.md 设计一致 | `risk_overlay.py:126-149` |
| D5 | 无前视（实盘）：yfinance `end=today` 为排他区间 → 现价=**t-1 已完成K线收盘**；前向验证以 `df.index[-1]` 真实K线日为锚，非日历日 | ✅ | `pipeline.py:28-31`, `forward_tracker.py:112-121` |
| D6 | 缓存卫生：空 DataFrame 一律不入缓存（避免 API 故障被 24h TTL 掩盖）；7 天自动清理 | ✅ | `cache.py:52-53`, `housekeeping` |
| D7 | 前向验证止损撮合用 `min(stop_loss, Open)` 模拟跳空击穿，不理想化成交 | ✅ | `forward_tracker.py:192-226` |

---

## 4. 缺陷与风险清单 Defects & Risks

> 分级：**P0**=正确性缺陷（结果可能错误）；**P1**=阻断「每日自动运行」目标；
> **P2**=静默降级/数据质量；**P3**=校准偏差与文档漂移。每条含失效场景。

### 4.1 P0 — 正确性缺陷 Correctness Defects

| # | 缺陷 | 位置 | 失效场景与影响 |
|---|------|------|---------------|
| **1** | **模拟组合卖出绕过迟滞层（B层防护对组合失效）** | `main.py:257`（`is_sell = rating in _SELL or sell_pt is not None`） | 迟滞层在翻转第 1 天把 rating 改为 `Hold` 沿用昨仓（`hysteresis.py:42-51`），但组合层只要 `chan.sell_point_type` 非空就当日强制清仓——右端一根新K重画出的 s 类卖点仍会造成隔夜甩卖，正是 A/B/C' 三层防护要压制的场景。且与 `screening.py:195-234` 的移除条件（`sell_pt 非空 **且** chan.score<0`）语义不一致：同一卖点，选股层「观察」、组合层「清仓」。 |
| **2** | **结构止损可能高于入场价（负风险额）** | `risk_overlay.py:102-106` | 当缠论触发**卖点**（s1/s2/s3，`chan.stop_loss=末笔高×1.01 > price`）而宏观强正把 `final_score` 抬为正数时，代码进入多头止损分支且不校验 stop<price：`risk_amount = price − stop < 0` → `take_profit < price`。产出「止损在上、止盈在下」的自相矛盾执行参数（与已修复的 QCOM 历史 bug 同型，只是触发路径不同）。组合层随后 `price < stop_loss` 恒真 → 买入即触发止损卖出。 |
| **3** | **回测事件提取存在轻度前视（与 docstring 声明不符）** | `chan_signal.py:95-165`（`extract_chan_events`，docstring 声称「无前视偏差」） | `pbars/strokes` 在**全量历史**上一次性构建后按 `strokes[:i+1]` 切片：(a) 包含关系合并会用**未来被吸收的K线**改写已有处理K的高低点（`fractal.py:82` 原地替换 `result[-1]`）；(b) `build_strokes` 清洗可因后来的分型回溯弹出旧分型（`stroke.py:72-87`）。故第 i 笔「当日可见结构」≠ 截至当日重算的结构。MACD/价格已按 `sub_df` 正确截断，泄漏量级小，但 **79.8% 缠论胜率与 ML 回测结论可能轻度乐观**，且该胜率是 55% 主轴权重的实证依据，需重新标定。 |

### 4.2 P1 — 每日自动运行阻断项 Daily-Run Blockers

| # | 缺陷 | 位置 | 失效场景与影响 |
|---|------|------|---------------|
| **4** | **cron/非TTY 下池变更永不生效** | `main.py:118-120`（池编辑器仅 `sys.stdin.isatty()` 时交互） | 定时任务运行时，`screen_for_adds/removes` 每天都在计算候选（耗 API 配额）但结果被静默丢弃，动态池永久冻结——「每日输出可交易候选」目标退化为「固定 9+N 票的每日重打分」。A股侧已有 `watchlist.txt` 文件输入先例（commit `5d449cc`）可平移。 |
| **5** | **无 CLI/调度接口与交易日历** | `main.py:544-545`（无 argparse）；`utils/time_utils.py:8-13` | 无 `--non-interactive`/`--date`/`--skip-backtest` 等参数；`prev_trading_day` 仅跳周末**不含美股节假日**（代码注释自认），节假日运行时「t-1 基准日」标注错误、模拟组合快照日期与真实交易日错位。 |
| **6** | **油价/美元绕过缓存直连 Yahoo** | `external_factors.py:99-111` | `CL=F`、`DX-Y.NYB` 每次 run 都 `yf.download(period="2y")`，不走 SQLiteCache、无重试/限流；失败时该因子**静默退出**复合均值分母 → 同一天两次运行宏观分可能不同（不可复现），且是每日调度下最先触发 Yahoo 限流的点。 |

### 4.3 P2 — 静默降级与数据质量 Silent Degradation & Data Quality

| # | 缺陷 | 位置 | 失效场景与影响 |
|---|------|------|---------------|
| **7** | VIX 缺失 → 默认 20（neutral、70% 上限）仅记 log；DGS10/DGS2 缺失 → yield_score=0 | `macro_signal.py:77-96` | FRED 断供当天，系统以「中性宏观」继续给出买入建议，报告无任何降级标记——风险门控主轴在数据故障时静默失效。 |
| **8** | 量化子因子任何异常 → 0.0 中性 | `factor_engine.py:77-84` | 错误与真中性不可区分，复合分被静默拉向 0；仅基本面暴露 `coverage`，trend/mom/rel/vol 无数据完备度指标。 |
| **9** | FRED `get_latest` 无新鲜度校验；全库无重试/退避 | `fred_source.py:62-67`；各 source | CPI/T10YIE 数据点可能滞后数周仍被当作「最新」；任何一次网络抖动即当日该数据缺失（配合 #7/#8 静默降级）。 |
| **10** | universe marketCap 失败 → 0 静默出局 | `data/universe.py:61-76` | Yahoo 单票限流即把该票挤出 NDX Top-30 排名，候选宇宙逐日漂移且无告警。 |

### 4.4 P3 — 校准偏差与文档/实现漂移 Calibration & Drift

| # | 缺陷 | 位置 | 说明 |
|---|------|------|------|
| **11** | 报告权重硬编码字符串「55%/35%/10%」 | `report_writer.py:96,118,134` | 背离分支实际 70/20/10 时报告仍显示 55/35/10，误导人工复核。`ScorerOutput` 已携带真实权重却未被使用。 |
| **12** | Buy≥0.60 几乎不可达（评级标尺名不副实） | `rating.py:17-22` | chan 贡献上限 0.55×0.75(b2)=0.41，需 macro≥+0.5 且 quant 强正才可达 0.60——实际输出以 Overweight 为主。组合 `_BUY` 含 Overweight 故系统可运转，但「Buy」档基本空转。**12b**：VIX tense 的缠论门槛以「仓位×0.5」实现，弱于门控表「仅 1买+多级共振有效」的一票否决语义。 |
| **13** | RSI 用简单 rolling mean 而非 Wilder 平滑 | `momentum.py:16-23` | 与标准 RSI14 数值系统性偏差（同文件 ADX 却用了 Wilder EWM，口径不一致）。 |
| **14** | CLAUDE.md 称「桶内横截面 Z-score」，实现为百分位 rank | `relative.py:48-63` | rank 对肥尾更稳健（docstring 有意为之），属**文档漂移**非代码缺陷，应改文档。 |
| **15** | 异动惩罚单边看空；`breakeven_trend` 名不副实 | `external_factors.py:213-235,281-289` | anomaly_score 只减不加（利多异动如油价暴跌也只能扣分）；`breakeven_trend` 实为「与硬编码 2.5% 目标的偏离」而非 20 日趋势（注释已自认）。 |
| **16** | 基本面无「财报+2月延迟」 | `fundamental.py` 全文（消费实时 `Ticker.info`） | 违反 CLAUDE.md 开发原则 4 的字面要求。**实盘每日扫描可接受**（用的就是当下已公开数据）；但若该模块被复用进回测即构成前视，需在代码/文档中显式声明边界。 |
| **17** | 其余工程卫生 | `portfolio_core.py:82-88`（当日重跑仅回滚快照不回滚持仓，已注释的已知局限）；`stroke.py:78-87`（`len(clean)<2` 时贴近分型静默丢弃，仅影响极早期历史）；`alpha_vantage_source.py`（实盘路径未使用，死代码）；周线过滤不对称（仅折半多头、不抑制周线 up 时的空头信号，属设计选择应文档化） | — |

---

## 5. 优化需求 Improvement Requirements

> 按 R1（正确性）→ R2（可调度性）→ R3（数据可靠性）→ R4（校准与观测）分期。
> 每条含：背景 / 需求 / 验收标准 / 涉及文件。**遵循开发四原则：极简、精准、无前视。**

### R1 — 正确性修复（最高优先，对应缺陷 #1-#3）

> **✅ 实施状态（2026-07-09）**：R1.1/R1.2/R1.3 已全部实施并通过验收（22 个场景单测 +
> 3 票新旧事件序列 diff + P7/ML 回测重跑）。R1.1 顺带修复组合「卖出后同日回补」洗仓缺陷
> （`portfolio_core.py` buys 过滤加 `not s.is_sell`）。
> **R1.3 重要修正**：缺陷 #3 实测为**重度**而非"轻度"前视——旧提取只统计"存活到最终
> 几何"的笔，被重画的失败信号（恰是亏损边）被结构性删除。as-of 逐日重放（复刻实盘三重
> 发射门）后：ML 规则策略 79.4% → **53.2%**（632 信号）< 随机基准 55.5%；P7 核心池
> 109 笔 40.4%（b3 53.3% 唯一强类型，b1/b2 ≈35%）。55/35/10 权重的实证依据已失效，
> **R4.2 评级/权重重标定升级为高优先**（✅ 已于 2026-07-14 落地，见 R4.2 状态块）。
> **✅ A股移植（2026-07-15）**：`extract_chan_events_ashare` 已按同法改为逐日 as-of 重放
> （三重发射门用 A股实盘口径：is_fresh 12 交易日；MACD 用预计算列切片；分值 BUY_SCORES_ASHARE）。
> 缓存真实K线验证 15/15：低波动名旧事件 100% 同日复现，高波动名（日均振幅 8.6%）延后 2~6 天
> 与 C' 门一致，事件数 1.5~3.3 倍（被删失败信号回归）。**68.7%/六阶段基线重测被数据阻塞**：
> `processed_stocks_selected/` 当前不在机器上，放回后运行 `python run_ashare_backtest.py` 即可重测；
> 重测出无偏基线前，A股买点分值维持原标定不动。

**R1.1 组合卖出与迟滞层协调**
- 背景：缺陷 #1。缠论卖点当日直通清仓，绕过 B 层迟滞，重现「LITE 隔夜甩动」类场景。
- 需求：统一卖出语义（推荐方案）——组合层卖点触发同样经迟滞状态机（卖点连续 `CONFIRM_DAYS=2` 天才清仓），
  或最低限度与 screening 对齐为「`sell_pt 非空 且 chan.score<0`」；跌破结构止损的卖出**不受迟滞约束**（风控优先）；VIX panic 直通离场维持不变。
- 验收：构造「昨日 Overweight 持仓 + 今日首现 s2 但 score>0」用例，组合当日**不**清仓且报告出现 HYSTERESIS_HOLD；连续第 2 天卖点则清仓；跌破止损当日照卖。
- 涉及：`main.py:_run_portfolio`、`decision/hysteresis.py`（或新增卖点 streak 字段入 `signal_state.json`）。

**R1.2 结构止损方向校验**
- 背景：缺陷 #2。多头分支可能采用卖点侧止损（stop>price）。
- 需求：`risk_overlay` 多头止损仅当 `chan.stop_loss < current_price` 时采用结构止损，否则回退 `_STOP_PCT[regime]` 百分比兜底；对称地，若未来支持空头参数亦校验方向。
- 验收：单测「final_score>0 且 chan 信号为 s1（stop=末笔高×1.01）」→ 输出止损 < 现价、止盈 > 现价；现有多头买点用例输出不变。
- 涉及：`decision/risk_overlay.py:99-112`。

**R1.3 回测结构 as-of 重算（或如实降级声明）**
- 背景：缺陷 #3。`extract_chan_events` 的笔结构来自全量历史，胜率证据（79.8%，55% 权重依据）轻度乐观。
- 需求（二选一，推荐 a）：
  (a) 事件循环内按 `sub_df` 截断后**重跑** `process_bars→detect_fractals→build_strokes`（O(n²) 可接受，池小 + 每日一次；可按 stop_date 递增做增量优化）；
  (b) 若性能不可接受，删除「无前视偏差」docstring 声明，改为如实标注「结构全量构建、指标按日截断，存在轻度右端泄漏」，并在回测报告中注明。
- 验收：方案 a——对 3 只代表票对比新旧事件序列，输出差异清单；重跑 ML 回测更新胜率基线并同步修订 CLAUDE.md/MEMORY 中的 79.8% 数字。方案 b——docstring 与回测报告已更新。
- 涉及：`signals/chan/chan_signal.py:95-165`、`backtest/ml_backtest.py`（基线数字）。

### R2 — 可调度性（对应缺陷 #4-#5，达成「每日自动运行」目标）

> **✅ R2 实施状态（2026-07-16）**：R2.1 + R2.2 已实施并通过验收（23 项单测 + 端到端非交互跑通）。
> - **R2.1**：argparse `--non-interactive`（非 TTY 自动启用）/ `--auto-adopt-adds N`（默认 0=仅记录）/
>   `--date`（补跑标签，数据仍为当前抓取，已在 --help 中如实说明）；`_non_interactive_pool_update`
>   自动采纳 Top-N、**removes 一律仅记录不执行**（保守）；`watchlist_us.txt` 人工强制关注
>   （`pool_manager.load_us_watchlist`，与 A 股 watchlist.txt 同格式，gitignore 不入库），
>   两种模式均在流程起点并入 dynamic_pool（source="watchlist"）。异常退出码 1、--date 格式错 2。
> - **R2.2**：`utils/time_utils` 内置规则法 NYSE 假日（含复活节/observed 移位/元旦落周六不补休特例，
>   2025/2026 与官方日历逐日核对一致），`is_trading_day` + `prev_trading_day` 升级；
>   非交易日：非交互模式 0.3s 快速退出（码 0，cron 不误报），TTY 模式警告后继续（保留周末人工复盘用法）。
> - **顺带修复**：验收跑发现 Wikipedia Nasdaq-100 主条目 2026-07 改版后不含成分表 →
>   `_NDX_URL` 改指专页 `List_of_NASDAQ-100_companies`；且 universe 抓取失败降级为
>   「本次无 add 候选」warning，不再终止主流程（缺陷 #10 相邻问题，完整 DEGRADED 贯穿仍属 R3.2）。

**R2.1 非交互模式与池变更落地**
- 需求：`main.py` 增加 argparse：`--non-interactive`（默认在非 TTY 自动启用）、`--auto-adopt-adds N`（自动采纳 Top-N 加池候选，0=仅记录）、`--date YYYY-MM-DD`（补跑）；
  平移 A 股 `watchlist.txt` 先例为 `watchlist_us.txt`（人工强制关注列表，与筛选合并去重）；removes 建议默认仅记录不自动执行（保守）。
- 验收：`echo | python main.py --non-interactive --auto-adopt-adds 3` 全程无输入等待、退出码 0；`stock_pool.json` 反映新增票；TTY 交互行为不变。
- 涉及：`main.py:118-120,392-398,544`、`config/pool_manager.py`。

**R2.2 美股交易日历**
- 需求：`utils/time_utils.py` 引入 NYSE 节假日（优先 `pandas_market_calendars`；否则内置年度假日表），`prev_trading_day`/`today_str` 语义与真实交易日对齐；非交易日运行时提前退出并明示。
- 验收：对 2026-07-03（独立日休市前后）等节假日用例断言 t-1 标注正确；周一运行回溯到上周五。
- 涉及：`utils/time_utils.py`、`main.py`（开盘日判断）。

### R3 — 数据可靠性（对应缺陷 #6-#10）

> **✅ R3 实施状态（2026-07-16）**：R3.1–R3.3 已实施并通过验收（21 项 mock 单测 + 端到端）。
> - **R3.1**：`with_retry` 薄封装（`data/base.py`，≤2 次重试 + 1s/2s 指数退避，穷尽原样抛出）
>   套在 `yf.download` 与 FRED `get_series`；油/美元改走 `YFinanceSource`+SQLiteCache
>   （断供第二跑命中缓存输出一致）。顺带对齐无前视口径：旧 `period="2y"` 混入今日盘中
>   未完成 bar，改 `end=today`（排他）→ 最后一根为 t-1 已完成K线，同日重跑可复现。
> - **R3.2**：`FREDSource.get_latest_dated` + `staleness`（日频 5 日历日≈2 交易日、月频 45 天）；
>   `pipeline.get_macro_snapshot` 返回 `(snapshot, degraded)`；`MacroSignalResult.degraded` 聚合
>   FRED_MISSING/FRED_STALE/VIX_MISSING/YIELD_MISSING/外部因子不可用；`QuantSignalResult.factor_ok`
>   五子因子布尔（异常≠中性，info 空时 fund 亦标记）；`daily_summary.md` 头部降级区块（正常日无）。
> - **R3.3**：`get_universe` marketCap 失败率 >20% → 告警 + 复用最近历史缓存且不写今日缓存
>   （下次可重试）；无缓存可回退时按部分数据继续并告警。

**R3.1 油/美元入缓存 + 统一重试**
- 需求：`external_factors._fetch_price_series` 改走 `YFinanceSource.get_price`（享受 SQLiteCache）；
  `yfinance_source`/`fred_source` 增加统一「重试 ≤2 次 + 指数退避」薄封装（不引重依赖）。
- 验收：断网下第二次运行命中缓存产出与首次一致的宏观分；日志出现重试记录而非直接失败。
- 涉及：`signals/macro/external_factors.py:99-111`、`data/yfinance_source.py`、`data/fred_source.py`。

**R3.2 宏观/量化 DEGRADED 标记贯穿**
- 需求：`MacroSignalResult` 增加 `degraded: List[str]`（如 `VIX_MISSING`、`FRED_STALE>7d`、`OIL_UNAVAILABLE`）；FRED `get_latest` 返回值附带数据点日期并按序列设新鲜度阈值（VIXCLS/DGS 2 个交易日、CPI/UNRATE 45 天）；`QuantSignalResult` 为五个子因子各暴露 `data_ok` 布尔；`daily_summary.md` 头部显示当日降级项。
- 验收：mock VIXCLS 缺失 → 汇总报告头部出现「⚠️ 宏观降级：VIX_MISSING（按 VIX=20 处理）」；正常日无该区块。
- 涉及：`signals/macro/macro_signal.py`、`data/fred_source.py`、`signals/quant/factor_engine.py`、`report/report_writer.py`。

**R3.3 universe 排名健壮性**
- 需求：marketCap 抓取失败的票记入日志计数并在失败率 >20% 时告警（当日复用上一日 universe 缓存），不再静默置 0。
- 验收：mock 30% 失败率 → 日志告警 + 使用前日缓存；正常日行为不变。
- 涉及：`data/universe.py:61-76`。

### R4 — 校准与观测（对应缺陷 #11-#16）

> **✅ R4 实施状态（2026-07-17，R4.2 已于 07-14 先行落地）**：
> - **R4.1 已实施**：`StockDecision` 新增 `chan/macro/quant_weight` + `divergence_applied`
>   （ScorerOutput 透传），三处报告标题动态渲染；背离票额外显示「⚠️ 背离加权生效」提示条。
>   验收：构造背离票渲染 70%/20%/10% + 提示条，普通票 55%/35%/10% 无提示条 ✓。
> - **R4.3 对比完成，决策=不切换**：128 只缓存池实测，Cutler vs Wilder RSI 值差
>   mean|Δ|=6.1点/max 17点（Wilder 拉极值向 50），但经 24%×30%×10% 三层衰减后
>   **|Δfinal|≤0.0035、80/20 特判带翻转 0 只**——决策层差异不显著；切换需重跑 ML 回测
>   重建全部基线，成本收益不成立。决策记录入 `momentum.py._rsi` docstring 与 CLAUDE.md。
> - **R4.4 已实施**：CLAUDE.md「桶内 Z-score」→「百分位 rank」+ RSI Cutler 口径注记 +
>   基本面快照禁入回测（开发四原则#4）+ 周线单边过滤注记（缠论精髓节）；
>   `breakeven_trend` 改名 `breakeven_deviation`（仅定义文件内两处引用，无外部依赖）；
>   `alpha_vantage_source.py` 加保留注记（PIT 回测基本面唯一入口，删除即断路）。
> - **R4.5 缓议（记录在案）**：异动双向化需先证明「利多异动加分」提升宏观分辨力，
>   当前无宏观标注回测框架可验证；单边看空 cap −0.3 是保守偏置（宁可少赚不多亏），
>   在证据出现前维持现状。若未来搭建宏观事件回测（异动日 vs 前向 QQQ 收益），再评估。

**R4.1 报告权重动态化**：`report_writer` 从 `ScorerOutput.chan_weight/macro_weight/quant_weight` 取实际权重渲染标题；背离票额外显示「背离加权 70/20/10」。验收：构造背离票，报告标题显示 70%/20%/10%。涉及：`report/report_writer.py:96,118,134`、`decision/strategy.py`（透传 ScorerOutput）。

**R4.2 评级标尺重标定**：基于近 60 个交易日 final_score 分布（`output/*/` 已留存）重设阈值（如 Buy≥0.50）或将档位更名为语义中性（Strong/Positive/Neutral/...）；同时评估 12b——VIX tense 下非「b1+共振2」是否应从「×0.5」升级为一票否决以对齐门控表。验收：重放近 30 日历史决策，输出新旧评级迁移矩阵供人工确认后启用。涉及：`decision/rating.py`、`decision/risk_overlay.py:57-64`。

> **✅ R4.2 实施状态（2026-07-14，用户确认后落地）**：按 R1.3 无偏基线
> （b3 53.3% 唯一期望为正 ≈+0.60R@2:1；b1 35.3%/b2 35.6% 贴近 2:1 保本线 33.3%）重标定：
> 1. **买点分值类型内重分配**（`chan_signal.py` 新增 `BUY_SCORES` 表）：b3 0.65→**0.75**、
>    b2 0.75→**0.40**、b1 0.50→**0.35**；`_detect_buy` 参数化 `scores`，
>    **A股钉住原分值**（`chan_signal_ashare.BUY_SCORES_ASHARE`，两处调用点显式传入，行为零变化——
>    A股偏差未修，不适用美股标定）。卖点分值未动（回测仅做多，无卖侧胜率数据）。
> 2. **Buy 阈值 0.60→0.50**（`rating.py`）：旧标 Buy 几乎不可达；新标下 b3+宏观≥+0.25 恰达 Buy。
> 3. **DIV_CHAN_MIN 0.30→0.45**（`scorer.py`）：背离「结构优先」加权只为 b3 级强结构保留
>    （b1×趋势加权最高 0.4025 不触发）。55/35/10 顶层权重维持——无新证据支持特定替代值，
>    类型内重分配已使弱类型自然拉低 final_score。
> 4. **12b 处置**：tense 门由「仅 b1+共振2」改为「**b3，或 b1+共振2**」（`risk_overlay.py`），
>    维持 ×0.5 不升级一票否决（无分制度胜率数据支持否决）。
> 验收：无历史成分分留存（output 仅 Markdown），30 日真实重放不可行，改为**全场景合成迁移矩阵**
> （64 组合：类型×趋势×周线×macro×quant）——b3 上迁 6 项（顺风 OW→Buy）、b2 下迁 8 项、
> b1 下迁 10 项，b1/b2 单独出现全部落 Hold；20/20 断言通过（含 A股分支恒等）。

**R4.3 指标口径统一（谨慎项）**：RSI 切换 Wilder 平滑前，先用回测基线对比新旧口径的量化分分布与 ML 回测胜率差异，差异显著才切换（避免为「标准化」而破坏已验证基线）。验收：对比报告先行，切换与否留决策记录。涉及：`signals/quant/momentum.py:16-23`。

**R4.4 文档对齐**：CLAUDE.md「桶内 Z-score」改为「桶内百分位 rank」；`breakeven_trend` 改名 `breakeven_deviation`（或文档注明）；周线过滤不对称、基本面实时快照边界（仅限实盘、禁入回测）写入 CLAUDE.md；删除 `alpha_vantage_source.py` 或注明保留原因。验收：文档与实现一致，`graphify update .` 后 wiki 无漂移。

**R4.5 异动双向化（低优先）**：评估 anomaly 按方向赋号（利多异动加分、利空减分，仍 cap ±0.3）；先在回测中验证是否提升宏观分辨力再上线。

### R5 — 量价确认因子 Volume-Confirmation Factors（借鉴外部策略，2026-07-20 立项）

#### R5.0 立项背景与审阅结论 Background & External Review

**来源**：用户提出借鉴 `ZhuLinsen/daily_stock_analysis`（15 个内置策略）增强本系统的量化信号
生成与因子工程。**明确排除** LLM 叙事层——系统保持纯 Python 确定性引擎。

**外部项目审阅结论**（已抓取策略 YAML 原文逐条核对，非凭名字判断）：
- 该项目的 15 个"策略"是喂给 LLM 的自然语言 prompt（`.yaml`），**无回测、无胜率验证、无数值引擎**；
- 逐条映射到本项目：**~9 个与现有能力重复或不如现有**（均线金叉/箱体/趋势/缠论——本项目有
  均线排列因子、中枢、趋势因子、真缠论结构引擎）；**~4 个对美股不适用或不可证伪**
  （艾略特波浪；龙头/热点/情绪周期/事件驱动=A股散户题材博弈，无干净美股数据源）；
- **真正可借鉴的 3 个想法（shrink_pullback / volume_breakout / bottom_volume）本质是同一原则
  的三种表达：量价确认——成交量证实或证伪价格行为**。

**核心论点**：借来的 alpha 不是"15 个策略"，而是本系统**系统性欠用的一个维度**——
`momentum.py` 的 pullback(+0.30)/breakout(+0.20) special 信号是**纯价格逻辑**（只看价格
在哪，不看量能是否配合）。且量价确认原则**本项目 A 股侧 lb2 已内部验证过**
（`chan_signal_ashare._pb2_*`：极度缩量+BOLL收口+放量突破），本次属于**移植已验证的内部
概念到美股量化侧**，非引入外部未验证策略。

**已锁定的三项范围决策**（用户确认）：
1. **Scope**：仅量价确认（3 个量能想法，全部可回测）；基本面一致性（growth_quality）与
   新数据源类（expectation_repricing）排除。
2. **Depth**：仅量化 10% sleeve（只改 `momentum.py`），**不触碰缠论 55% 本体**。
3. **Validation**：严格回测门——每个因子须回测证明 ≥ 基线才准 merge 进实盘打分。

**防复发纪律**（幸存者偏差教训，R1.3）：每条借来的规则落成**确定性因子**，回测采用与实盘
共用的 as-of 发射逻辑，**过 P7 级回测门后才进入实盘打分**。

#### R5.1 Pullback 量能收缩门（借鉴 shrink_pullback）

- 背景：缩量回调=惜售/健康吸筹，放量回调=派发/破位前兆——同一价格形态，量能决定其含义相反。
- 现状：`momentum.py:103-108`（R4.3 注记后行号有偏移，逻辑段为 Pullback special）——上升趋势
  (c>SMA200) 且 -3%≤ema_dev≤+1% → special=+0.30，**不看成交量**。
- 需求：引入 `pullback_vol_ratio = Volume.tail(3).mean() / Volume.rolling(20).mean().iloc[-1]`
  （仅用 ≤当日数据，无前视）：
  - ratio < 0.7（缩量）→ 维持/略增 special；
  - 0.7 ≤ ratio ≤ 1.0（中性）→ 折减；
  - ratio > 1.0（放量回调）→ 归零或微负。
- ⚠️ 阈值 0.7 为外部 A 股经验值，**须在美股数据上回测重标定**，禁止照搬。
- 验收：R5.4 因子回测门下，量能门版 pullback 胜率/期望 ≥ 纯价格版；信号数不塌缩到无统计意义；
  无 Volume 数据时回退旧行为（行为不变）。
- **🔴 结论（2026-07-24 因子回测，13,540 pullback 事件）：证伪且方向相反——不 merge。**
  缩量 KEEP(<0.7) fwd10 win .526/exp **+.0041** < 放量 DEMOTE(≥1.0) win .585/exp **+.0170**
  （Δexp(K−D)=**−.0130**）。A 股「缩量回调=健康吸筹」不迁移美股大盘：上升趋势中放量回踩
  （买盘接盘）反而更强，缩量常为无量磨叽。→ **pullback 维持纯价格 +0.30，不加门**
  （`pullback_gate=False`）。反向门（奖励放量回调）疑似有效但同段样本内，转 R5.3 待 OOS。
- 涉及：`signals/quant/momentum.py`（indicators 暴露 `pullback_vol_ratio` 供诊断，不参与打分）。

#### R5.2 Breakout 量能扩张门（借鉴 volume_breakout）

- 背景：无量新高=假突破高发区；放量+强收盘=真突破。现有信号会给无量磨顶的票 +0.20。
- 现状：`momentum.py:111-114` 价近 52 周高（-3%~0%）→ special=max(special,+0.20)，
  **不看量、不看收盘强弱**。
- 需求：`breakout_vol_ratio = Volume.iloc[-1] / Volume.rolling(20).mean().iloc[-1]`，
  可叠加强收盘 `close_pos=(C-L)/(H-L)>0.7`：
  - vol_ratio 高（真突破）→ 维持/略增；
  - vol_ratio < 1.0（无量近高）→ 折减至 ~+0.05 或 0。
- ⚠️ 外部阈值（2× 5日均量）同样须美股回测重标定。
- 验收：同 R5.1；额外报告**被过滤的无量突破数量**（假信号削减是主要价值）。
- **🟢 结论（2026-07-24 因子回测，15,404 breakout 事件）：通过——已 merge，thr=1.5×。**
  放量 KEEP(≥1.5×) fwd10 win **.611**/exp **+.0385** ≫ 无量 DEMOTE(<1.0×) win .570/exp +.0165
  （Δexp(K−D)=**+.0220**）；且随阈值**单调**（1.5/2.0/2.5×→exp +.0385/+.0395/+.0414），
  是信号非噪声的判据。选 **1.5×**（分离度×样本量最优：KEEP n=1812≥100；2.0/2.5× 分离更大但
  KEEP population 缩到 713/338，因子发火频率不足）。价值=**过滤 9,000+ 无量近高假突破**
  （该组 exp 显著更低）。→ `breakout_gate=True, breakout_thr=1.5`（momentum 默认）。
  ⚠️ 具体小数随 cache 快照变动（如全量扫描刷新后 KEEP exp≈+.036、DEMOTE≈+.016），
  **稳健的是分离度与单调性**，非某位小数。
- **code-review 加固（2026-07-24）**：① 修正末窗量能缺失(NaN)误降 bug——bo_ratio 非有限时
  回退纯价格 +0.20（原 NaN 比较恒 False 会误判为 +0.05）；② **移除弱收盘 close_pos 降档**
  （未进回测 A/B 且实测中性，违背严格门纪律 → 删，close_pos 仅保留为诊断）；③ shipped **三档
  单调性获证**：DEMOTE<1.0(+0.05) exp+.016 → MID[1.0,1.5)(+0.10) exp+.021 → KEEP≥1.5(+0.20)
  exp+.036，单调 ✓（中间档 +0.10 由此有据，非拍脑袋）。
- 涉及：`signals/quant/momentum.py`（`_special_signal` breakout 分支；`breakout_vol_ratio`/`close_pos` 诊断暴露）。

#### R5.3 底部放量反转 + 反向 pullback 门（本期缓议，记录在案）

（a）bottom_volume（跌幅>15% + 量>3×均量 + 阳线/长下影=衰竭反转）属**反转/抄底**逻辑，与 momentum
因子的**趋势延续**取向相悖；其自然归宿是缠论 b1（下跌末端一买）的确认层——但 Depth 决策已排除
触碰 55% 本体，且 b1 是实证最弱买点（35.3%）。**本期不做**。
（b）**反向 pullback 门**（R5.1 副产物）：美股数据显示放量回调前向更强（与 A 股相反），
奖励放量回调疑似有效——但结论来自与 breakout 相同的 127 只/2021-2026 段，**属样本内**；
在 OOS（新时段/新票池，或 forward_tracker 积累样本外）确认前**不 ship**，防幸存者偏差重演。

#### R5.4 因子级回测门 Factor-Level Backtest Gate（strict gate 落地机制）

- 背景：现有 P7 回测撮合的是**缠论信号**，量化 sleeve 不独立产生交易——无法直接回答
  「量能门是否提升了 pullback/breakout 的质量」。需要**因子级 as-of 回测**。
- 需求：
  1. 逐日重放 momentum special 信号（**仅用 ≤当日数据**，发射逻辑与实盘共用，复用 R1.3 纪律）；
  2. 标注前向 5/10/20 交易日收益作 label；
  3. 对比**纯价格版 vs 量能门版**：胜率 / 期望 / 信号数 / 假信号率（被过滤信号的前向收益应显著更差）；
  4. 阈值重标定：在 0.6/0.7/0.8（pullback）与 1.5×/2.0×/2.5×（breakout）网格上选美股最优。
- **merge 条件**：量能门版在胜率或期望或精确度上优于纯价格版，且信号数具统计意义
  （单臂 ≥ ~100 信号）。未过门则如实记录、不 merge（允许只 merge 通过的那一个门）。
- 实施路径：先 scratchpad 研究脚本产出 go/no-go 证据（写回本节验收记录）；过门后固化为
  `backtest/factor_eval.py` 供未来因子复用。
- **✅ 落地（2026-07-24）**：数据源=`cache/market_data.db` ~127 个 OHLCV blob（2021-07~2026-07，
  ~1253 日/只），向量化 as-of 滚动统计（位置 t 的 rolling 仅用 ≤t 数据，无前视），~2.8 万事件，
  前向 5/10/20 日收益。**breakout 门过、pullback 门证伪**（见 R5.1/R5.2 结论）。已固化为
  `backtest/factor_eval.py`（`python -m backtest.factor_eval` 复现）。
- **code-review 加固（2026-07-24）**：① `_selfcheck` 由「仅纯价格触发」升级为**并断言 pb/bo 量能比率
  两路一致**（120/120 抽样），覆盖 `rolling(3,min_periods=1)` ≡ `tail(3).mean(skipna)` 语义等价，
  防向量化/逐日发射漂移；② 样本诚实：cache 键为哈希、blob 不带 ticker 无法 allowlist，QQQ/SPY 等
  基准 ETF 可能混入 ~2/127（^VIX 无量能已被 OHLCV 过滤剔除），量级可忽略、不改单调性结论。
- 涉及：`backtest/factor_eval.py`（新）、`signals/quant/momentum.py`。

#### R5.5 诚实边界 Honest Bounds（反 overclaim，必读）

- **三层稀释**：momentum 占 quant 30%，quant 占 final 10% → 单因子对 final_score 杠杆
  **≤0.03**（与 R4.3 RSI 结论同构）。R5 的价值在**因子自身精确度、横截面排序质量、假信号
  削减**，不在评级剧变——**禁止宣称"提升系统胜率"之类大词**，验收只对因子级指标负责。
- 外部项目零回测零验证，其阈值是 A 股散户经验值——**只借"看量"这个维度，不借任何数字**。
- 成交量数据走 yfinance `auto_adjust=True`（拆股已调整）；个别公司行动导致的量能异常
  由 20 日滚动均值天然稀释，不另做清洗。
- 基本面一致性（growth_quality 的 rev/profit/cashflow 同向加分）想法有价值但**无 PIT 历史
  不可回测**（R4.4 既定原则：yfinance 快照禁入回测）——已被 Scope 决策排除，如实记录不做。

#### R5.6 样本外(OOS)前向验证 breakout 门（2026-07-24 落地）

- 背景：R5.2 的 breakout 门是 **in-sample**（127只/2021-2026）调参，有过拟合风险；in-sample
  的 pullback 门证伪已警示照搬危险。真正确认需**样本外前向证据**——不能用同段历史 cache 回填
  （=再污染），须从上线日向前逐日累积。
- 机制：`backtest/factor_forward.py`（与 `forward_tracker` 平行、共库独立表）。每日在 `main.py`
  记录 **final_pool 内每一个 breakout special 触发**（不限评级——breakout 是 10% 量化子信号极少
  翻动最终评级，Buy 门下样本会饿死；剔除 benchmark ETF 防污染），标注 bo_ratio 与 KEEP/MID/
  DEMOTE 桶；满 20 交易日后计前向 5/10/20 日收益；报告写 `output/{date}/r5_breakout_oos.md`。
  发射逻辑复用实盘 `_special_signal`（新增 `breakout_trig`/`pullback_trig` aux 标记，免重算触发、
  防重复漂移），与 in-sample as-of 同口径。
- 判定：KEEP/DEMOTE 桶各 ≥30 才下结论——单调且 KEEP−DEMOTE>0 → **OOS 确认**；KEEP−DEMOTE≤0
  → **OOS 背离**（提示复核过拟合）；否则"待累积"。**诚实**：门槛前不下任何结论，避免小样本噪声
  当证据。首批约需 20 交易日成熟，统计意义的确认需数月积累（对齐三层稀释下"因子精确度"的定位）。
- 涉及：`backtest/factor_forward.py`（新）、`main.py`（log/eval/report 三处挂载）、
  `signals/quant/momentum.py`（`_special_signal` 暴露 trig 标记）。

### R6 — 因子挖掘与验证体系 Factor Mining & IC/IR·Quantile Validation（2026-08-02 立项）

#### R6.0 立项背景与范围决策 Background & Scope

**来源**：用户观察系统整体表现平庸，提出**基于有效因子开发策略**，给出三类因子挖掘想法 +
一套正式的因子有效性验证标准，要求把通过验证的因子以加权贡献并入现有缠论×宏观×量化管线。

**明确约束**（用户强调，写入 PRD）：
- **不写批量爬虫，但可程序化一站式获取**（2026-08-02 用户澄清）——用 Python 包 / 官方文档化文件端点 / 官方 API
  **自动获取** FINRA/SEC 数据，与现有 yfinance / FRED / Finnhub **同类**（直接 GET 日期参数化的已发布数据文件或
  调官方 API，**非 HTML 抓取、非跨页链接遍历**）；缓存 / 重试 / 新鲜度纪律与现有源一致。**不设计批量网页爬虫。**
  目标：`python main.py` 从取数 → 分析 → 出结果全自动，**无手动下载 / 导入步骤**（保留本地文件回退用于离线 / 降级）。
- **沿用既有节奏**：本轮唯一交付为本 R6 章节（零源码改动），待 user 确认后才进入编码。

**三类因子想法**：
1. **FINRA/SEC 卖空数据因子**：日度做空量 / Short Volume Ratio / FTD（交割失败）→ 做空情绪因子 + 逼空风险因子。
2. **缠论原生衍生因子**：把缠论计算转成可量化特征——中枢水平、中枢震荡幅度、背驰强度（MACD 面积 / 力度衰减比）、
   价格相对摆动高低点位置、段结构形态。
3. **候选因子加工**：滚动均值、同比 / 环比、横截面排序、去极值、标准化。

**验证标准**（用户指定，是本轮核心交付物）：
- **IC / IR**：每因子与前向 N 日收益的相关（逐日横截面 Spearman RankIC → 序列均值 / 标准差 = IR），检验稳定性；
- **分位回测**：按因子值分组，检验高低分位收益分化、单调性、跨年份稳定性；
- **相关性剪枝**：剔除高相关因子，保留逻辑独立、低相关因子。

**已锁定的两项范围决策**（用户确认）：
1. **卖空数据 = 纳入，但在现有股票池上过门**。诚实前提：FINRA 日度做空量是**做空流量代理**（≠ 做空兴趣 short
   interest），且逼空类因子历史上主要在小盘 / 难借券 / 易逼空票有效；在流动性极佳的大盘科技池上 IC 可能近零。
   → 建 loader + 因子、走 IC / 分位门；**过则并入，不过则如实记录不 merge**。
2. **缠论原生特征 = 落成 quant sleeve 内新「结构因子」，55% 缠论本体零改动**。缠论引擎内部已算出的数值**只读
   暴露**、组装为横截面结构因子，不改缠论信号判定逻辑（延续 R5「不碰 55% 本体」边界）。

**防复发纪律**（承接 R1.3 幸存者偏差、R5 量价门教训）：每条候选因子落成**确定性因子**，回测采用与实盘共用的
as-of 发射逻辑，**过 R6.1 验证门后才进入实盘打分**；in-sample 通过者再经 R6.7 样本外前向验证闭环。

#### R6.1 因子验证实验室 Factor Lab（先建，是一切因子的门）

- 背景：现有 P7 撮合缠论信号、`factor_eval` 只做 momentum special 的布尔触发事件门——**都无法回答「任意连续
  因子与前向收益的横截面相关性、分位单调性、跨年稳定性、彼此冗余度」**。用户的验证标准需要一个通用因子实验室。
- 现状：仓库无 IC / IR、无 `qcut` 分位组合、无 Spearman RankIC、无相关性剪枝（仅 `relative.py:62` 有
  `.rank(pct=True)` 横截面百分位，是 RankIC 思路的「排序半边」，未含 IC 测量半边）。
- 需求（新 `backtest/factor_lab.py`）：
  1. **面板**：用 `backtest/ml_backtest.py:build_dataset()` 产出的 `date×ticker×factor` 长表（保留 ticker+date，
     宽于 14 活跃池、多年）——**不用** `factor_eval.load_price_blobs`（cache blob 哈希、丢 ticker，无法横截面 IC /
     跨年分组）。前向 5 / 10 / 20 日收益标注复用 `factor_eval` 的 `close.shift(-h)/close-1`。
  2. **IC / IR**：逐日横截面 Spearman RankIC → 序列均值 = IC、均值 / 标准差 = IR；逐年 IC 表检验稳定性。
  3. **分位回测**：因子 `qcut` N 组、组均前向收益、high−low 价差、单调性检查（复用 R5.2 三档单调 idiom）、逐年分位稳定。
  4. **相关性剪枝**：候选因子两两 `|corr|` 矩阵，阈值剔除冗余、保留低相关独立因子。
  5. **`_selfcheck`**：断言向量化因子值 ≡ 实盘 `compute_*_score` 逐日 as-of（防发射漂移，沿用 `factor_eval._selfcheck` 纪律）；
     跨年 IC 稳定性复用 `ml_backtest.run_walk_forward` 的 HOLD_DAYS embargo / purge（防标签重叠泄漏）。
- 验收：`_selfcheck` 绿；对既有五因子回算 IC / 分位作为**基线校准**（自证实验室口径正确）；报告可复现
  （`python -m backtest.factor_lab`）。
- **merge 判据**（所有后续因子共用）：|IC| 显著非零 **且** 分位单调 **且** 跨年稳定 **且** 与现有因子低相关；
  任一不满足 → 如实记录、不 merge。
- 涉及：`backtest/factor_lab.py`（新）；复用 `ml_backtest.build_dataset` / `factor_eval` / `relative.py`。

#### R6.2 缠论原生结构因子 Chan-Native Structure Factor（harvest+expose → quant sleeve）

- 背景：缠论引擎内部算出的**结构数值**（背驰力度、中枢几何、价格位置）目前被塌成布尔门或只进 `reasoning` 日志、
  在构造 `ChanSignalResult` 前丢弃——这些正是用户想要的可量化缠论特征。
- 现状（探查确认）：
  - MACD 面积比 `curr_area/prev_area`：`_detect_buy/_detect_sell` 内只用于 `<0.8` 布尔门，比值本身被丢弃；
  - A股 `_trend_strength`（0..1，CCI+BOLL 位置）：已算，只进日志；
  - 中枢震荡幅度 `(ZG-ZD)/mid`、中枢年龄 `index[-1]-pivot.end_date`、价格带内位置 `(price-ZD)/(ZG-ZD)`、
    到摆动高低点距离：均可从已暴露的 `current_pivot` / `Stroke.high/low` 一行导出。
- 需求：
  1. `ChanSignalResult` **只读暴露**结构数值：`macd_area_ratio`、`pivot_width_pct`、`pivot_age_td`、
     `price_pos_in_band`、`dist_to_swing_low/high`——改 `_detect_*` 返回元组或让 `compute_chan_signal` 复算两次
     `_stroke_area`（廉价 masked `hist.abs().sum()`），**不改任何信号判定逻辑与既有字段**；
  2. 新 `signals/quant/structure.py`：把上述数值组装为横截面结构分（统一 `(score:float, ind:dict)` 签名，
     接 `factor_engine._run`）。
- 验收：R6.1 门下结构因子 IC 显著、分位单调、跨年稳定、与现有五因子低相关才 merge；无缠论结果 / 缺中枢时中性回退（0）；
  暴露字段不改动既有 US / A股 决策与回测逐票输出（回归对比 final_score 无非预期漂移）。
- **🔴 结论（2026-08-02 因子级 as-of 回测，R6.1 门）：6 特征全 REJECT，不 merge。**
  6 特征（`chan_area_ratio` 背驰面积比 / `chan_pivot_width` 中枢震荡幅度 / `chan_pivot_age` 中枢新鲜度 /
  `chan_price_pos` 带内位置 / `chan_dist_swhi/swlo` 到摆动高低点距离）经 `signals/quant/structure.py` 逐日
  as-of 重放（复用 chan 公开构建器，**未改 chan_signal.py**）入 R6.1 门。**首门 29 只（偏科技）**：
  `chan_dist_swlo` 近失（IC +.059/fwd10 win .61/**t +2.07**，仅栽严格单调 ρ .70）、`pivot_width`/`price_pos`
  稳定正 IC。**按用户决策扩宇宙到 78 只跨行业再门**（本应收窄 CI 抬 t）——实测反而**削弱**：`chan_dist_swlo`
  t 2.07→**1.40**、IC .059→.034；`pivot_width` t 1.36；`price_pos` t .68。**扩宇宙证伪「瓶颈是横截面太窄」
  假设**：t 随宇宙变宽而降 = 信号非稳健、集中于窄科技同群（sector artifact）、跨板块不泛化。
  → 结构数值维持缠论引擎内部（bool 门 / 日志），**不暴露为量化因子、不改 `chan_signal.py`（55% 本体零改）**。
  `structure.py` 留作验证器 + 诚实记录（同 `factor_eval` 保留 pullback 证伪）。**meta 洞察**：R6.1 门（跨年 +
  overlap 调整 t + 宽宇宙）恰好拦下一个 naive 29 只下 t=2.07「像赢家」的窄样本假象——门起作用了。
- 涉及：`signals/quant/structure.py`（新，隔离验证器，`chan_signal.py` **零改动**）；未过门 → 不接入
  `factor_engine.py`、不暴露 `ChanSignalResult`。

#### R6.3 FINRA/FTD 做空情绪 & 逼空风险因子（现有池上过门）

- 背景：做空流量与交割失败是与价 / 量正交的另一维信息（空头拥挤度、逼空燃料）。
- 现状：系统无卖空 / FTD 数据源。
- 需求（**程序化 DataSource，一站式自动，非爬虫**，同 `FREDSource` / `YFinanceSource` 模式）：
  1. 新 `data/short_data_source.py`——接受共享 `SQLiteCache`，`make_key("finra_shvol", date)` / `make_key("sec_ftd", yyyymm, half)`
     缓存 + `with_retry` 重试 + TTL（FINRA 日频短、FTD 半月频长）：
     - **FINRA 日度短量**：GET `https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt`（管道分隔，
       表头 + 尾行，列 `Date|Symbol|ShortVolume|TotalVolume|Market`），`pd.read_csv(sep="|")` 去尾行 → **单份全市场
       文件含全 ticker** → 按 `Symbol` 取本池票；**Short Volume Ratio = ShortVolume/TotalVolume**。（备选：官方
       `developer.finra.org` Reg SHO Query API。）
     - **SEC FTD 半月度**：GET `cnsfails{YYYYMM}{a|b}`（zip，管道分隔，列含 settlement date / CUSIP / **SYMBOL** /
       issuer / price / fail quantity），解压解析 → 按 `SYMBOL` join（文件自带 symbol，**无需 CUSIP 映射**）；
       **⚠️ SEC 要求声明 `User-Agent`（`app_name email`）**、公平访问 ≤10 req/s（本用量每日数份、重缓存，天然远低于限）。
  2. 新鲜度走 `FREDSource` 模板（`STALE_LIMIT_DAYS`：FINRA 日频 ~5d、FTD 半月频 ~30d + `staleness()` + `degraded` 列表）；
     注册进 `DataPipeline.__init__`、经 `fetch_all` 透传。**本地文件回退**（离线 / 端点故障）复用 `ashare_loader`
     discover→validate→normalize→dict pattern 作降级路径，主路径为自动获取。
  3. 新 `signals/quant/short_sentiment.py`——做空情绪（Short Volume Ratio 的滚动均值 / z-score / 环比变化）、
     逼空风险（高 SVR × 高 FTD × 价升 = 逼空前兆），统一 `(score, ind)` 签名接入。
- ⚠️ **无前视纪律**：FINRA 日文件 T 日盘后发布、FTD 首半月末发布 / 次半月约次月 15 日发布（~2 周 + 滞后）→ 因子按
  **发布可得日**对齐（lag），与全系统「末行 = t-1 已完成」口径一致；回测按可得日消费，禁用未来发布的数据。
- ⚠️ **诚实前提**（锁定决策 1）：大盘科技池上做空流量占比小、套利充分，IC 可能近零；**过 R6.1 门则并入，
  不过则如实记录不 merge**——不为「有数据源」而强行纳入。
- 验收：source 单测（端点 mock + 缺文件 / 脏值 / 尾行容错、User-Agent 头断言、可得日 lag 断言）；R6.1 门 IC / 分位 /
  相关性；数据缺失中性回退 + `degraded` 标记进报告（复用 R3.2 降级纪律）；端到端 `main.py` 全自动取数无手动步骤。
- 涉及：`data/short_data_source.py`（新）、`data/pipeline.py`（注册 + `fetch_all` 透传）、`config`（端点 / TTL / UA 常量）、
  `signals/quant/short_sentiment.py`（新）、`main.py`（摄入串接）。

> 🔴 **结论（2026-08-03 R6.3 落地——仅 FINRA，FTD 全缓；全 REJECT，不 merge）**
> 「仅 FINRA，FTD 全缓」路径已建（`data/short_data_source.py` 程序化自取 + 可断点续传 +
> `signals/quant/short_sentiment.py`）。SVR 宽表 **647 日 × 78 票**（2024-01-02→2026-07-02，
> 面板 48,620 行 / 627 横截面日），过 R6.1 门（IC/IR + 分位 + 跨年 + 相关性）——**4 特征全 REJECT**：
>
> | 特征 | fwd10 IC | t | 分位单调ρ | 跨年同号 | max\|corr\| | 判定 |
> |------|---------|---|----------|---------|-----------|------|
> | svr_level  | −0.016 | −0.96 | **−1.00** | 0.67 | 0.68 | REJECT（t/IC 不达） |
> | svr_mean20 | −0.001 | −0.08 | −0.30 | 0.67 | 0.66 | REJECT（近零） |
> | svr_z20    | −0.021 | −1.26 | −0.90 | **1.00** | 0.72 | REJECT（t 不达 + 与 chg5 冗余） |
> | svr_chg5   | −0.008 | −0.48 | −0.70 | 1.00 | 0.72 | REJECT（弱 + 冗余） |
>
> **强洞察（负结果但方向真实）**：做空流量因子的**符号全为负且经济上正确**——SVR 越高（做空占比越大）→
> 前向收益越弱（空头压力=看跌）；且**跨年同号 0.67~1.00、svr_level 分位完美单调 ρ=−1.00**，说明这个
> 边**真实存在、方向稳定、非噪声偶合**。但在流动性极佳、套利充分的**大盘池**上其**幅度太小**（\|IC\|≤0.021）、
> **t 从不达 \|2\|**（最强 svr_z20 fwd10 t=−1.26），恰好**证实预注册的诚实前提**（锁定决策 1）：大盘做空流量
> 占比小 → 信号存在但不显著，不足以过门。svr_z20↔svr_chg5 相关 0.72 亦触发剪枝。**如实记录不 merge**
> （同 R5 pullback / R6.2 结构因子）。DataSource 与因子代码留作诚实记录 + 未来若纳入小盘/难借券池的复用底座。
> **若未来 SEC FTD 端点在本环境可达**，可补逼空风险因子（高 SVR × 高 FTD × 价升）——那是做空维度理论上
> 更可能在大盘偶发逼空事件里显著的一支，本期因 sec.gov TLS 不可达全缓。

#### R6.4 候选因子变换工具箱 Candidate Transforms

- 背景：用户想法 3（滚动均值 / 同比环比 / 横截面排序 / 去极值 / 标准化）是**服务前述因子的加工层**，非独立因子。
- 需求：新 `signals/quant/transforms.py`——滚动均值、环比 / 同比、横截面 `rank(pct=True)`（复用 `relative.py` idiom）、
  去极值（winsorize / MAD）、标准化（z-score / rank）。纯函数、无副作用、可组合。
- ⚠️ **诚实边界**：变换**仅施于有真实历史的序列**（价 / 量 / 做空流量）；**基本面同比 / 环比排除**——yfinance
  快照无 PIT 历史、不可回测（R4.4 既定：基本面快照禁入回测），如实记录不做。
- 验收：单元测试（各变换数值正确 + 无前视：位置 t 仅用 ≤t 数据）；被 R6.2 / R6.3 因子按需调用。
- 涉及：`signals/quant/transforms.py`（新）。

> 🔴 **结论（2026-08-03 R6.4 落地——工具箱 + 6 派生候选过门；全 REJECT，但现 R6 最强近失 amihud_20）**
> `signals/quant/transforms.py` 已建：时序变换（`roll_mean/roll_std/roll_z/pct_change_n/diff_n/yoy/roll_rank_pct`，
> 尾窗无前视）+ 横截面变换（`cs_rank/winsorize/mad_winsorize/cs_zscore`，单日跨票、**切勿沿时间施用**）。
> `_selfcheck` 绿（数值正确 + 时序变换截断不变）。**不为空建工具**——用它从价/量组出 6 派生候选
> `DERIVED_CANDIDATES` 过 R6.1 门（面板 ~1200 横截面日、2020→2025）：
>
> | 候选 | fwd10 IC | t | fwd20 t | 分位ρ | 跨年同号 | max\|corr\| | 判定 |
> |------|---------|---|--------|-------|---------|-----------|------|
> | mom_z_20_60  | −0.001 | −0.05 | +0.25 | +0.60 | 0.67 | 0.28 | REJECT（近零） |
> | mom_rank_120 | −0.009 | −0.46 | −0.83 | +0.20 | 0.60 | 0.28 | REJECT |
> | vol_z_20     | +0.007 | +0.56 | +0.41 | +0.70 | 0.67 | 0.58 | REJECT |
> | hl_range_z   | −0.000 | −0.01 | +0.11 | +0.30 | 0.50 | 0.56 | REJECT |
> | **amihud_20**| **+0.025** | +1.76 | +1.67 | +0.30 | **1.00** | **0.03** | REJECT（t/mono 不达） |
> | turnover_mom | +0.012 | +0.87 | +0.91 | +0.90 | 0.67 | 0.58 | REJECT |
>
> **强洞察——amihud_20 是 R6 全程最接近过门的一支，且与前两个负结果性质不同**：Amihud 非流动性
> （|ret|/美元成交额 的 20 日均）**IC 随前向单调升**（fwd5 +0.017 → fwd10 +0.025 已破 \|0.02\| → fwd20 +0.032）、
> **hit 升到 0.60**、**跨年同号 1.00**（符号完美稳定）、**与其余候选 max\|corr\|=0.03**（近乎正交、真独立信息）。
> 卡在两处：① **t 从不达 \|2\|**（1.61/1.76/1.67，显著性不足）；② **分位非单调**（ρ=0.30，Q1→Q5
> [+.0083 +.0054 +.0068 +.0057 **+.0150**]=只有最不流动的 Q5 尾部才跳）。经济解读正确：**流动性溢价**
> 在流动性极佳的大盘池上本就微弱、且集中在「相对最不流动」尾部而非平滑梯度 → 大盘上不过门是**预期内**，
> 但其**年际稳定 + 正交**强烈提示：**若未来纳入更广 / 更小盘宇宙，amihud_20 是最该优先重门的一支**
> （与 R6.2 结构因子「扩宇宙反而变弱」相反——那是窄群偶合，这是真溢价被大盘稀释）。⚠️ 注：表内 max\|corr\|
> 仅对派生候选集，**尚未对既有五因子测独立性**（因 t/mono 已不过门，暂不需）。**全 REJECT，不 merge**
> （同 R5/R6.2/R6.3）；工具箱留作 R6 加工层底座 + 未来扩宇宙时 amihud 重门的现成入口。
>
> 🔴🔴 **扩宇宙定向重门（2026-08-03，同日追加）——「稀释假设」被证伪，amihud_20 彻底 REJECT**：
> 隔离脚本把宇宙扩到 **143 票**（大盘 78 + 中小盘 65，实载；中位日成交额 **$10M→$4B、13,624× 分散**），
> 定向重门 amihud_20 + 首测其对既有五因子参照的独立性（面板 174k 行 / 1228 日 / 2021-08→2026-07）：
>
> | 口径 | fwd10 IC | fwd10 t | fwd20 t | 分位ρ | 跨年同号 | vs 五因子 max\|corr\| |
> |------|---------|---------|--------|-------|---------|---------------------|
> | 大盘 78 | +0.025 | +1.76 | +1.67 | +0.30 | 1.00 | —（未测） |
> | **广 143** | **+0.011** | **+0.70** | +0.62 | **+0.80** | 0.83 | **0.11**(dist_high252) |
>
> **决定性洞察——扩宇宙 *削弱* 而非增强 amihud，稀释假设错**：IC 近乎腰斩（+0.025→+0.011）、
> **t 更远离显著**（1.76→0.70）。**唯一变好的是分位单调（ρ 0.30→0.80）**——真流动性轴出现、小盘落入高-amihud 尾。
> 且**独立性证实**：对 mom(0.00)/reversal(0.01)/dist_high(0.11)/lowvol(0.11) 近乎正交，是真独立信息。
> **重解读**：大盘 78 上那点正 IC 并非经典流动性溢价（大盘间流动性无差异），而是 amihud 分子 |ret| 主导 →
> 其实在proxy **波动/风险**，恰好年际稳定；一旦放进真小盘，amihud 才真按流动性排序，但 **2021–2025 恰是
> 「Mag7 碾压小盘」制度、流动性/小盘溢价没被支付** → IC 与 t 双降。**结论：amihud_20 两个宇宙都不过门，
> 稀释假设证伪，R6 唯一活口关闭**。此为**正确的科学结果**：形成可证伪假设→扩宇宙检验→被数据推翻，门再次尽责。

#### R6.5 集成与权重分配 Integration & Weight Allocation

- 背景：过门的 survivor 因子须并入 quant sleeve，且不能把 momentum 等既有因子稀释到失效。
- 需求：survivor 经 `factor_engine._run` 接入，新增 `W_*` 常量并**重归一化**（保持 quant 内五 / 六 / 七因子权重和为 1）；
  相关性剪枝（R6.1）确保新增因子与既有因子逻辑独立、不重复计分。
- 验收：quant 权重和 = 1 断言；集成前后对未触发新因子的票 final_score 无漂移；背离 / 顶层 55/35/10 权重不受影响。
- 涉及：`signals/quant/factor_engine.py`（`W_*` + `_run` 接入 + 重归一化）。

#### R6.6 诚实边界 Honest Bounds（反 overclaim，必读）

- **三层稀释**：quant 占 final 10%，单个新因子对 `final_score` 的杠杆 **~≤0.01–0.03**（与 R4.3 RSI / R5.5 结论同构）。
  R6 的价值在**因子自身精确度、横截面排序质量、假信号削减**，**不在评级剧变**——禁止宣称「提升系统胜率」之类大词，
  验收只对因子级指标（IC / IR / 分位单调 / 相关性）负责。
- **不写批量爬虫**：官方数据经文档化文件端点 / 官方 API 程序化获取（与 FRED / yfinance 同类），非 HTML 抓取 /
  跨页遍历；一站式自动、无手动步骤，另留本地文件回退用于离线 / 降级。SEC 侧遵守其 `User-Agent` 与公平访问约束。
- **外部阈值须美股重标定**：任何借来的经验阈值（如 SVR / FTD 触发线）一律以本 IC / 分位门在美股数据上重定，不照搬。
- **卖空因子可能不过门**：大盘池上或近零 IC，如实记录不 merge（锁定决策 1）。
- **无 PIT 基本面不可回测**：基本面派生变换（growth 同比等）排除（R4.4 既定）。

#### R6.7 样本外前向验证 OOS Forward Validation（survivor）

- 背景：in-sample 通过 ≠ 样本外有效（R5 的 pullback 门 in-sample 疑似有效、实为证伪且方向相反，已警示照搬危险）。
- 需求：R6.1 过门的 survivor 因子仿 R5.6 `backtest/factor_forward.py` 建**无回填**样本外累积（绝不从历史 cache 回填 =
  再污染），每日在 `main.py` 记录 final_pool 内因子取值 + 前向收益，逐月累积确认 / 证伪 in-sample 结论。
- 验收：样本量门槛前不下结论（沿用 R5.6「各桶 ≥N 才判定」纪律）；报告写 `output/{date}/`。
- 涉及：`backtest/factor_forward.py`（扩展或平行新表）、`main.py`（挂载）。

### 第七批 R7 — 突破 R6「单信号 t<2」的三条路（2026-08-06；全 REJECT，但收敛出统一机制）

R6 总账 3 类 × 0 幸存，共同败因**同一个**：无条件、单信号、线性的因子在高效大盘上 |t|<2。
R7 用三条互不相同的路攻这个 t<2，均复用 R6.1 `factor_lab` 门；**全 REJECT 不 merge**，但三路
汇成一个**一致的经济机制**（这是 R6 未产出的）。三模块 `signals/quant/{composite,regime_ic,anomalies}.py`
留作诚实记录 + 复用底座。

> 🔴 **R7.1 弱正交信号合成 `composite.py`——分散化数学对，但没有 4 个同号弱信号可分散**
> 依据：k 个各自弱（IC≈c、符号对）且近正交（ρ̄≈0）的信号等权合成，理论 IC≈c·√k/√(1+(k-1)ρ̄)——
> c≈0.02、k=4、ρ̄≈0 → ≈0.04（两倍门槛）。预注册**一个**合成（4 轴等权横截面 rank 均值，防子集择优）。
> 实测（76 票 / 1228 日）：
>
> | 成分（orient 高=预期高前向） | fwd10 IC | t | 与预注册符号 |
> |------|---------|---|------|
> | amihud_20（非流动性+） | **+0.0281** | +1.94 | ✓ 对 |
> | reversal_5（反转+） | +0.0034 | +0.16 | 死（大盘反转已消） |
> | lowvol_20（低波+） | **−0.0539** | −1.88 | ✗ **反号** |
> | dist_high252（趋势+） | −0.0066 | −0.26 | 死 |
> | **composite** | **−0.0136** | **−0.64** | REJECT（跨年 0.50、mono−0.90） |
>
> **强洞察——ρ̄=0.177 分散化前提满足（漂亮地正交），却毁于符号冲突**：等权把 +0.028 的 amihud
> 与 **−0.054 的 lowvol** 平均 → **相消而非分散**，且 lowvol 幅度更大 → 合成被拽成负。分散化数学
> 只在**成分同号**时放大；本宇宙我根本没有 4 个同号弱信号——只有 amihud 一个真信号 + 三个死/反号。
> **事后翻正 lowvol 符号再合成 = in-sample snooping，不做**。真正的发现是 **lowvol 在 2021–2026 大盘上反号**。
>
> 🔴 **R7.2 制度条件 IC `regime_ic.py`——预注册格子全不过门，但暴露教科书级制度结构（零新增数据）**
> 把每日横截面 IC 按 VIX 四档（FRED VIXCLS，与宏观主轴同源）分桶，预注册每因子应生效的制度、只裁决该格：
>
> | 因子 | calm<15 | normal15-25 | stress25-35 | panic>35 | 预注册格判定 |
> |------|---------|-------------|-------------|----------|------|
> | amihud_20 | +0.027(t.80)★ | +0.013 | +0.064(t1.68) | +0.205(t3.83,n=8) | REJECT（★calm t.80） |
> | reversal_5 | +0.017 | −0.012 | +0.043(t.77)★ | +0.278(n=8)★ | REJECT |
> | lowvol_20 | −0.023 | −0.049 | −0.097★ | **−0.466(t−3.14,n=8)**★ | REJECT（全档负） |
> | dist_high252 | +0.013★ | −0.002★ | −0.049 | −0.330 | REJECT |
> | mom_roc20 | −0.002★ | +0.015★ | −0.078 | −0.309 | REJECT |
>
> **强洞察（探索性，多重比较下不作 merge 依据）**：① **amihud 的 IC 随 VIX 单调升**
> （calm+.027→stress+.064→panic+.205）——非流动性溢价**在压力期被重定价**，我预注册的 calm 反而最弱、
> 经济先验搞反了；但 panic **n_days=8**（overlap 后≈1 独立观测）统计上一文不值，stress t=1.68 仍 <2。
> ② **动量在 panic 崩溃**（dist_high−0.33 / mom_roc−0.31）= 教科书 momentum crash（Daniel-Moskowitz）。
> ③ **lowvol 全档负、越恐慌越负**——低波异象在本样本**彻底反转**（panic 日 fwd10 = V 型反弹，高 beta 领涨）。
>
> 🔴 **R7.3 大盘可存活异象 `anomalies.py`——文献异象也全 REJECT，且指向同一机制**
> 换用文献里在大盘也存活的异象（对 SPY 滚动中性化残差；orient 高=预期高前向），逐个过门（78 票 / 1213 日）：
>
> | 异象 | fwd10 IC | t | 分位ρ | 跨年 | 判定 |
> |------|---------|---|-------|------|------|
> | max_lottery（−MAX 彩票） | −0.0383 | −1.52 | **−1.00**（完美单调） | 0.67 | REJECT（−IC=高MAX胜、反先验；t<2） |
> | idio_skew（−残差偏度） | −0.0049 | −0.37 | −0.60 | 0.40 | REJECT（死） |
> | idio_vol（−残差波动） | −0.0441 | **−1.78**（R7 最近失） | −0.90 | 0.80 | REJECT（−IC=高波胜；t<2） |
> | resid_mom（残差动量+） | +0.0240 | +1.09 | +0.30（U 型） | 0.80 | REJECT（符号对但非单调 + t<2） |
>
> `idio_vol ↔ max_lottery` 相关 0.60 = **同一支**。max_lottery 分位完美单调（ρ−1.00）、idio_vol 跨年 0.80——
> 结构干净，只卡 t 与符号。
>
> 🔴🔴 **R7 统一收官——三路 × 0 幸存，但十余个因子变体收敛为一个机制**：R6+R7 在 2021–2026 大盘上反复
> 触到的**同一件事**——**高波动 / 高 beta / 不流动的名字跑赢了低风险的名字**（amihud+、idio_vol/max_lottery/lowvol
> 全部指向「高波胜」），且该暴露**随 VIX 单调增强**（amihud calm→panic 梯度 = 风险在压力期被重定价）。
> 它是**被补偿的 beta（系统性风险敞口），不是 alpha**——量级 t≈1.5–1.9、单调、跨年稳，但**从不到 t≥2**，
> 而系统**本就用 VIX 四档仓位门控在管这块 beta 敞口**（<15→100% … >35→0%）→ 对 10% quant sleeve **无正交新信息可加**。
> 这不是失败，是**正确的科学结果**：三条独立的路把「大盘因子为何弱」收敛到单一可解释机制，且证明这块暴露
> **归属宏观 VIX 门、不归 quant sleeve**。
>
> **唯一前瞻性假设（预注册待 OOS，绝不现在 merge）**：**「非流动性 / 低流动 beta 溢价集中于高 VIX 制度」**
> ——amihud 的 calm→panic 单调梯度。翻案需三件事齐备：① 足够多 panic 日（现 n=8 无用）；② 非-Mag7 制度样本
> （承接 R6「Mag7 碾压小盘」的制度约束）；③ R5.6 式无回填 OOS 确认。此为 R6 amihud 线索的正确延续：
> 它是**压力-beta 因子**，非独立非流动性 alpha。

### 5.1 优先级与依赖

```
R1.1 R1.2 ──────────────► 立即（正确性，独立可测）
R1.3 ───────────────────► 立即启动（重算胜率基线，产出物影响 R4.2）
R2.1 R2.2 ──────────────► 第二批（达成每日 cron 目标）
R3.1 R3.2 R3.3 ─────────► 第三批（可靠性；R3.2 依赖 R2.1 的报告改动点）
R4.1~R4.5 ──────────────► 第四批（R4.2 依赖 R1.3 新基线）
R5.1 R5.2 ──────────────► 第五批（2026-07-20 立项；R5.4 回测门通过是 merge 前置，
R5.4 ───────────────────►   R5.3 缓议待 R5.1/R5.2 结论）
R6.1 ───────────────────► 第六批（2026-08-02 立项；R6.1 验证门是 R6.2/R6.3 merge 前置，先建）
R6.2 R6.3 R6.4 ─────────►   R6.2/R6.3 过门→R6.5 集成；R6.7 OOS 对 survivor 闭环
```

---

## 6. 非功能需求 Non-Functional Requirements

| 维度 | 需求 |
|------|------|
| **调度** | 支持 cron/launchd 每交易日盘前运行一次；非 TTY 全自动完成（R2.1）；非交易日快速退出（R2.2）；单次全流程 ≤15 分钟（当前池规模）。 |
| **幂等性** | 同日重复运行：报告/快照覆盖生成；模拟组合不重复成交（现有回滚快照机制保留，R1.1 不得破坏）；`signal_state.json` 迟滞 streak 同日重跑不重复累加。 |
| **数据降级策略** | 任何外部数据缺失都必须：走缓存兜底 → 显式 DEGRADED 标记 → 报告可见（R3.2）；**禁止**静默中性化后照常给买入建议不留痕。 |
| **可观测性** | 每日日志含：数据源命中/失败计数、宏观降级项、池变更、组合成交；异常退出返回非 0 退出码供调度器告警。 |
| **可复现性** | 同日同缓存两次运行输出一致（R3.1 消除油/美元直连的不确定性）。 |
| **无前视纪律** | 实盘路径维持 t-1 收盘基准；回测路径结构/指标一律 as-of 截断（R1.3）；基本面模块禁入回测（R4.4 文档化）。 |
| **兼容性** | 所有改动不得影响 A 股共用模块行为（`hysteresis_core`、`portfolio_core`、`fractal/stroke/pivot`）；改共用模块需同步跑 `run_ashare_backtest.py` 基线对比。 |

---

## 7. 验收与回归 Acceptance & Regression

### 7.1 修复验证矩阵

| 需求 | 验证方式 |
|------|---------|
| R1.1 | 单测三场景（首日卖点保仓 / 次日确认清仓 / 破止损直通卖出）+ 组合状态 JSON 断言 |
| R1.2 | 单测「宏观抬正的卖点票」止损方向断言；重放最近一日全池决策，无 stop>entry 记录 |
| R1.3 | 3 票新旧事件序列 diff 报告；ML 回测重跑，新胜率写回文档 |
| R2.x | 非 TTY dry-run（`echo | python main.py --non-interactive`）退出码 0 + 产物齐全；节假日用例 |
| R3.x | 断网/断 FRED mock 测试：报告出现 DEGRADED 区块、进程不崩溃 |
| R4.x | 背离票报告权重显示 70/20/10；评级迁移矩阵人工签核 |
| R5.x | 因子级 as-of 回测对比报告（纯价格 vs 量能门：胜率/期望/信号数/假信号率）；无 Volume 回退行为不变断言；`echo \| python main.py --non-interactive` 端到端退出码 0 |
| R6.x | `factor_lab._selfcheck` 向量化≡实盘绿；每因子 IC/IR + 分位单调 + 跨年稳定 + 相关性剪枝报告（过门才 merge、不过如实记录）；short/structure loader 缺数据中性回退 + `degraded` 标记；集成后 quant 权重和=1、未触发票 final_score 无漂移；`echo \| python main.py --non-interactive` 端到端退出码 0 |

### 7.2 全局回归基线（每批改动后必跑）

1. `python main.py`（TTY 交互路径）完整跑通，`output/{date}/` 产物齐全、与改动前逐票对比 final_score 无非预期漂移；
2. `python run_ml_backtest.py` 胜率与改动前对比（R1.3 会刻意改变基线，其余批次不得变）；
3. A 股侧 `python run_ashare_backtest.py` 六阶段胜率不变（守护共用模块）；
4. `git status` 确认无缓存/状态文件误提交；`graphify update .` 保持知识图谱同步。

---

## 8. 附录 Appendix

### 8.1 全参数表（阈值/权重/常量及其位置）

| 参数 | 值 | 位置 |
|------|----|------|
| 主权重 W_CHAN/W_MACRO/W_QUANT | 0.55 / 0.35 / 0.10 | `decision/scorer.py:27-29` |
| 背离权重 | 0.70 / 0.20 / 0.10 | `decision/scorer.py:32-34` |
| 背离触发 DIV_CHAN_MIN / DIV_QUANT_MAX | +0.30 / −0.10 | `decision/scorer.py:36-37` |
| 共振/逆风阈值 | chan≥+0.30；macro≥0 / ≤−0.15 | `decision/scorer.py:40-41` |
| 评级阈值 | Buy≥0.60 / OW≥0.30 / Hold≥−0.30 / UW≥−0.60 | `decision/rating.py:17-22` |
| VIX 四档 | <15 / 15-25 / 25-35 / >35 → 100/70/40/0% | `signals/macro/regime.py:13-28` |
| 宏观子权重 | VIX 0.35 / Yield 0.20 / External 0.30 / Bucket 0.15 | `signals/macro/macro_signal.py:17-20` |
| 外部因子归一锚 | 油 0.15 / 加息 1.5 / 美元 0.05 / 通胀 0.5（目标 2.5%） | `external_factors.py:36-39,217` |
| 量化五因子权重 | 0.15/0.25/0.30/0.20/0.10 | `factor_engine.py:17-21` |
| 缠论基础分 | b1 +0.50 / b2 +0.75 / b3 +0.65；s1 −0.50 / s2 −0.65 / s3 −0.70 | `chan_signal.py:11-12` |
| 背驰面积比 | curr < prev × **0.8**（且必须创新极值） | `chan_signal.py:332,368` |
| 趋势/盘整背驰权重 | ×1.15 / ×0.85（仅 b1/s1） | `chan_signal.py:230-236` |
| 右端防护 | STROKE_CONFIRM_BARS=2；HIGH_VOL_PCT=6%(+2根)；CONFIRM_DAYS=2；MAX_STALE_DAYS=5 | `chan_signal.py:31-34`、`hysteresis_core.py:20-21` |
| 笔最小间隔 MIN_BARS | 4 根处理K | `stroke.py:22` |
| 信号新鲜度 | 末笔 15 日历日内（A股为 12 交易日） | `chan_signal.py:424` |
| 止损缓冲 / R_MAX / 止盈比 | 1% / 15% / 2:1 | `chan_signal.py:240`、`risk_overlay.py:24-25` |
| VIX 兜底止损 | calm 7% / neutral 8% / tense 6% / panic 5% | `risk_overlay.py:18-23` |
| B3 理想窗口 | ZG×0.99 ~ ZG×1.03 | `risk_overlay.py:136-137` |
| 筛选阈值 | ADD≥0.40 / REMOVE≤−0.20 / TopN=5 / 价≥$5 / 日成交额≥$20M / 市值≥$5B | `signals/screening.py:32-39` |
| 数据窗口 | 实盘 800 天 / 回测 1825 天 / 缠论最少 200 根 | `settings.py:28,31`、`chan_signal.py:395` |
| 缓存 TTL | 价格 24h / info 7d / FRED 24h / 财报 90d | 各 source 模块 |
| 组合参数 | 初始 $100,000 / lot=1 | `config/stocks.py:14-15` |

### 8.2 术语表 Glossary

| 术语 | 含义 |
|------|------|
| 包含关系 GG/DD | 相邻K线互相吞没时按方向合并（上取高高、下取低低） |
| 笔 Stroke | 相邻异类分型连线，端点间隔 ≥4 根处理K |
| 中枢 Pivot (ZG/ZD) | ≥3 笔重叠区间；ZG=min(笔高)、ZD=max(笔低) |
| 背驰 Divergence | **创新极值 + MACD 面积衰竭**（<80%）双条件，缺一不可 |
| b1/b2/b3 | 一买（下跌背驰）/ 二买（中枢下沿回踩，近似）/ 三买（突破 ZG 后回踩不破） |
| 定笔 | 末笔终点分型再过 N 根处理K才确认（反右端重画） |
| 分型停顿 | 分型后收盘站上/跌破第三根K极值才确认（缠论第四章） |
| R 比率 | (入场−止损)/入场；>15% 视为离支撑太远，降级 Hold |
| 共振/逆风 | 缠论看多时宏观同向/敌对的双主轴互验标记（不重复计分） |
| DEGRADED（拟新增） | 外部数据缺失/过期时的显式降级标记（R3.2） |

### 8.3 审计方法说明 Audit Methodology

- 范围：`main.py` 全链路 + `signals/{chan,quant,macro}` + `decision/*` + `data/*` + `report/*` + `backtest/forward_tracker`；
- 方法：3 路全库结构化扫描 + 对 `chan_signal.py`、`scorer.py`、`risk_overlay.py`、`rating.py`、`hysteresis.py`、`macro_signal.py`、`main.py` 关键段**逐行人工核验**；文中所有 file:line 均对应基线 commit `7b0c609` 工作树；
- 本次审计**未修改任何源码**，唯一产物为本 PRD。
