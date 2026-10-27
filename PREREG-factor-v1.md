# 预注册：因子验证 v1（F4 残差动量 · F3 跳空回补率）

> **写定时间：2026-10-27（任何 F3/F4 回测运行之前）**｜基线 commit `17f77e4`
> 基线 `backtest/factor_lab.py` sha256 = `12eed0facf12d21e884d1ddb10ff28c108922c7c54742dd3d7d5ef8e87eb5c64`
> 基线 `signals/quant/ear.py` sha256 = `55ef100ca2698c6843feb3dd5fc6a965a81cc9353f5cb549c4bcbf38958d3e21`
> 上游：`PRD-factor-validation.md`（§7 用户裁定 2026-10-26：78 只裁决、主窗口 2022-01 起、PASS 只进战术前向记录）。
> ⚠️ **价值来源只有一条：写在结果之前。** 运行后不得修改本文件任何一节；
> 若必须调整口径，只能在文末「变更记录」追加，写明原因与时间，**且该次结果降级为探索性**。

---

## 1. 要回答的问题

1. **F4**：剥离 QQQ β 之后的残差动量，在 78 只上对未来 10 日收益是否有**独立于已有价格因子**的横截面预测力？
2. **F3**：财报跳空后的「当日未回补程度」是否预测随后收益，且**不被 R11 EAR 张成**？

**不回答**：四只个案（UNH/MSFT/NVDA/META）各自「好不好」。它们只出事件表，不参与裁决（N=4 的 IC 无意义，见 PRD §3.1）。

## 2. 数据（冻结）

| 项 | 值 |
|---|---|
| 宇宙 | `factor_lab.VALIDATION_UNIVERSE`（78 只，基线 sha 下的列表原样） |
| 基准 / 行业 ETF | QQQ；XLK XLF XLV XLI XLY XLC XLP XLE；EAR 对照用 SPY（同 R11） |
| 价格下载 | 2019-01-02 起（给 F4 留 ≥504 个交易日暖机），**一次性下载后钉死**到 `output/prereg_factor_v1/prices.pkl`，P1 首次运行时记录其 sha256 并写入 RESULTS.md；之后所有计算只读这份 |
| 财报日 | 与 R11 同源：`DataPipeline().yf.get_earnings_history(tk)`；同样一次性钉死到 `output/prereg_factor_v1/earnings_dates.json` |
| 信号日窗口（主） | **2022-01-03 → 价格末日**（fwd 标签不足的尾部自然为 NaN 丢弃）。RESULTS.md 必须写出实际起止日 |
| 子窗口（只报告，不裁决） | 2025-01-02 → 价格末日 |

**已知偏差（事前承认，不修）**：宇宙是今天的名单（幸存者偏差，同 R10/R11）；行业标签是今天的 yfinance `sector`，不是 PIT（窗口内无影响较大的重分类，GOOGL/META 迁 XLC 在 2018）。
ARM / SNDK / CRWD / VRT 历史较短，F4 暖机不足的日期为 NaN，按 `MIN_ROWS` / `MIN_NAMES_PER_DATE` 原规则处理。

### 2.1 行业 → ETF 映射（冻结，用于 F4b）

| sector（yfinance） | ETF | 名字 |
|---|---|---|
| Technology | XLK | AAPL ADBE ADI AMAT AMD ARM AVGO CRM CRWD CSCO DELL FTNT IBM INTC INTU KLAC LRCX MRVL MSFT MU NOW NVDA ORCL PANW QCOM SNDK TXN |
| Financial Services | XLF | AXP BAC BLK C GS JPM MA MS SCHW V WFC |
| Healthcare | XLV | ABBV ABT AMGN DHR JNJ LLY MRK PFE TMO UNH |
| Industrials | XLI | BA CAT DE GE HON LMT UNP VRT |
| Consumer Cyclical | XLY | AMZN HD LOW MCD NKE SBUX TSLA |
| Communication Services | XLC | CMCSA DIS GOOGL META NFLX VZ |
| Consumer Defensive | XLP | COST KO PEP PG TGT WMT |
| Energy | XLE | COP CVX XOM |

合计 27+11+10+8+7+6+6+3 = **78**，无缺失。

## 3. 因子定义（冻结）

记号：`C` 收盘、`O` 开盘、`r_t = C_t/C_{t−1} − 1`，全部按单票自身的位置索引；t 时刻的值只用 ≤ t 的数据。

### 3.1 F4 残差动量（主）

1. 在 t 时刻，用 `[t−L+1, t]` 的日收益做 OLS：`r_i = α + β·r_QQQ + ε`（L = 回看长度）。
2. 用**这一次**估出的 α、β 计算同一窗口内的残差 `ε_s`。
3. `F4 = Σ ε_s (s ∈ [t−L+1, t−21]) / std(ε_s, s ∈ [t−L+1, t−21])`
   —— 跳过最近 21 天（避开短期反转），按残差波动标准化（Blitz, Huij & Martens 2011 的写法）。
4. 窗口内有效样本 < 0.9·L 则为 NaN。

**F4b（稳健性）**：同上，回归改为 `r_i = α + β₁·r_QQQ + β₂·r_ETF(sector) + ε`。
科技股的 XLK 与 QQQ 高度共线，β 分配不稳但残差仍有定义 —— 只看残差，不解读 β。

### 3.2 F3 跳空回补率（主）

对每个财报事件 d（与 R11 同源同口径），`d0` = d 当天或之后的第一个交易日：

1. **反应日** `k` = `{d0, d0+1}` 中 `|O_k / C_{k−1} − 1|` 更大的那一天（不依赖盘前/盘后公告时点）。
2. `g = O_k / C_{k−1} − 1`；`fill = (O_k − C_k) / (O_k − C_{k−1})`，截断到 [0, 1]。
3. 若 `|g| ≥ G`：`F3 = sign(g) · (1 − fill)`；否则该事件不产生值。
4. 从 **d0+2** 起生效 `HOLD` 个交易日（k 最晚是 d0+1，故 d0+2 起无前视），被下一事件覆盖；窗口外为 NaN。

含义：+1 = 向上跳空且收盘守住全部缺口；0 = 当日回补完；−1 = 向下跳空且未回补。
**砍掉的分层**：PRD 草案里的「是否上修 capex 指引」—— 无 PIT 数据，不做。

### 3.3 参数网格（冻结；主规格之外的只作稳健性）

| 因子 | 网格 | **主规格** |
|---|---|---|
| F4 | L ∈ {126, 252} | **L = 252** |
| F4b | L = 252 | —（稳健性） |
| F3 | G ∈ {2%, 4%} × HOLD ∈ {20, 60} | **G = 2%，HOLD = 20** |
| 标签 | fwd5 / fwd10 / fwd20（`factor_lab.forward_returns`） | **fwd10** |

**裁决只看主规格**（F4 一个、F3 一个，共两次主检验）。非主规格只要求**与主规格同号**；
若主规格 REJECT 而某个非主规格过门，**不得改判**，只能写进 RESULTS.md 的「探索性」节，要用须另起预注册。

## 4. 裁决门（主规格，全部 AND）

| 门 | 判据 |
|---|---|
| **G0 无前视** | `factor_lab.assert_asof_consistent`：F4 全宇宙随机抽 12 只 × 10 点、F3 有事件的票随机抽 12 只 × 10 点，**最大差 = 0** |
| **G1 裸因子** | `factor_lab.evaluate_factor(panel, col, others=REFERENCE_FACTORS, primary_h=10)` 返回 `PASS`（基线 sha 下的 IC_MIN 0.02 / T_MIN 2.0 / MONO_MIN 0.90 / YEAR_SIGN_MIN 0.60 / CORR_MAX 0.70 原样） |
| **G2 不冗余** | 对参照因子逐日横截面残差化后的残差 IC：`|t| ≥ 2.0` 且与裸 IC 同号。参照集：F4 = 4 个 `REFERENCE_FACTORS`；F3 = 4 个参照 + `ear`（R11 口径原样）。同时报告 pooled adj-R²（R10 写法），只作描述 |
| **G3 扣成本** | 组合：每 10 个交易日调仓一次（不重叠），**信号日 t 收盘算出、t+1 收盘成交**，做多 Q5 / 做空 Q1、等权；每次调仓按实际换手 × **0.10% 往返**扣成本。净夏普的 **block bootstrap（块长 6 个调仓期，5,000 次）95% CI 下限 > 0** |
| **G4 样本外一致** | ① 从 2024-01 起按 6 个月切测试窗口（前 24 个月为训练期；主规格不在训练期调参，训练期只用来选非主规格的排名作参考），每个窗口边界前后各留 10 天 embargo；**≥ 60% 的测试窗口 IC 与全样本同号**。② **剔除 UNH / MSFT / NVDA / META 四只（草案构思来源）后重跑 G1 的 IC 与 t：符号不变且 |t| ≥ 2.0** |

- **对 PRD §4.3 的一处偏离（事前说明）**：purged K-fold 不做 —— 本次没有任何需要拟合的模型参数（主规格固定），K-fold 无对象；重叠标签的问题已由 `ir_stats` 的 overlap 调整 t 与 G4 的 embargo 处理。
- **多重检验**：两次主检验沿用 `T_MIN = 2.0`，不另做 Bonferroni；网格和子窗口只作稳健性，不能救活主规格。这是事前接受的显著性水平，RESULTS.md 照写。
- **测不准写「弃权」**：若主规格面板有效日 < 250（F3 事件太稀）或 G3 bootstrap 无法计算，写「弃权」，不写 REJECT。

## 5. 处置（写死）

- **PASS** ⇒ 只挂成战术 sleeve 的**前向记录器**（同 R5.6 / R11 修正的做法），满足预注册的样本外 IC 日数后才谈接入 `quant_score`；**不进任何真金决策、不进核心三轴、不是加速器扳机**。
- **REJECT** ⇒ 代码保留、默认关闭，不接实盘（R10/R11 先例）。
- 无论结果如何，四只个案出事件表（PRD §4.5），写明「n 很小，不构成证据」。

## 6. 产出

`output/prereg_factor_v1/RESULTS.md`，必须包含：实际信号日起止、价格与财报日钉死文件的 sha256、
每个门的数值与 PASS/FAIL、主规格分位曲线、跨年 IC、子窗口、网格稳健性表、剔四只结果、G3 的换手/毛夏普/净夏普/最大回撤、四只个案事件表。
运行前先核对本文件 sha256 与 `PRD-factor-validation.md` 里记录的一致。

## 变更记录

### 变更 1（2026-10-27，P1 运行之后；用户选定方案 1）⇒ 本变更下的 F4 结果为**探索性**

- **原因**：§3.1 把 α/β 估计窗与残差求和窗设成同一窗口 [t−L+1, t]。带截距 OLS 残差在估计窗内和为零，
  故 Σε[t−L+1, t−21] ≡ −Σε[t−20, t]，冻结的 F4 机械地等于「近一月残差反转」（P1 实测逐位相等；与 `mom_roc20` corr −0.73）。
  这是预注册的定义错误，不是代码偏离。v1 口径的 REJECT 原样保留（见 `output/prereg_factor_v1/RESULTS.md`）。
- **新口径 F4′**（Blitz, Huij & Martens 2011 的写法）：
  1. α、β 在 **[t−755, t]（756 个交易日 ≈ 36 个月）** 上带截距 OLS 估计；有效样本 < 0.9×756 则为 NaN。
  2. 残差求和窗不变：**[t−L+1, t−21]**，`F4′ = Σε / std(ε)`（同一求和窗）。L 网格 {126, 252} 与主规格 L=252 不变。
  3. F4b′ 同理：QQQ + 行业 ETF 双因子，估计窗 756。
- **其余一律不变**：宇宙、钉死价格（`prices.pkl` sha256 `6e662ec5…c985`，2019-01-02 起，不重新下载）、
  信号窗 2022-01-03 起、标签、G0–G4 判据与阈值、处置。
- **事前承认的代价**：2022 年初的信号日需要 2019-01 起的 756 天暖机，覆盖可能略低于 v1；短历史名（ARM/SNDK/CRWD/VRT）缺值更多。
- **裁决地位**：即使 F4′ 过全部门，也只记为「探索性 PASS」，接入前向记录器之前须另立预注册、只用之后的新数据确认。
