# 预注册：F5 回购强度 · 样本外前向记录

> **写定时间：2026-10-27（首条记录之前）**｜基线 commit `806612a`
> 上游：`PRD-factor-validation.md` §3.4 / §5 P3 —— 回购金额在 yfinance 只有约 5 季、无 PIT 历史，**只能从今天起向前记**（同 R11 EPS 修正）。
> ⚠️ **价值来源：写在任何前向数据之前。** 记录开始后不得修改本文件任何一节；改口径只能在文末追加，且自改动日起**重新计数**。

---

## 1. 要回答的问题

草案 F5：以「回购金额 / 自由流通市值」「回购金额 / 日均成交额」双阈值判定回购对价格的支撑。
本记录器回答：**在 78 只大盘股上，回购强度高的名字随后 20 个交易日是否跑赢？**

事前先验（写下来，不影响裁决）：大盘股的**毛**回购收益率预测力在文献里弱且不稳；
被反复验证的是**净股本变化**（Pontiff & Woodgate 2008）—— 科技股的回购很大一部分只是抵消股权激励
（MSFT 季度回购约 $4.6B 对 SBC 约 $3.1B）。故把净缩股作为事前指定的副口径一并记录。

## 2. 冻结口径（每个记录日，as-of 当日可见数据）

| 量 | 定义 |
|---|---|
| `ttm_rep` | yfinance `quarterly_cashflow` 的 `Repurchase Of Capital Stock` 最近 4 个**连续**季度之和取正（相邻期末间隔 80–100 天才算连续）。**该行缺失**而现金流表本身有 ≥4 个连续季度 ⇒ 记 0 并打 `rep_row_missing=1`（2026-10-27 实测缺行的 8 只 AMZN/TSLA/INTC/ARM/PFE/AMGN/BA/SNDK 均为近期无回购的公司）；不足 4 个连续季度 ⇒ NaN |
| `float_cap` | `info.floatShares` × 记录时最后一根收盘 |
| `adv20` | 最近 20 根 K 的 `Close × Volume` 均值 |
| **`bb_float`（主）** | `ttm_rep / float_cap` |
| `bb_adv` | `(ttm_rep / 252) / adv20` —— 回购若均匀执行，每日约占成交额的比例 |
| `net_shrink` | `−(DilAvgShares[q0] / DilAvgShares[q−4] − 1)`，`quarterly_income_stmt` 的 `Diluted Average Shares`，需 5 个连续季度 |
| `dual_flag` | `bb_float` 与 `bb_adv` **都 ≥ 当日横截面中位数**（草案没给阈值数字；用中位数事前写死，**不调**） |

另存原始分量供审计与日后重定义（**这张表本身就是本项目唯一的回购 PIT 历史**）：
`q_end`（所用最新季度期末）、`float_shares`、`shares_out`、`close`、`ttm_sbc`、`rep_row_missing`。

- 宇宙：`VALIDATION_UNIVERSE`（78 只）；频率：距上次记录 ≥ 5 个日历日；挂 `main.py`。
- **入场锚 = `logged_date` 之后第一根收盘**（快照是运行时现取的，同 R11 修正记录器的理由）。
- 标签：fwd10 / **fwd20（主）** / fwd60，均自入场锚起算。
- 已知局限（事前承认）：yfinance 季报行比公司申报**滞后数周**（例：UNH 10/13 已发 Q3，表里最新仍是 6/30）—— 记录的是「当日 yfinance 可见」，`q_end` 留痕；回购 TTM 在一个季度内基本不变，因子近乎季度频率。

## 3. 统计量与裁决

- 每个记录日一个横截面 Spearman IC（≥ 8 名）；t = IR × √n_eff，n_eff = n × min(1, 5/h)。
- `dual_flag`：每个记录日 `mean(fwd20 | flag) − mean(fwd20 | ¬flag)`，同样的 t。
- **预注册方向**：`bb_float`、`bb_adv`、`net_shrink` 的 IC > 0；`dual_flag` 差值 > 0。
- **只在固定日期正式查看**：每年 10 月最后一个交易日之后的第一次运行（首次 2027-10），最多 8 次。
  主口径 `bb_float × fwd20`：**t ≥ +2.5 ⇒ 前向确认；t ≤ −2.5 ⇒ 方向证伪**；之间 ⇒ 继续；第 8 次仍在之间 ⇒ **弃权**。
  副口径（`bb_adv`、`net_shrink`、`dual_flag`、其他 horizon）只报告，**不能替代主口径**。
- 主口径 IC 日 < 30 不判方向。报告每次运行显示的累计数字是**进度不是裁决**。
- 稳健性（只报告）：剔除 `rep_row_missing=1` 的名字后的主口径 IC。

## 4. 处置

- 前向确认 ⇒ **只**讨论接入战术 sleeve `quant_score`（另过 `factor_lab` 独立性门与 A/B），**永不进核心 sleeve、三轴、加速器**。
- 证伪或弃权 ⇒ 停记录、代码保留；**原始分量表继续有价值**，不删。

## 变更记录

（空）
