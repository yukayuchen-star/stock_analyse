# US Equity Quant & Chan Theory Research System
# 美股量化分析与智能投资系统

A daily-run research pipeline that produces tradable US equity candidates by combining
**Chan Theory structure** (缠论), **macro regime gating**, and **cross-sectional quant factors**.

> **Honest scope | 诚实定位**
>
> This is a **faithful daily-bar, single-level Chan Theory engine** — not a complete
> multi-level Chan system. Both the US and A-share tracks have **daily bars only**; intraday
> data is unavailable, which is a hard data constraint, not a code gap.
>
> - **Implemented faithfully | 已忠实实现**: K-line inclusion (包含关系), fractals (分型),
>   strokes (笔), pivots (中枢 ZG/ZD), divergence (背驰 — requires *new extreme + MACD area
>   exhaustion*), buy/sell points b1/b2/b3 & s1/s2/s3, fractal-stop confirmation (分型停顿法),
>   trend vs consolidation classification, R-ratio risk control, right-edge stability guards.
> - **Simplified by data constraint | 按数据约束简化**: line segments (线段, approximated by
>   stroke→pivot), level recursion / nested intervals (级别递归/区间套 — daily only, with a
>   weekly SMA filter), sub-level confirmation for b1.
> - Pivot direction and `b2` (actually "pullback to pivot lower band") are approximate semantics.
>
> The reference standard for all Chan logic is `缠论.md` + `images/` in this repo.

---

## Quick Start | 快速开始

```bash
pip install -r requirements.txt

# Create .env with your API keys (no .env.example is tracked — .env* is gitignored)
cat > .env <<'EOF'
ALPHA_VANTAGE_KEY=your_key
FINNHUB_KEY=your_key
FRED_KEY=your_key
EOF

python main.py                 # US selection → output/YYYY-MM-DD/
```

`main.py` is interactive on a TTY (pool editor prompts) and fully schedulable otherwise:

```bash
python main.py --non-interactive                      # cron path: no prompts, exit 0 on
                                                      # non-trading days (NYSE calendar built in)
python main.py --non-interactive --auto-adopt-adds 3  # auto-adopt Top-3 add candidates
                                                      # (default 0 = record-only; removes are
                                                      #  always record-only, never auto-executed)
python main.py --date 2026-07-14                      # backfill label (data still fetched as-of now)

# Example crontab: every weekday 18:30 local, after US close data settles
# 30 18 * * 1-5  cd /path/to/stock_analyse && ./.venv/bin/python main.py --non-interactive
```

`watchlist_us.txt` (gitignored, same format as the A-share `watchlist.txt`) force-includes
tickers into the analysis pool in both modes.

| Command | Purpose |
|---------|---------|
| `python main.py` | US daily selection, reports, paper portfolio, forward tracking |
| `python run_ml_backtest.py` | US ML backtest (LightGBM, walk-forward, purged) |
| `python mainA.py` | A-share selection (secondary track) |
| `python run_ashare_backtest.py` | A-share Chan backtest by signal type |

---

## Architecture | 四层架构

```
Layer 4  Report      output/YYYY-MM-DD/{TICKER}.md + daily_summary.md
         报告输出    + portfolio.md (paper trading) + backtest_summary.md + forward_validation.md
             ▲
Layer 3  Decision    Score fusion → 5-tier rating → VIX position gate → risk overlay
         决策层      scorer.py → rating.py → risk_overlay.py → hysteresis.py → strategy.py
             ▲
Layer 2  Signals
         信号层
         ├── Chan  (55%)   Primary axis 1 — structural timing: 分型→笔→中枢→买卖点 (when to buy)
         ├── Macro (35%)   Primary axis 2 — regime gate: VIX 4-tier + rates + oil/USD/inflation
         └── Quant (10%)   Supporting — cross-sectional ranking: 5 factor groups (which one)
             ▲
Layer 1  Data        yfinance + Finnhub + FRED (+ Alpha Vantage) + SQLite cache
         数据底座    data/pipeline.py, data/cache.py
```

---

## Scoring | 打分公式

```python
final_score = 0.55 × chan_score + 0.35 × macro_score + 0.10 × quant_score
```

**Two primary axes in parallel, quant as a supporting role.** Rationale: Chan supplies the
structural entry, macro decides whether the environment permits it, and quant only breaks ties
across candidates. Chan and momentum-ML capture **opposite edges** (ML-high-confidence names
show *lower* Chan win rates), so the two must run in parallel — never chained as filters.

**Divergence branch | 背离规则**: when Chan is strong but quant is weak
(`chan ≥ +0.45` and `quant ≤ −0.10`), structure wins over statistics →
`0.70 × chan + 0.20 × macro + 0.10 × quant`. After the R4.2 recalibration only b3-grade
structure can clear the 0.45 gate.

**Chan ↔ Macro consistency** (tagged, not double-counted):

| Tag | Condition | Meaning |
|-----|-----------|---------|
| `resonance` 共振 | chan ≥ +0.30 and macro ≥ 0 | Both axes bullish — signal more credible |
| `headwind` 逆风 | chan ≥ +0.30 and macro ≤ −0.15 | Chan bullish but macro hostile — size down |

### Chan buy-point scores | 缠论买点分值

Recalibrated 2026-07-14 (**R4.2**, US only) against unbiased backtest baselines:

| Point | Score | Meaning | Evidence |
|-------|-------|---------|----------|
| **b3** (三买) | **+0.75** | Breakout above ZG, pullback holds | 53.3% win rate — the only type with positive expectancy (≈+0.60R at 2:1) |
| **b2** (二买) | **+0.40** | Pullback to pivot lower band | 35.6% — near the 33.3% breakeven line at 2:1 |
| **b1** (一买) | **+0.35** | Downtrend divergence (left-side) | 35.3% — near breakeven; requires new low + MACD area exhaustion |
| s1 / s2 / s3 | −0.50 / −0.65 / −0.70 | Sell points | Not recalibrated — backtest is long-only, no sell-side data |

Net effect: **b1/b2 alone only reach Hold**; they need macro tailwind ≥ +0.25 to reach
Overweight. b3 alone reaches Overweight, and with macro tailwind reaches Buy.

`b1`/`s1` are additionally scaled by trend type (趋势 ×1.15 / 盘整 ×0.85), and any long signal
is halved when the weekly trend is down.

> The top-level 55/35/10 weights were **kept** during R4.2 — no evidence supported a specific
> alternative — and the recalibration was done *within* the Chan types instead.
> A-share pins the original scores (`BUY_SCORES_ASHARE`) and is unaffected.

### Quant factors | 量化五因子

```python
quant_score = (0.15 × fundamental + 0.25 × trend + 0.30 × momentum
             + 0.20 × relative_strength + 0.10 × volume)
```

| Group | Weight | Metrics |
|-------|--------|---------|
| Fundamentals 基本面 | 15% | Revenue/EPS growth, ROE, gross margin, D/E, PEG (yfinance `info`) |
| Trend 趋势 | 25% | SMA20/60/200 arrangement, ADX14, EMA20 slope |
| Momentum 动量 | 30% | ROC20, MACD histogram, RSI14, KAMA, pullback/breakout |
| Relative strength 相对强度 | 20% | Excess return vs QQQ/SPY + in-bucket percentile rank |
| Volume 量价 | 10% | OBV trend, VWMA20 deviation |

---

## Right-Edge Stability | 右端稳定性

The last stroke sits at the right edge and is the least stable part of any Chan structure — new
bars can repaint it and flip a signal overnight, especially on high-volatility names. Three guards:

| Guard | Mechanism | Parameter |
|-------|-----------|-----------|
| **A — Stroke confirmation** 定笔 | The terminal fractal must survive N more processed bars before a signal fires | `STROKE_CONFIRM_BARS = 2` |
| **C′ — Volatility guard** 波动率 | Names averaging ≥6% daily range get +2 confirmation bars and a `HIGH_VOL` flag | `HIGH_VOL_PCT = 0.06` |
| **B — Hysteresis** 迟滞 | A long→exit flip needs 2 consecutive confirming days before the portfolio liquidates; prior state older than 5 days resets | `CONFIRM_DAYS = 2` |

Stop-loss breaches and VIX panic bypass hysteresis — risk control takes priority.

---

## Risk Control | 风控

**VIX four-tier position gating | VIX 四档仓位门控**

| VIX | Regime | Position limit | Chan entry threshold |
|-----|--------|----------------|----------------------|
| < 15 | calm 平静 | 100% | b2 / b3 |
| 15–25 | neutral 中性 | 70% | b1 / b2 |
| 25–35 | tense 紧张 | 40% | **b3, or b1 + multi-level resonance** (others ×0.5) |
| > 35 | panic 恐慌 | 0% | Stand aside |

Note: "multi-level resonance" here means **daily + weekly SMA20** agreement (0–2), the only
level pair available on daily data — *not* intraday levels.

**Structural stops | 结构止损** — stops come from Chan structure, never `price × fixed %`:

- b1/b2 → stop = last stroke low × 0.99; b3 → stop = pivot upper band ZG × 0.99
- Take profit = price + (price − stop) × 2 (**2:1 R/R**)
- Long stops are direction-validated (`stop < price`), else fall back to a VIX-based percentage
- **`R_MAX = 0.15`**: if R = (price − stop) / price > 15%, entry is too far from support →
  downgrade to Hold, position zeroed

**B3 entry window**: ideal entry is ZG×0.99 ~ ZG×1.03 (the golden pullback zone). If price is
already above ZG×1.03 the window has passed — the report flags `B3_WINDOW_PASSED` and quotes
the current price instead. "Wait for it to fall back to ZG" is wrong: by then the structure has
changed and the b3 signal is most likely gone.

---

## Rating Map | 5 档评级

| final_score | Rating | Note |
|-------------|--------|------|
| ≥ **0.50** | **Buy** | b3 + macro tailwind (≥ +0.25) clears this — semantics: strong structure × confirmed environment |
| 0.30 – 0.50 | **Overweight** | b3 alone, or b1/b2 + macro tailwind |
| −0.30 – 0.30 | **Hold** | b1/b2 alone land here |
| −0.60 – −0.30 | **Underweight** | |
| < −0.60 | **Sell** | |

VIX caps the rating: `tense` → max Overweight, `panic` → max Hold.

---

## Outputs | 输出物

```
output/YYYY-MM-DD/
├── {TICKER}.md            # Per-name decision: score breakdown, Chan structure, entry/stop/TP
├── daily_summary.md       # Ranked candidates + macro read + pool changes
├── portfolio.md           # Paper-trading portfolio: equity, P&L, positions, fills
├── backtest_summary.md    # Chan signal backtest per name
└── forward_validation.md  # Forward tracking of past signals (5TD evaluation)
```

The paper portfolio (`decision/portfolio_core.py`, shared with A-share) trades on the strategy's
own signals: sell-before-buy ordering, idempotent same-day reruns, positions carried across days.

---

## Backtest Honesty | 回测诚实性

**The old 79.8% Chan win rate was survivorship-inflated and is retracted.**

The former event extractor built strokes on the *full* history and sliced backwards. Because
inclusion-merging and stroke cleanup can repaint the right edge, only strokes that *survived
into the final geometry* were counted — and failing signals are exactly the ones that get
repainted away. The backtest was structurally deleting its own losing trades.

Fixed (**R1.3**) by replaying **day-by-day as-of**, recomputing structure from only the data
available on each date and replicating the live engine's emission gates (freshness /
fractal-stop / stroke-confirmation + volatility). Honest baselines:

| Measure | Result |
|---------|--------|
| Chan rule strategy (buy point → 5TD return > 0) | **53.2%** (632 signals) vs random **55.5%** |
| P7 core pool (7% SL, 2:1 TP) | 109 trades, **40.4%**; b3 **53.3%**, b1 35.3%, b2 35.6% |
| Chan × ML | ML high-confidence half: Chan 55.0% < low-confidence 62.0% (opposite edges — still holds) |

These baselines are what drove the R4.2 recalibration above. **Lesson: a backtest must share
the live engine's emission gates** — "recompute then slice backwards" quietly introduces
survivorship bias.

---

## No-Lookahead Rules | 无前视规则

| Path | Rule |
|------|------|
| Live | Price basis is the last completed bar (t−1); yfinance `end=today` is exclusive |
| Chan | Signals fire only after stroke/pivot confirmation — in-progress structures are never anticipated |
| Backtest | Structure and indicators are recomputed as-of each date (R1.3) |
| Fundamentals | ⚠️ `signals/quant/fundamental.py` reads a **live yfinance snapshot** with no point-in-time history. Acceptable for live scanning; **excluded from backtests** — reusing it there would be lookahead. The "financials + 2-month lag" rule applies to any future PIT-backed implementation. |

---

## A-Share Track | A股缠论侧（次线）

Secondary track. A-share has no macro/quant data available here, so it runs as a **pure Chan
axis**, reusing the market-agnostic structure engine with three adaptations:

```bash
python mainA.py               # Selection → output/ashare/{date}/ (Buy/Watch + next-day plan)
python run_ashare_backtest.py # Backtest by type (b1/b2/b3 only) + bull/bear phases
```

| Aspect | US (`main.py`) | A-Share (`mainA.py`) |
|--------|----------------|----------------------|
| Data | yfinance live | local CSV `processed_stocks_selected/` |
| Axes | Chan × Macro × Quant | Chan only |
| MACD divergence | recomputed | precomputed `macd` column (= 2×(dif−dea)) |
| Indicators | — | KDJ/RSI divergence + CCI/BOLL strength (**confirm only, never create signals**) |
| Gating | VIX 4-tier | conservative: b2/b3 priority; b1 needs weekly-not-down + bottom divergence |
| Price limits | none | board ±10/20/30% modeled in backtest |
| Sizing | VIX-gated | Livermore 2%: position = RISK_BUDGET(2%) / R; R > 15% → Watch |
| Buy scores | R4.2 recalibrated | pinned to original (0.50/0.75/0.65) |

**Boards | 板块**: `300/301` → 创业板 (±20%), `688/689` → 科创板 (±20%), `8/4` → 北交所 (±30%),
else 主板 (±10%).

**`lb2` (类二买)** — a right-side entry on dried-up volume + Bollinger squeeze + fast ZG
breakout — backtested at only ~42% and is therefore **Watch-only**: visible for manual review,
excluded from Buy and from backtests.

**⚠️ A-share baselines pending re-test**: the old 68.7% pool win rate and the "all six bull/bear
phases > 50%" conclusion came from the biased extractor. The extractor was ported to as-of
replay on 2026-07-15, but re-running requires the `processed_stocks_selected/` dataset, which is
not in the repo. Until then, A-share buy scores stay at their original calibration.

**Core principle | 核心原则**: Chan structure (走势形态 + 中枢) *is* the signal;
MACD/KDJ/RSI/CCI/BOLL only adjust score/confidence and gating — they never replace it.

---

## Directory Structure | 目录结构

```
stock_analyse/
├── main.py                        # US selection entry | 美股选股入口
├── mainA.py                       # A-share selection entry | A股选股入口
├── run_ml_backtest.py             # US ML backtest | 美股 ML 回测
├── run_ashare_backtest.py         # A-share Chan backtest | A股缠论回测
├── CLAUDE.md                      # Project rules & Chan standard | 项目规则与缠论标准
├── PRD.md                         # Audit report & roadmap | 审计报告与需求路线图
├── config/
│   ├── settings.py                # pydantic-settings (.env)
│   ├── stocks.py                  # US pool + buckets | 美股池（3桶 + dynamic）
│   ├── stocks_ashare.py           # A-share params: boards, regimes, lb2 | A股参数
│   └── pool_manager.py            # Dynamic pool snapshots | 动态池快照
├── data/
│   ├── base.py                    # DataSource Protocol
│   ├── yfinance_source.py / finnhub_source.py / fred_source.py / alpha_vantage_source.py
│   ├── universe.py                # Candidate universe | 候选宇宙
│   ├── cache.py                   # SQLite cache (empty frames never cached)
│   ├── pipeline.py
│   └── ashare_loader.py           # A-share CSV loader | A股本地CSV装载
├── signals/
│   ├── screening.py               # Pool add/remove screening | 加池/removed 筛选
│   ├── chan/                      # Chan engine (market-agnostic core) | 缠论引擎
│   │   ├── fractal.py             # 包含关系 + 分型
│   │   ├── stroke.py              # 笔
│   │   ├── pivot.py               # 中枢 ZG/ZD
│   │   ├── chan_signal.py         # US Chan → ChanSignalResult + BUY_SCORES
│   │   └── chan_signal_ashare.py  # A-share Chan (precomputed MACD + gating)
│   ├── quant/
│   │   ├── fundamental.py / trend.py / momentum.py / relative.py / volume.py
│   │   └── factor_engine.py       # → QuantSignalResult
│   └── macro/
│       ├── regime.py              # VIX four-tier | VIX 四档制度
│       ├── external_factors.py    # Oil / USD / inflation / rate-hike expectations
│       ├── sector_strength.py
│       └── macro_signal.py        # → MacroSignalResult
├── decision/
│   ├── scorer.py                  # Dual-axis fusion | 双主轴打分
│   ├── risk_overlay.py            # Position + structural stops + R_MAX
│   ├── rating.py                  # 5-tier rating | 5档评级
│   ├── hysteresis.py / hysteresis_ashare.py / hysteresis_core.py   # Guard B | 迟滞
│   ├── portfolio_core.py          # Paper trading (shared US/A-share) | 模拟组合
│   ├── strategy.py                # → StockDecision
│   └── strategy_ashare.py         # → AShareDecision
├── backtest/
│   ├── engine.py                  # US matching | 美股撮合
│   ├── engine_ashare.py           # A-share matching (limits/gaps) | A股撮合
│   ├── ml_backtest.py             # LightGBM walk-forward, purged/embargoed
│   ├── forward_tracker.py         # Forward validation (SQLite) | 前向验证
│   └── report.py
├── report/report_writer.py
├── utils/                         # logger, time_utils, housekeeping
├── output/  cache/  logs/         # git ignored
```

---

## Interface Contracts | 接口契约

| Data class | Source |
|------------|--------|
| `ChanSignalResult` | `signals/chan/chan_signal.py` |
| `QuantSignalResult` | `signals/quant/factor_engine.py` |
| `MacroSignalResult` | `signals/macro/macro_signal.py` |
| `StockDecision` | `decision/strategy.py` |
| `AShareDecision` | `decision/strategy_ashare.py` |

---

## Data Sources | 数据源

| API | Limit | Primary use |
|-----|-------|-------------|
| yfinance | unlimited | OHLCV, indices, `info` fundamentals |
| FRED | unlimited | Rates (DGS10/DGS2), CPI, VIX |
| Finnhub | 60/min | News sentiment, earnings calendar |
| Alpha Vantage | 500/day | Financials (not used in the live path today) |

---

## Status & Roadmap | 状态与路线图

| Phase | Content | Status |
|-------|---------|--------|
| P1 | Data layer, 4 sources + SQLite cache | ✅ |
| P2 | Quant five-factor engine | ✅ |
| P3 | Macro gating + external factors | ✅ |
| P4 | Chan engine (fractal/stroke/pivot/points) | ✅ |
| P5 | Decision layer (scoring + risk + hysteresis) | ✅ |
| P6 | Report layer + end-to-end `main.py` | ✅ |
| P7 | Historical backtest (as-of, unbiased) | ✅ |
| P8 | Forward validation + paper portfolio | ✅ |

See **[PRD.md](PRD.md)** for the full audit (17 catalogued defects with file:line evidence) and
the R1–R4 roadmap. Delivered: R1.1 (portfolio sells routed through hysteresis), R1.2 (stop
direction validation), R1.3 (backtest survivorship fix), R4.2 (score recalibration).
Open: R2 (schedulability — argparse / `--non-interactive` / trading calendar), R3 (data
reliability — cache oil/USD, retries, `DEGRADED` markers), R4.1/R4.3–R4.5.

---

## Development Principles | 开发四原则

1. **Think before coding** — compare approaches; ask when uncertain.
2. **Minimal first** — no speculative or redundant logic.
3. **Precise edits** — change only what the requirement names.
4. **No lookahead** — close[t−1], fundamentals lagged, signals only after structure confirms.
