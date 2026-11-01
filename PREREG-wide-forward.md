# 预注册：宽宇宙前向记录（R11 修正 / F5 回购 / F4′ 残差动量 × S&P 500）

> **写定时间：2026-10-29**，早于首条宽宇宙记录｜基线 commit `3caed88`
> 上游：R13 复盘（`PRD-signal-conditional.md` 之后的方向 ②）。三个前向记录器的瓶颈是**横截面宽度**，不是时间：
> 78 只时单日 IC 噪声约 1/√78 ≈ 0.11，淹没 0.03 量级的真效应。
> ⚠️ 记录开始后，不得修改本文件任何一节；改口径只能在文末追加，且自改动日起**重新计数**。

---

## 1. 与已有记录器的关系

- 78 只的三个记录器（`factor_forward_revision` / `factor_forward_buyback` / `factor_forward_resmom`）**原样继续**，
  按各自的预注册（R11 docstring / `PREREG-f5-forward.md` / `PREREG-f4-forward.md`）各自裁决，一个字不改。
- 本文件登记的是**同三个假设在更宽横截面上的独立记录**：因子定义逐字复用那三个模块的函数，只换宇宙。
- 两边结论冲突时**并列报告，不仲裁**。宽宇宙功效更高，但宇宙不同（见 §6 局限 2）。

## 2. 冻结口径

| 项 | 冻结值 |
|---|---|
| 宇宙 | `config/sp500_constituents_2026-10-29.csv`（Wikipedia「List of S&P 500 companies」2026-10-29 抓取，503 只，`.`→`-`），sha256 `4b8ea078569b80ae1fdd688d30748f95ac1f6ee7b9b311c79cd1dc92f3fb3642`；**并上 ARM**（78 只里唯一不在指数内的），共 504 只。成分**冻结于今日**，此后指数调整不跟进 |
| 行业 | CSV 的 `gics_sector`（ARM 记 Information Technology） |
| 修正因子 | `factor_forward_revision.revision_factors(get_eps_revisions(t))`：`rev30_fy`（主）/ `rev90_fy` / `breadth_fy` / `rev30_q` / `breadth_q` |
| 回购因子 | `factor_forward_buyback.buyback_snapshot(t, price)`：`bb_float`（主）/ `bb_adv` / `net_shrink` + 原始分量 |
| 残差动量 | `factor_forward_resmom.f4p_last(price, qqq_ret)`（底层 `prereg_factor_v1.make_resid_mom`，L 252 / E 756） |
| 代码冻结 | `factor_forward_revision.py` `11729c4c…649f`、`factor_forward_buyback.py` `ac9e5660…c0`、`factor_forward_resmom.py` `9237572f…1d71`、`prereg_factor_v1.py` `959255e5…b23`；任一不符 ⇒ 本次**不记录**并告警 |
| 频率 | 距上次宽宇宙记录 ≥ 5 个日历日；由独立入口 `python forward_wide.py` 触发（单次约 15 分钟，不挂进 `main.py`） |
| 入场锚 | **`logged_date` 之后第一根收盘**，三个因子统一。⚠️ 这对 F4′ 比 78 只记录器（锚在 `bar_date` 之后）晚约一天，事先承认 |
| 标签 | fwd5 / fwd10 / fwd20 / fwd60，原始收益 |

## 3. 主假设与统计量

| 因子 | 主口径 | 方向 |
|---|---|---|
| R11 修正 | `rev30_fy` × fwd20 | IC > 0 |
| F5 回购 | `bb_float` × fwd20 | IC > 0 |
| F4′ 残差动量 | `f4p` × fwd10 | IC > 0 |

- 每个记录日一个横截面 Spearman IC（≥ 30 名才计入）；`t = IR × √n_eff`，`n_eff = n × min(1, 5/h)`。
- **副口径（只报告）**：
  - ① 行业中性 IC：因子与收益先在 GICS 行业内去均值；
  - ② 78 只子集上的 IC；
  - ③ 其他因子与 horizon。

## 4. 裁决（写死）

- **只在固定日期正式查看**：每年 10 月最后一个交易日之后的第一次运行（首次 2027-10），最多 8 次。
- 某因子**前向确认**须同时满足：
  - (a) 主口径 t ≥ +2.5（多次查看纠正，同 F4′ / F5 预注册）；
  - (b) 行业中性 IC 同号；
  - (c) 78 只子集 IC 同号。
- 主口径 t ≤ −2.5 ⇒ 方向证伪。其余 ⇒ 继续；第 8 次仍未定 ⇒ **弃权**。
- 主口径 IC 日 < 30 不判方向。报告里每次显示的累计数字是**进度，不是裁决**。
- 三个因子各自独立裁决；这是 3 次试验，计入项目试验计数。
- **处置**：前向确认 ⇒ **只**讨论接入战术 `quant_score`，须另过 `factor_lab` 独立性门与 A/B；**永不进核心 sleeve**。证伪或弃权 ⇒ 停记，表保留。

## 5. 功效（假设，写在看数据之前）

- 504 只时单日 IC 抽样噪声约 1/√504 ≈ 0.045；因子收益本身的日间波动还会再加一层噪声，所以单期 IR 不会按 √(504/78) 等比放大。
- 假设单期 IR 在 0.3–0.4（78 只下 F4′ 实测 0.155）：
  - fwd20：每年 n_eff ≈ 13，t ≈ 1.1–1.4/年 ⇒ 约 **3–5 年**到 2.5；
  - fwd10（F4′）：每年 n_eff ≈ 26，约 **2–4 年**。
- 比 78 只的 6–7 年快，但**仍可能以弃权收场**；这一点事先承认。

## 6. 事先承认的局限

1. **成分冻结**：今后被剔出指数的名字照记，直到取不到价格；记录失败数逐次报告。
2. **宇宙不同 ≠ 同一个假设的更大样本**：S&P 500 含大量非科技、小一些的公司；在宽宇宙成立的效应，在 78 只（科技重仓）上可能不成立（R6 amihud 是反方向的先例），故设门 (c)。
3. **yfinance 稳定性**：单次约 2,000 次请求，可能被限流；失败名字当次缺失，不补录、不回填。
4. 季报行滞后、回购行缺失记 0 等口径与 `PREREG-f5-forward.md` 相同。

## 变更记录

（空）
