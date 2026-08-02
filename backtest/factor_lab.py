"""R6.1 因子验证实验室 Factor Lab — 通用 IC/IR + 分位 + 相关性剪枝门。

动机：R5 的 `factor_eval` 只能验证 momentum special 的**布尔触发事件**，无法回答
「任意**连续因子**与前向收益的横截面相关性、分位单调性、跨年稳定性、彼此冗余度」。
本模块是 R6 所有候选因子（R6.2 缠论结构因子、R6.3 做空情绪/逼空因子 …）**共用的门**：
过门（IC 显著 + 分位单调 + 跨年稳定 + 与现有因子低相关）才准 merge 进实盘打分。

设计要点（与既有代码库全兼容——纯新增模块，零改动现有文件）：
  • **因子无关**：核心分析函数只消费一张长面板 `[date, ticker, <因子列…>, fwd5/10/20]`，
    不含任何具体因子逻辑；R6.2/R6.3 因子以 `factor_fn(df)->pd.Series` 形式插入（接入 seam）。
  • **面板来源**：逐票 `DataPipeline.get_backtest_price`（cache 命中即离线返回、保留 ticker+date），
    不用 `factor_eval.load_price_blobs`（哈希 blob 丢 ticker，无法横截面 IC/跨年分组），
    亦不用 `ml_backtest.build_dataset`（网络批量下载 + 耦合 lightgbm 栈）。
  • **前向收益口径**与 `factor_eval` 一致：`close.shift(-h)/close - 1`（无前视）。
  • **as-of 漂移守卫**：`assert_asof_consistent` 断言 `fn(全序列)[t] == fn(截断到 t)[-1]`——
    截断不变性即「仅用 ≤t 数据」的无前视保证，单实现即可自证（比 factor_eval 的「向量化==另写实盘
    标量函数」更通用，是每个新因子必过的守卫）。
  • **诚实显著性**：前向窗口重叠 h 天→日度 IC 序列自相关，朴素 t 值虚高；有效独立样本按 `n/h`
    折算（overlap 调整），与三层稀释纪律一致，不夸大。

用法：`python -m backtest.factor_lab`（先跑 `python main.py` 预热价格 cache）。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

FactorFn = Callable[[pd.DataFrame], pd.Series]

# ── 常量 ─────────────────────────────────────────────────────────
FWD = (5, 10, 20)                 # 前向收益天数（与 factor_eval 对齐）
PRIMARY_H = 10                    # 主口径前向天数
MIN_NAMES_PER_DATE = 8            # 逐日横截面所需最少名字数（不足则该日丢弃）
N_QUANTILES = 5                   # 分位组数
MIN_ROWS = 300                    # 单票入面板最少行数（保证长窗口特征成熟）

# merge 判据阈值（**筛选启发式，非硬定律**；equity factor 常规量级，实测可微调）
IC_MIN = 0.02                     # |IC| 显著非零下限
T_MIN = 2.0                       # overlap 调整后 |t| 下限
MONO_MIN = 0.90                   # 分位均值 vs 序号 |Spearman| 单调下限
YEAR_SIGN_MIN = 0.60             # 与全样本同号年份占比（跨年稳定性）
CORR_MAX = 0.70                   # 与现有因子最大 |corr|（超过=冗余，剪枝）

# 验证宇宙：**宽于活跃池**的流动性大盘美股（cache 未命中者首跑下载、之后离线复用）。
# 2026-08-02 由 29(偏科技) 扩到 ~78 跨行业大盘：更宽横截面收窄 IC 置信区间、de-confound 板块，
# 给因子公平的显著性检验（R6.2 首门 29 只下仅 chan_dist_swlo 够 t≥2，疑瓶颈在宇宙宽度非因子）。
VALIDATION_UNIVERSE = [
    # 科技 & 半导体
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AVGO", "AMD", "MU",
    "INTC", "QCOM", "TXN", "AMAT", "LRCX", "KLAC", "ADI", "MRVL", "CRM", "ORCL",
    "ADBE", "NOW", "INTU", "NFLX", "PANW", "FTNT", "CRWD", "CSCO", "IBM", "DELL", "ARM",
    # 消费
    "HD", "LOW", "NKE", "SBUX", "MCD", "COST", "WMT", "TGT", "PG", "KO", "PEP",
    # 金融
    "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "AXP", "BLK", "SCHW",
    # 医疗
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "AMGN",
    # 能源 & 工业
    "XOM", "CVX", "COP", "CAT", "DE", "BA", "GE", "HON", "UNP", "LMT",
    # 通信 & 其它
    "DIS", "CMCSA", "VZ", "VRT", "SNDK",
]


# ── 面板构建 ─────────────────────────────────────────────────────
def forward_returns(close: pd.Series, horizons: Sequence[int] = FWD) -> Dict[str, pd.Series]:
    """前向 h 日收益 `close[t+h]/close[t]-1`（与 factor_eval 同口径，无前视）。"""
    return {f"fwd{h}": close.shift(-h) / close - 1.0 for h in horizons}


def build_factor_panel(
    price_by_ticker: Dict[str, pd.DataFrame],
    factor_fns: Dict[str, FactorFn],
    horizons: Sequence[int] = FWD,
    min_rows: int = MIN_ROWS,
) -> pd.DataFrame:
    """把逐票价格 + 一组 `factor_fn(df)->Series` 装配成长面板。

    每票：因子按整列向量化（仅 ≤t 数据）、附前向收益、带 ticker/date；
    丢弃 fwd(max) 为 NaN（尾部无标签）或全因子 NaN 的行。返回 [date, ticker, <因子…>, fwdN]。
    """
    max_h = max(horizons)
    frames: List[pd.DataFrame] = []
    for tk, df in price_by_ticker.items():
        if df is None or "Close" not in df or len(df) < min_rows:
            continue
        close = df["Close"].astype(float)
        cols = {name: pd.Series(np.asarray(fn(df), dtype=float), index=df.index)
                for name, fn in factor_fns.items()}
        fr = pd.DataFrame(cols, index=df.index)
        for k, v in forward_returns(close, horizons).items():
            fr[k] = v
        fr = fr.dropna(subset=[f"fwd{max_h}"]).dropna(subset=list(factor_fns), how="all")
        if fr.empty:
            continue
        fr["ticker"] = tk
        fr["date"] = fr.index      # DatetimeIndex → 显式 date 列，供 groupby 横截面
        frames.append(fr.reset_index(drop=True))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["date", "ticker"]).reset_index(drop=True)


# ── IC / IR ──────────────────────────────────────────────────────
def daily_ic(
    panel: pd.DataFrame,
    factor_col: str,
    fwd_col: str,
    method: str = "spearman",
    min_names: int = MIN_NAMES_PER_DATE,
) -> pd.Series:
    """逐日横截面 IC 序列（默认 Spearman RankIC）；不足 min_names 的日丢弃。"""
    ics: Dict[pd.Timestamp, float] = {}
    for d, g in panel.groupby("date"):
        s = g[[factor_col, fwd_col]].dropna()
        if len(s) < min_names or s[factor_col].nunique() < 2:
            continue
        ics[d] = s[factor_col].corr(s[fwd_col], method=method)
    return pd.Series(ics, dtype=float).dropna().sort_index()


def ir_stats(ic: pd.Series, horizon: int) -> dict:
    """IC 序列 → IC 均值 / IR(=均值/标准差) / 命中率 / overlap 调整 t 值。"""
    n = int(len(ic))
    mean = float(ic.mean()) if n else float("nan")
    std = float(ic.std(ddof=1)) if n > 1 else float("nan")
    ir = mean / std if (std and np.isfinite(std) and std > 0) else float("nan")
    hit = float((ic > 0).mean()) if n else float("nan")
    # overlap 调整：fwd 窗口重叠 horizon 天→有效独立样本≈ n/horizon（防重叠自相关抬高 t）
    n_eff = max(n / horizon, 1.0)
    t_stat = ir * np.sqrt(n_eff) if np.isfinite(ir) else float("nan")
    return {"n_days": n, "ic_mean": mean, "ic_std": std, "ir": ir,
            "hit_rate": hit, "t_stat": t_stat}


def ic_by_year(panel: pd.DataFrame, factor_col: str, fwd_col: str, **kw) -> pd.DataFrame:
    """逐年 IC 均值/样本数（跨年稳定性）。"""
    ic = daily_ic(panel, factor_col, fwd_col, **kw)
    if ic.empty:
        return pd.DataFrame(columns=["mean", "count"])
    tmp = pd.DataFrame({"ic": ic.values, "year": pd.DatetimeIndex(ic.index).year})
    return tmp.groupby("year")["ic"].agg(["mean", "count"])


def _year_sign_consistency(by_year: pd.DataFrame, full_mean: float) -> float:
    """与全样本 IC 同号的年份占比。"""
    if by_year.empty or full_mean == 0 or not np.isfinite(full_mean):
        return 0.0
    signs = np.sign(by_year["mean"].values)
    return float((signs == np.sign(full_mean)).mean())


# ── 分位回测 ─────────────────────────────────────────────────────
def quantile_profile(
    panel: pd.DataFrame,
    factor_col: str,
    fwd_col: str,
    n_q: int = N_QUANTILES,
    min_names: int = MIN_NAMES_PER_DATE,
) -> dict:
    """逐日横截面按因子值分 n_q 组，汇总各组前向收益、high-low 价差、单调性。"""
    parts: List[pd.DataFrame] = []
    for _, g in panel.groupby("date"):
        s = g[[factor_col, fwd_col]].dropna()
        if len(s) < max(min_names, n_q) or s[factor_col].nunique() < n_q:
            continue
        q = pd.qcut(s[factor_col], n_q, labels=False, duplicates="drop")
        parts.append(s.assign(q=q))
    if not parts:
        return {}
    allq = pd.concat(parts)
    grp = allq.groupby("q")[fwd_col].agg(["mean", "count"])
    means = grp["mean"].to_numpy()
    counts = grp["count"].to_numpy()
    hi_lo = float(means[-1] - means[0]) if len(means) >= 2 else float("nan")
    # 单调性：分位均值序列 vs 序号的 Spearman（±1=严格单调，符号=方向）
    if len(means) >= 3:
        mono_rho = float(pd.Series(means).corr(pd.Series(range(len(means))), method="spearman"))
    else:
        mono_rho = float("nan")
    return {"q_means": means, "q_counts": counts, "hi_lo": hi_lo,
            "mono_rho": mono_rho, "mono_ok": bool(abs(mono_rho) >= MONO_MIN)}


# ── 相关性剪枝 ───────────────────────────────────────────────────
def factor_correlations(panel: pd.DataFrame, factor_cols: Sequence[str]) -> pd.DataFrame:
    """候选因子两两 |corr| 矩阵（Pearson，池化全样本）。"""
    return panel[list(factor_cols)].corr().abs()


def max_corr_with(panel: pd.DataFrame, factor_col: str, others: Sequence[str]) -> Tuple[float, str]:
    """因子与一组既有因子的最大 |corr| 及其对手名（无对手→0）。"""
    others = [c for c in others if c != factor_col and c in panel.columns]
    if not others:
        return 0.0, ""
    c = panel[[factor_col, *others]].corr().abs()[factor_col].drop(factor_col)
    return float(c.max()), str(c.idxmax())


# ── 综合判定 ─────────────────────────────────────────────────────
def evaluate_factor(
    panel: pd.DataFrame,
    factor_col: str,
    others: Sequence[str] = (),
    primary_h: int = PRIMARY_H,
) -> dict:
    """对单因子出 merge 判据裁决：|IC|/t/单调/跨年同号/与现有因子低相关。"""
    fwd = f"fwd{primary_h}"
    ic = daily_ic(panel, factor_col, fwd)
    st = ir_stats(ic, primary_h)
    qp = quantile_profile(panel, factor_col, fwd)
    yr = ic_by_year(panel, factor_col, fwd)
    year_sign = _year_sign_consistency(yr, st["ic_mean"])
    mx, mx_name = max_corr_with(panel, factor_col, others)

    checks = {
        "ic":        bool(abs(st["ic_mean"]) >= IC_MIN),
        "t":         bool(np.isfinite(st["t_stat"]) and abs(st["t_stat"]) >= T_MIN),
        "monotonic": bool(qp.get("mono_ok", False)),
        "year":      bool(year_sign >= YEAR_SIGN_MIN),
        "independent": bool(mx < CORR_MAX),
    }
    passed = all(checks.values())
    return {"factor": factor_col, "ic_stats": st, "quantile": qp, "by_year": yr,
            "year_sign": year_sign, "max_corr": mx, "max_corr_with": mx_name,
            "checks": checks, "verdict": "PASS" if passed else "REJECT"}


# ── as-of 漂移守卫（无前视保证，每个新因子必过）──────────────────
def assert_asof_consistent(
    price_by_ticker: Dict[str, pd.DataFrame],
    factor_fn: FactorFn,
    n_samples: int = 60,
    seed: int = 7,
    tol: float = 1e-9,
) -> int:
    """断言 `fn(全序列)[t] == fn(截断到 t)[-1]`——截断不变性即「仅用 ≤t 数据」。"""
    items = [(tk, df) for tk, df in price_by_ticker.items()
             if df is not None and len(df) >= MIN_ROWS]
    if not items:
        raise AssertionError("no eligible price frames for as-of check")
    rng = np.random.default_rng(seed)
    checked = mism = 0
    for _ in range(n_samples):
        tk, df = items[rng.integers(len(items))]
        t = int(rng.integers(260, len(df) - max(FWD) - 1))
        full = pd.Series(np.asarray(factor_fn(df), dtype=float), index=df.index)
        trunc = pd.Series(np.asarray(factor_fn(df.iloc[: t + 1]), dtype=float),
                          index=df.index[: t + 1])
        a, b = float(full.iloc[t]), float(trunc.iloc[-1])
        checked += 1
        if np.isfinite(a) and np.isfinite(b) and abs(a - b) > tol:
            mism += 1
    assert mism == 0, f"as-of drift: {mism}/{checked} 全序列≠截断（因子用了未来数据）"
    return checked


# ── 自检：合成面板已知真值，校准 IC/分位口径 ─────────────────────
def _synthetic_panel(n_dates: int = 300, n_names: int = 20, beta: float = 1.0,
                     seed: int = 1) -> pd.DataFrame:
    """构造 fwd = beta·g + 噪声 的面板：beta>0→IC 正且分位递增；=0→IC≈0；<0→反向。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_dates)
    parts = []
    for d in dates:
        g = rng.normal(size=n_names)
        fwd = beta * g * 0.01 + rng.normal(size=n_names) * 0.01
        parts.append(pd.DataFrame({"date": d, "ticker": [f"S{i}" for i in range(n_names)],
                                   "g": g, "fwd10": fwd}))
    return pd.concat(parts, ignore_index=True)


def _selfcheck() -> None:
    """校准实验室口径：正/零/负 factor 的 IC 与分位方向须被正确复现。"""
    pos = _synthetic_panel(beta=1.0)
    st = ir_stats(daily_ic(pos, "g", "fwd10"), 10)
    qp = quantile_profile(pos, "g", "fwd10")
    assert st["ic_mean"] > 0.2 and st["t_stat"] > 2, f"正 factor IC 未复现: {st}"
    assert qp["hi_lo"] > 0 and qp["mono_ok"], f"正 factor 分位未单调: {qp}"

    zero = ir_stats(daily_ic(_synthetic_panel(beta=0.0), "g", "fwd10"), 10)
    assert abs(zero["ic_mean"]) < 0.1, f"零 factor IC 未≈0: {zero}"

    neg = ir_stats(daily_ic(_synthetic_panel(beta=-1.0), "g", "fwd10"), 10)
    qn = quantile_profile(_synthetic_panel(beta=-1.0), "g", "fwd10")
    assert neg["ic_mean"] < -0.2 and qn["hi_lo"] < 0, f"负 factor 未反向: {neg} {qn}"
    print("[selfcheck] IC/分位口径正确（正/零/负 factor 方向均正确复现）")


# ── 基线校准：真实参照因子（证明实验室在真数据上口径合理）───────────
REFERENCE_FACTORS: Dict[str, FactorFn] = {
    "mom_roc20":    lambda df: df["Close"] / df["Close"].shift(20) - 1.0,          # 20日动量
    "reversal_5":   lambda df: -(df["Close"] / df["Close"].shift(5) - 1.0),        # 5日反转
    "dist_high252": lambda df: df["Close"] / df["Close"].rolling(252).max() - 1.0, # 距52周高
    "lowvol_20":    lambda df: -df["Close"].pct_change().rolling(20).std(),        # 低波动
}


def _load_universe_prices(universe: Sequence[str]) -> Dict[str, pd.DataFrame]:
    """逐票取 backtest 窗口价格（cache 命中即离线；缺失首跑下载后复用）。"""
    from data.pipeline import DataPipeline
    pl = DataPipeline()
    out: Dict[str, pd.DataFrame] = {}
    for tk in universe:
        try:
            df = pl.get_backtest_price(tk)
            if df is not None and not df.empty and len(df) >= MIN_ROWS:
                out[tk] = df
        except Exception as e:  # 单票失败不阻断（离线/退市等）
            print(f"  skip {tk}: {e}")
    return out


def _report_factor(panel: pd.DataFrame, col: str, others: Sequence[str]) -> None:
    res = evaluate_factor(panel, col, others)
    st, qp = res["ic_stats"], res["quantile"]
    print(f"\n── {col} ──  verdict={res['verdict']}  checks={res['checks']}")
    for h in FWD:
        s = ir_stats(daily_ic(panel, col, f"fwd{h}"), h)
        print(f"   fwd{h:>2}: IC={s['ic_mean']:+.4f} IR={s['ir']:+.3f} "
              f"hit={s['hit_rate']:.2f} t={s['t_stat']:+.2f} (n_days={s['n_days']})")
    if qp:
        qm = " ".join(f"{m:+.4f}" for m in qp["q_means"])
        print(f"   分位(Q1→Q{len(qp['q_means'])}) fwd{PRIMARY_H}: [{qm}]  "
              f"hi-lo={qp['hi_lo']:+.4f}  单调ρ={qp['mono_rho']:+.2f}")
    print(f"   跨年同号={res['year_sign']:.2f}  与现有因子 max|corr|={res['max_corr']:.2f}"
          f"{'('+res['max_corr_with']+')' if res['max_corr_with'] else ''}")


def main() -> None:
    _selfcheck()
    prices = _load_universe_prices(VALIDATION_UNIVERSE)
    if len(prices) < MIN_NAMES_PER_DATE:
        print(f"验证宇宙价格不足（{len(prices)}<{MIN_NAMES_PER_DATE}）——先跑 `python main.py` 预热 cache")
        return

    # as-of 守卫：参照因子须全序列≡截断（无前视）
    for name, fn in REFERENCE_FACTORS.items():
        assert_asof_consistent(prices, fn)
    print(f"[asof] 参照因子全序列≡截断 as-of 一致（{len(REFERENCE_FACTORS)} 因子）")

    panel = build_factor_panel(prices, REFERENCE_FACTORS)
    cols = list(REFERENCE_FACTORS)
    span = (f"{panel['date'].min():%Y-%m-%d}→{panel['date'].max():%Y-%m-%d}"
            if not panel.empty else "n/a")
    print(f"\n面板: tickers={panel['ticker'].nunique()}  rows={len(panel)}  "
          f"dates={panel['date'].nunique()} ({span})")

    print("\n" + "=" * 72)
    print("基线校准 — 参照因子 IC/IR + 分位 + 跨年 + 相关性（自证实验室口径）")
    print("（⚠️ 横截面 CI 随宇宙宽度收窄；稳健的是符号与单调方向，非某位小数）")
    print("=" * 72)
    for col in cols:
        _report_factor(panel, col, [c for c in cols if c != col])

    print("\n因子两两 |corr|（相关性剪枝依据，>%.2f 视为冗余）:" % CORR_MAX)
    print(factor_correlations(panel, cols).round(2).to_string())

    print("\nR6.1 Factor Lab 就绪：R6.2/R6.3 因子以 factor_fn 插入 build_factor_panel，"
          "过 evaluate_factor 判据（IC/t/单调/跨年/低相关）才 merge。")


if __name__ == "__main__":
    main()
