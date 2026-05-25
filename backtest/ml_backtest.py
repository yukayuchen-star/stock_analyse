"""
ML 历史回测模块 — LightGBM 走步前向验证

目标：验证缠论 + 量化因子在 2022~今 的历史预测能力。

特征（无前视偏差，仅用截至当日数据）：
  技术面：SMA 比率、RSI14、ROC20、MACD、ADX14、成交量偏离
  相对强度：vs QQQ/SPY 超额收益（20d / 60d）
  缠论：当日是否有买/卖事件、信号类型、近期结构强度
  宏观：VIX 水平、VIX 制度、利差

标签：5 个交易日后收益 > 0 → 1（做多盈利），否则 0

走步前向（expanding window）：
  min 训练集 18 个月，测试窗口 6 个月滚动
  → 防止过拟合，模拟真实边界条件
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf
from loguru import logger

try:
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    _HAS_LGB = True
except (ImportError, OSError):
    _HAS_LGB = False

warnings.filterwarnings("ignore")

# ── 常量 ────────────────────────────────────────────────────────

HOLD_DAYS   = 5       # 预测持仓周期（交易日）
CONF_THRESH = 0.55    # 模型置信度门槛（高于此值才"做多"）
WARMUP      = 250     # 预热期（天），特征需要 SMA200 等长窗口
MIN_TRAIN_MONTHS = 18

# 默认覆盖宇宙（可在调用方覆盖）
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
    "AVGO", "AMD", "MU", "INTC",
    "CRM", "ORCL", "ADBE", "NFLX", "PANW",
    "JPM", "GS", "V", "MA",
    "LLY", "UNH", "ABBV",
    "XOM", "CVX",
    "FTNT", "SNDK", "VRT", "ARM",
]
BENCHMARKS = ["QQQ", "SPY"]


# ── 特征工程 ──────────────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder 平均 True Range + DI → ADX（精简版）。"""
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()

    plus_dm  = (high.diff()).clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    # 当 +DM > -DM 才计；反之为 0（简化：直接用 rolling）
    plus_di  = 100 * _ema(plus_dm,  period) / atr.replace(0, np.nan)
    minus_di = 100 * _ema(minus_dm, period) / atr.replace(0, np.nan)
    dx       = (100 * (plus_di - minus_di).abs() /
                (plus_di + minus_di).replace(0, np.nan))
    return dx.ewm(span=period, adjust=False).mean()


def _obv_slope(close: pd.Series, volume: pd.Series, window: int = 20) -> pd.Series:
    direction = np.sign(close.diff())
    obv = (direction * volume).cumsum()
    return obv.diff(window) / (obv.abs().rolling(window).mean() + 1e-9)


def compute_tech_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    给定单只股票的 OHLCV DataFrame，返回技术面特征（行对齐，含 NaN 预热期）。
    所有特征在 t 时刻只使用 ≤t 的数据（无前视偏差）。
    """
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]

    sma20  = c.rolling(20).mean()
    sma60  = c.rolling(60).mean()
    sma200 = c.rolling(200).mean()

    macd_line   = _ema(c, 12) - _ema(c, 26)
    macd_signal = _ema(macd_line, 9)

    feat = pd.DataFrame(index=df.index)

    # 过去收益
    for n in [5, 10, 20, 60]:
        feat[f"ret_{n}d"] = c.pct_change(n)

    # 趋势
    feat["sma20_ratio"]  = c / sma20  - 1
    feat["sma60_ratio"]  = c / sma60  - 1
    feat["sma200_ratio"] = c / sma200 - 1
    feat["sma20_slope"]  = sma20.diff(5) / (sma20.shift(5).abs() + 1e-9)
    feat["sma_20_60"]    = sma20 / sma60 - 1

    # 动量
    feat["rsi14"]        = _rsi(c, 14)
    feat["roc20"]        = c.pct_change(20) * 100
    feat["macd_hist"]    = (macd_line - macd_signal) / (c + 1e-9)
    feat["roc5"]         = c.pct_change(5) * 100

    # 趋势强度
    feat["adx14"]        = _adx(h, l, c, 14)

    # 量价
    vol_sma20 = v.rolling(20).mean()
    feat["vol_ratio"]    = v / (vol_sma20 + 1e-9)
    feat["obv_slope"]    = _obv_slope(c, v, 20)
    feat["hl_ratio"]     = (h - l).rolling(20).mean() / (c + 1e-9)  # 波动率代理

    return feat


def compute_statistical_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    统计特征工程：对数收益、滚动波动/统计量、滚动夏普、EWM、差分、区间位置。

    全部仅用 ≤t 数据（pandas rolling/ewm 默认后视，无前视偏差）。
    设计依据：对数收益分布更对称利于建模；滚动统计量捕捉波动制度；
    滚动夏普 = 风险调整动量；EWM 对近期更敏感；差分捕捉加速度。
    """
    c = df["Close"]
    feat = pd.DataFrame(index=df.index)

    # 对数收益（分布比 pct_change 更对称）
    log_ret = np.log(c / c.shift(1))
    feat["log_ret_1d"] = log_ret

    # 滚动已实现波动率（年化）
    for w in [5, 20, 60]:
        feat[f"realized_vol_{w}d"] = log_ret.rolling(w).std() * np.sqrt(252)

    # 滚动收益分布统计量（均值/标准差/偏度/中位数）
    for w in [20, 60]:
        r = log_ret.rolling(w)
        feat[f"ret_mean_{w}d"]   = r.mean()
        feat[f"ret_std_{w}d"]    = r.std()
        feat[f"ret_skew_{w}d"]   = r.skew()
        feat[f"ret_median_{w}d"] = log_ret.rolling(w).median()

    # 滚动夏普（风险调整动量，年化）
    for w in [20, 60]:
        mu  = log_ret.rolling(w).mean()
        sig = log_ret.rolling(w).std()
        feat[f"sharpe_{w}d"] = (mu / (sig + 1e-9)) * np.sqrt(252)

    # EWM（指数加权，近期权重更高）
    feat["ewm_ret_10d"] = log_ret.ewm(span=10, adjust=False).mean()
    feat["ewm_vol_20d"] = log_ret.ewm(span=20, adjust=False).std()

    # 差分 / 加速度（动量与波动的变化率）
    feat["ret_accel"] = log_ret.diff(5)
    feat["vol_accel"] = feat["realized_vol_20d"].diff(5)

    # 价格在滚动区间内的相对位置（0~1，类 Williams %R）
    for w in [20, 60]:
        lo = c.rolling(w).min()
        hi = c.rolling(w).max()
        feat[f"pos_in_range_{w}d"] = (c - lo) / (hi - lo + 1e-9)

    # 价格 z-score（相对滚动均值的标准化偏离）
    for w in [20, 60]:
        mu  = c.rolling(w).mean()
        sig = c.rolling(w).std()
        feat[f"price_zscore_{w}d"] = (c - mu) / (sig + 1e-9)

    return feat


def compute_rel_features(
    close: pd.Series,
    qqq: pd.Series,
    spy: pd.Series,
) -> pd.DataFrame:
    """相对强度 vs 基准（仅用过去数据）。"""
    feat = pd.DataFrame(index=close.index)
    for n in [20, 60]:
        stock_ret = close.pct_change(n)
        feat[f"rel_qqq_{n}d"] = stock_ret - qqq.pct_change(n)
        feat[f"rel_spy_{n}d"] = stock_ret - spy.pct_change(n)
    # 相对强度 Z-score（vs 自身过去 60d 分布）
    rel20 = feat["rel_qqq_20d"]
    feat["rel_zscore"] = (rel20 - rel20.rolling(60).mean()) / (rel20.rolling(60).std() + 1e-9)
    return feat


def compute_chan_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    提取缠论事件特征。
    在每个事件日打标记，非事件日填 0；
    另加"距上次信号天数"和近期累积信号强度。
    """
    from signals.chan.chan_signal import extract_chan_events

    feat = pd.DataFrame({
        "chan_is_buy":    0,
        "chan_is_sell":   0,
        "chan_score":     0.0,
        "chan_type_enc":  0,   # b1=1,b2=2,b3=3,s1=-1,s2=-2,s3=-3
    }, index=df.index, dtype=float)

    try:
        events = extract_chan_events(df)
    except Exception as exc:
        logger.debug(f"chan_events failed: {exc}")
        return feat

    enc_map = {"b1": 1, "b2": 2, "b3": 3, "s1": -1, "s2": -2, "s3": -3}
    for ev in events:
        if ev.date not in feat.index:
            continue
        if ev.signal_type.startswith("b"):
            feat.loc[ev.date, "chan_is_buy"]   = 1
            feat.loc[ev.date, "chan_score"]     = ev.score
            feat.loc[ev.date, "chan_type_enc"]  = enc_map.get(ev.signal_type, 0)
        else:
            feat.loc[ev.date, "chan_is_sell"]   = 1
            feat.loc[ev.date, "chan_score"]     = ev.score
            feat.loc[ev.date, "chan_type_enc"]  = enc_map.get(ev.signal_type, 0)

    # 距上次买信号天数（近期性）
    buy_dates = feat.index[feat["chan_is_buy"] == 1]
    days_since = pd.Series(np.nan, index=feat.index)
    last = -1
    for i, d in enumerate(feat.index):
        if feat.loc[d, "chan_is_buy"] == 1:
            last = i
        if last >= 0:
            days_since[d] = i - last
    feat["chan_days_since_buy"] = days_since.fillna(999).clip(upper=60)

    # 滚动 20 日内买信号次数（结构频率）
    feat["chan_buy_count_20d"] = feat["chan_is_buy"].rolling(20).sum().fillna(0)

    return feat


def build_macro_features(start: str, end: str) -> pd.DataFrame:
    """
    从 yfinance 拉取 ^VIX，构造宏观特征。
    （FRED yield 数据需 API key，这里退而使用 ^TNX 代替）
    """
    try:
        vix = yf.download("^VIX", start=start, end=end,
                           progress=False, auto_adjust=True)["Close"]
        tnx = yf.download("^TNX", start=start, end=end,
                           progress=False, auto_adjust=True)["Close"]
        tyx = yf.download("^TYX", start=start, end=end,
                           progress=False, auto_adjust=True)["Close"]
    except Exception as exc:
        logger.warning(f"Macro data fetch failed: {exc}")
        return pd.DataFrame()

    if isinstance(vix, pd.DataFrame):
        vix = vix.squeeze()
    if isinstance(tnx, pd.DataFrame):
        tnx = tnx.squeeze()
    if isinstance(tyx, pd.DataFrame):
        tyx = tyx.squeeze()

    macro = pd.DataFrame(index=vix.index)
    macro["vix_level"]  = vix
    macro["vix_sma20"]  = vix.rolling(20).mean()
    macro["vix_rising"] = (vix > vix.rolling(5).mean()).astype(int)
    macro["vix_regime"] = pd.cut(
        vix,
        bins=[0, 15, 25, 35, 999],
        labels=[0, 1, 2, 3],
    ).astype(float)

    # 利差：30Y - 10Y（短端替代，TNX=10Y，TYX=30Y）
    macro["yield_30_10"] = (tyx - tnx).fillna(0)
    macro["tnx_level"]   = tnx

    # VIX 百分位（过去 252 日）
    macro["vix_pct252"] = vix.rolling(252).apply(
        lambda x: (x[-1] >= x).mean(), raw=True
    )

    return macro.ffill()


# ── 数据集构建 ────────────────────────────────────────────────────

@dataclass
class MLDataset:
    df:      pd.DataFrame   # 全特征矩阵（含 label）
    tickers: List[str]
    feature_cols: List[str]
    label_col: str = "label"
    fwd_ret_col: str = "fwd_ret_5d"


def build_dataset(
    tickers: List[str],
    start: str = "2020-11-01",   # 比回测起点早 14m：覆盖 SMA200/vix_pct252 长窗口
    end: Optional[str] = None,
    backtest_start: str = "2022-01-01",
) -> MLDataset:
    """
    为每只股票下载数据、计算特征、拼接成训练矩阵。
    标签：5TD 后收益 > 0 → 1，否则 0。
    """
    if end is None:
        from utils.time_utils import today_str
        end = today_str()

    logger.info(f"[ML] 下载 {len(tickers)} 只股票 + 基准 ({start}→{end}) ...")
    raw: Dict[str, pd.DataFrame] = {}
    tickers_all = list(tickers) + BENCHMARKS
    try:
        batch = yf.download(
            tickers_all,
            start=start, end=end,
            progress=False, auto_adjust=True,
            group_by="ticker",
        )
        for t in tickers_all:
            try:
                df_t = batch[t].dropna(subset=["Close"])
                if len(df_t) > WARMUP:
                    raw[t] = df_t
            except Exception:
                pass
    except Exception as exc:
        logger.warning(f"Batch download failed ({exc}), falling back to serial")
        for t in tickers_all:
            try:
                df_t = yf.download(t, start=start, end=end,
                                   progress=False, auto_adjust=True).dropna(subset=["Close"])
                if len(df_t) > WARMUP:
                    raw[t] = df_t
            except Exception:
                pass

    valid_tickers = [t for t in tickers if t in raw]
    logger.info(f"[ML] 有效股票: {len(valid_tickers)} / {len(tickers)}")

    if "QQQ" not in raw or "SPY" not in raw:
        logger.error("[ML] 基准数据缺失，无法构建相对强度特征")
        raise RuntimeError("QQQ/SPY data missing")

    qqq_close = raw["QQQ"]["Close"]
    spy_close = raw["SPY"]["Close"]

    logger.info("[ML] 构建宏观特征 ...")
    macro_df = build_macro_features(start, end)

    rows: List[pd.DataFrame] = []
    for ticker in valid_tickers:
        df = raw[ticker]
        logger.debug(f"[ML]   {ticker}: {len(df)} 行 特征计算中 ...")

        tech   = compute_tech_features(df)
        stat   = compute_statistical_features(df)
        rel    = compute_rel_features(df["Close"], qqq_close.reindex(df.index).ffill(),
                                       spy_close.reindex(df.index).ffill())
        chan   = compute_chan_features(df)

        feats = pd.concat([tech, stat, rel, chan], axis=1)

        # 对齐宏观
        if not macro_df.empty:
            macro_aligned = macro_df.reindex(df.index).ffill()
            feats = pd.concat([feats, macro_aligned], axis=1)

        # 标签：5TD 后收益
        fwd_ret = df["Close"].pct_change(-HOLD_DAYS).shift(-HOLD_DAYS) * -1
        # pct_change(-n) = (close[t] - close[t+n]) / close[t+n]，需要翻转
        # 正确：(close[t+5] - close[t]) / close[t]
        fwd_ret = (df["Close"].shift(-HOLD_DAYS) / df["Close"] - 1)

        feats["fwd_ret_5d"] = fwd_ret
        feats["label"]      = (fwd_ret > 0).astype(int)
        feats["ticker"]     = ticker
        feats["date"]       = df.index

        # 只保留 backtest_start 之后、标签可用的行
        feats = feats[feats.index >= backtest_start]
        feats = feats.dropna(subset=["label", "fwd_ret_5d"])

        rows.append(feats)

    if not rows:
        raise RuntimeError("No valid feature rows built")

    combined = pd.concat(rows).sort_values(["date", "ticker"])
    feature_cols = [c for c in combined.columns
                    if c not in ("fwd_ret_5d", "label", "ticker", "date")]

    logger.info(f"[ML] 数据集: {len(combined)} 行 × {len(feature_cols)} 特征")
    return MLDataset(df=combined, tickers=valid_tickers,
                     feature_cols=feature_cols)


# ── 走步前向验证 ──────────────────────────────────────────────────

@dataclass
class FoldResult:
    train_end:   str
    test_start:  str
    test_end:    str
    n_train:     int
    n_test:      int
    auc:         float
    precision:   float   # win rate when model confident
    recall:      float
    avg_ret_model: float  # avg fwd return when model says buy
    avg_ret_all:   float  # avg fwd return for all samples
    n_model_buys:  int    # how many signals model generates


@dataclass
class WalkForwardResult:
    folds:            List[FoldResult] = field(default_factory=list)
    feature_importance: pd.DataFrame   = field(default_factory=pd.DataFrame)
    all_predictions:    pd.DataFrame   = field(default_factory=pd.DataFrame)
    overall_auc:       float = 0.0
    overall_precision: float = 0.0
    overall_avg_ret:   float = 0.0
    baseline_win_rate: float = 0.0
    baseline_avg_ret:  float = 0.0


def run_walk_forward(
    dataset: MLDataset,
    fold_months: int = 6,
) -> WalkForwardResult:
    """
    走步前向验证（expanding window）。
    训练集从 backtest_start 开始扩张，测试集每 fold_months 个月滚动。
    """
    if not _HAS_LGB:
        raise RuntimeError(
            "LightGBM 未能加载。macOS 请先运行: brew install libomp"
        )

    df = dataset.df.copy()
    df["date_ts"] = pd.to_datetime(df["date"])

    # 确定折点（每隔 fold_months 个月的月末）
    date_min = df["date_ts"].min()
    date_max = df["date_ts"].max()

    # 最早测试开始时间 = date_min + MIN_TRAIN_MONTHS
    min_train_end = date_min + pd.DateOffset(months=MIN_TRAIN_MONTHS)
    fold_starts   = pd.date_range(
        start=min_train_end,
        end=date_max - pd.DateOffset(months=fold_months),
        freq=f"{fold_months}MS",  # 每 fold_months 个月的月初
    )

    result = WalkForwardResult()
    all_preds: List[pd.DataFrame] = []
    fi_accum: Optional[pd.Series] = None

    feature_cols = dataset.feature_cols
    lgb_params = {
        "objective":        "binary",
        "metric":           "auc",
        "n_estimators":     300,
        "learning_rate":    0.04,
        "num_leaves":       31,
        "min_child_samples": 15,
        "subsample":        0.8,
        "colsample_bytree": 0.8,
        "reg_alpha":        0.1,
        "reg_lambda":       0.2,
        "random_state":     42,
        "verbose":          -1,
        "n_jobs":           -1,
    }

    baseline_rets: List[float] = []

    for fold_start in fold_starts:
        fold_end = fold_start + pd.DateOffset(months=fold_months) - pd.DateOffset(days=1)
        if fold_end > date_max:
            fold_end = date_max

        # ── 净化训练集：剔除 fold_start 前 HOLD_DAYS 个交易日 ──────────
        # 标签 = close[t+HOLD_DAYS]/close[t]-1，故 fold_start 前 HOLD_DAYS 天的
        # 训练行其前瞻标签会落入测试窗口 → 跨界泄漏（look-ahead）。purge 掉。
        train_days = np.sort(df.loc[df["date_ts"] < fold_start, "date_ts"].unique())
        if len(train_days) > HOLD_DAYS:
            train_mask = df["date_ts"] < train_days[-HOLD_DAYS]
        else:
            train_mask = df["date_ts"] < fold_start
        test_mask = (df["date_ts"] >= fold_start) & (df["date_ts"] <= fold_end)

        train_df = df[train_mask]
        X_test   = df[test_mask][feature_cols].fillna(0)
        y_test   = df[test_mask]["label"]

        if len(train_df) < 200 or len(X_test) < 10:
            continue
        if train_df["label"].nunique() < 2 or y_test.nunique() < 2:
            continue

        # ── 早停验证集：取净化训练集的时间末段，与拟合段再隔 HOLD_DAYS ──
        # eval_set 必须独立于测试折，否则 best_iteration 会借测试集反向选优，
        # 虚高样本外 AUC/精确率（信息泄漏）。
        tr_days = np.sort(train_df["date_ts"].unique())
        split_i = int(len(tr_days) * 0.8)
        if split_i > HOLD_DAYS and (len(tr_days) - split_i) > HOLD_DAYS:
            fit_df = train_df[train_df["date_ts"] < tr_days[split_i - HOLD_DAYS]]
            val_df = train_df[train_df["date_ts"] >= tr_days[split_i]]
        else:
            fit_df, val_df = train_df, None

        X_fit, y_fit = fit_df[feature_cols].fillna(0), fit_df["label"]
        if len(X_fit) < 200 or y_fit.nunique() < 2:
            continue

        model = lgb.LGBMClassifier(**lgb_params)
        if val_df is not None and val_df["label"].nunique() >= 2:
            model.fit(
                X_fit, y_fit,
                eval_set=[(val_df[feature_cols].fillna(0), val_df["label"])],
                callbacks=[lgb.early_stopping(30, verbose=False),
                           lgb.log_evaluation(-1)],
            )
        else:
            model.fit(X_fit, y_fit)

        proba = model.predict_proba(X_test)[:, 1]
        df.loc[test_mask, "pred_proba"] = proba

        # 指标
        auc = float(roc_auc_score(y_test, proba))
        model_buy_mask = proba >= CONF_THRESH
        n_model_buys = int(model_buy_mask.sum())

        test_rets = df[test_mask]["fwd_ret_5d"].values
        if model_buy_mask.sum() > 0:
            avg_ret_model = float(test_rets[model_buy_mask].mean())
            precision     = float((y_test.values[model_buy_mask] == 1).mean())
        else:
            avg_ret_model = 0.0
            precision     = 0.0

        avg_ret_all = float(test_rets.mean())
        recall      = float(
            (y_test.values[model_buy_mask] == 1).sum() / (y_test == 1).sum()
            if (y_test == 1).sum() > 0 else 0
        )

        baseline_rets.extend(test_rets.tolist())

        fold_res = FoldResult(
            train_end=str((fold_start - pd.DateOffset(days=1)).date()),
            test_start=str(fold_start.date()),
            test_end=str(fold_end.date()),
            n_train=int(len(X_fit)),
            n_test=int(len(X_test)),
            auc=auc,
            precision=precision,
            recall=recall,
            avg_ret_model=avg_ret_model,
            avg_ret_all=avg_ret_all,
            n_model_buys=n_model_buys,
        )
        result.folds.append(fold_res)

        # 累积特征重要性
        fi = pd.Series(model.feature_importances_, index=feature_cols)
        fi_accum = fi if fi_accum is None else fi_accum + fi

        logger.info(
            f"[ML] 折 {fold_start.date()}~{fold_end.date()} "
            f"AUC={auc:.3f} 精确率={precision:.1%} 模型信号={n_model_buys}"
        )

        # 保存预测列
        fold_pred = df[test_mask][["date", "ticker", "fwd_ret_5d", "label"]].copy()
        fold_pred["pred_proba"] = proba
        fold_pred["model_buy"]  = (proba >= CONF_THRESH).astype(int)
        all_preds.append(fold_pred)

    if not result.folds:
        logger.warning("[ML] 无有效折，检查数据量")
        return result

    # 汇总
    all_pred_df = pd.concat(all_preds)
    result.all_predictions = all_pred_df

    buy_preds = all_pred_df[all_pred_df["model_buy"] == 1]
    result.overall_auc      = float(np.mean([f.auc for f in result.folds]))
    result.overall_precision = float(
        (buy_preds["label"] == 1).mean() if len(buy_preds) > 0 else 0
    )
    result.overall_avg_ret  = float(
        buy_preds["fwd_ret_5d"].mean() if len(buy_preds) > 0 else 0
    )
    result.baseline_win_rate = float((all_pred_df["label"] == 1).mean())
    result.baseline_avg_ret  = float(all_pred_df["fwd_ret_5d"].mean())

    # 特征重要性（归一化）
    if fi_accum is not None:
        result.feature_importance = (
            (fi_accum / fi_accum.sum())
            .sort_values(ascending=False)
            .head(25)
            .reset_index()
            .rename(columns={"index": "feature", 0: "importance"})
        )

    return result


# ── 规则策略基准（用于对比）────────────────────────────────────────

def rule_based_win_rate(dataset: MLDataset) -> Tuple[float, float, int]:
    """
    规则策略基准：缠论有买点（chan_is_buy==1）时做多，统计胜率。
    返回：(win_rate, avg_ret, n_signals)
    """
    df = dataset.df
    buy_rows = df[df["chan_is_buy"] == 1]
    if len(buy_rows) == 0:
        return 0.0, 0.0, 0
    win_rate = float((buy_rows["label"] == 1).mean())
    avg_ret  = float(buy_rows["fwd_ret_5d"].mean())
    return win_rate, avg_ret, len(buy_rows)


# ── 缠论门控 ML 确认（洞察驱动的胜率增强）──────────────────────────

@dataclass
class ChanGatedResult:
    """缠论买点 + ML 确认层 vs 缠论单独 的对比（仅走步前向测试期，无前视）。"""
    split_proba:        float = 0.0   # 切分点（缠论信号 ML 置信度中位数）
    chan_only_n:        int   = 0
    chan_only_win_rate: float = 0.0
    chan_only_avg_ret:  float = 0.0
    chan_ml_n:          int   = 0     # ML 高置信半区（确认）
    chan_ml_win_rate:   float = 0.0
    chan_ml_avg_ret:    float = 0.0
    rejected_n:         int   = 0     # ML 低置信半区（否决）
    rejected_win_rate:  float = 0.0
    rejected_avg_ret:   float = 0.0
    win_rate_lift:      float = 0.0


def chan_gated_analysis(
    dataset: MLDataset,
    result: WalkForwardResult,
    conf_thresh: Optional[float] = None,
) -> Optional[ChanGatedResult]:
    """
    测试 ML 作为缠论信号"确认层"的判别力。

    缠论买点本身是看多形态，模型对其几乎都给 >0.5，固定阈值无法切分。
    故默认用缠论信号 ML 置信度的**中位数**切分：
      - 高置信半区（≥中位数）→ ML 确认
      - 低置信半区（<中位数）→ ML 否决
    若高置信半区胜率显著高于低置信半区，说明 ML 在缠论信号内部
    仍有方向判别力，可作为优先级排序 / 假阳性过滤。

    仅在走步前向测试期（out-of-sample）内统计。
    """
    preds = result.all_predictions
    if preds.empty:
        return None

    chan_cols = dataset.df[["date", "ticker", "chan_is_buy"]].copy()
    merged = preds.merge(chan_cols, on=["date", "ticker"], how="left")

    chan_buys = merged[merged["chan_is_buy"] == 1]
    if len(chan_buys) < 20:
        logger.info(f"[ML] 测试期缠论买点仅 {len(chan_buys)} 个，门控分析样本不足")
        return None

    split = float(conf_thresh) if conf_thresh is not None else float(chan_buys["pred_proba"].median())

    confirmed = chan_buys[chan_buys["pred_proba"] >= split]
    rejected  = chan_buys[chan_buys["pred_proba"] <  split]

    res = ChanGatedResult(split_proba=split)
    res.chan_only_n        = len(chan_buys)
    res.chan_only_win_rate = float((chan_buys["label"] == 1).mean())
    res.chan_only_avg_ret  = float(chan_buys["fwd_ret_5d"].mean())

    res.chan_ml_n        = len(confirmed)
    res.chan_ml_win_rate = float((confirmed["label"] == 1).mean()) if len(confirmed) else 0.0
    res.chan_ml_avg_ret  = float(confirmed["fwd_ret_5d"].mean()) if len(confirmed) else 0.0

    res.rejected_n        = len(rejected)
    res.rejected_win_rate = float((rejected["label"] == 1).mean()) if len(rejected) else 0.0
    res.rejected_avg_ret  = float(rejected["fwd_ret_5d"].mean()) if len(rejected) else 0.0

    res.win_rate_lift = res.chan_ml_win_rate - res.chan_only_win_rate

    logger.info(
        f"[ML] 缠论门控(切分@{split:.3f}): 单独={res.chan_only_win_rate:.1%}({res.chan_only_n}) "
        f"高置信={res.chan_ml_win_rate:.1%}({res.chan_ml_n}) "
        f"低置信={res.rejected_win_rate:.1%}({res.rejected_n}) "
        f"提升={res.win_rate_lift:+.1%}"
    )
    return res


# ── 报告 ──────────────────────────────────────────────────────────

def build_ml_report(result: WalkForwardResult, dataset: MLDataset, date_str: str) -> str:
    lines = [
        f"# LightGBM ML 历史回测报告 — {date_str}",
        "",
        "> **方法说明**  ",
        "> 走步前向验证（Expanding Window Walk-Forward）  ",
        f"> 训练集：2022-01-01 起扩张；测试集：每 6 个月向前滚动  ",
        f"> 预测目标：{HOLD_DAYS} 交易日后收益 > 0（做多盈利）  ",
        f"> 模型做多门槛：置信度 ≥ {CONF_THRESH:.0%}  ",
        f"> 宇宙：{len(dataset.tickers)} 只股票",
        "",
    ]

    # 基准
    rule_wr, rule_ret, rule_n = rule_based_win_rate(dataset)
    lines += [
        "## 基准对比",
        "",
        "| 策略 | 胜率 | 均 5TD 收益 | 信号数 |",
        "|------|------|-----------|--------|",
        f"| 随机基准（所有样本均做多） | {result.baseline_win_rate:.1%} | {result.baseline_avg_ret:+.2%} | 全部 |",
        f"| 规则策略（缠论买点触发） | {rule_wr:.1%} | {rule_ret:+.2%} | {rule_n} |",
        f"| ML 策略（置信度≥{CONF_THRESH:.0%}） | {result.overall_precision:.1%} | {result.overall_avg_ret:+.2%}"
        f" | {len(result.all_predictions[result.all_predictions['model_buy']==1]) if not result.all_predictions.empty else 0} |",
        "",
    ]

    # 缠论门控 ML 确认（核心洞察验证）
    gated = chan_gated_analysis(dataset, result)
    if gated:
        sep = gated.chan_ml_win_rate - gated.rejected_win_rate
        lines += [
            "## 🎯 缠论门控 + ML 确认（核心策略增强）",
            "",
            "> 仅在走步前向**测试期**统计（out-of-sample）。缠论买点本身是看多形态，",
            f"> 模型对其几乎都给 >0.5，故按 ML 置信度**中位数 {gated.split_proba:.3f}** 切分，",
            "> 检验高置信半区是否真比低置信半区胜率高（即 ML 是否有内部判别力）。",
            "",
            "| 子集 | 信号数 | 胜率 | 均 5TD 收益 |",
            "|------|--------|------|-----------|",
            f"| 缠论买点（全部） | {gated.chan_only_n} | {gated.chan_only_win_rate:.1%}"
            f" | {gated.chan_only_avg_ret:+.2%} |",
            f"| **ML 高置信半区**（确认） | {gated.chan_ml_n}"
            f" | **{gated.chan_ml_win_rate:.1%}** | {gated.chan_ml_avg_ret:+.2%} |",
            f"| ML 低置信半区（否决） | {gated.rejected_n} | {gated.rejected_win_rate:.1%}"
            f" | {gated.rejected_avg_ret:+.2%} |",
            "",
        ]
        if sep > 0.05:
            lines.append(
                f"✅ **ML 确认有判别力**：高置信半区胜率 {gated.chan_ml_win_rate:.1%} "
                f"vs 低置信半区 {gated.rejected_win_rate:.1%}（相差 {sep:+.1%}）。"
                f"实战可用 ML 对缠论信号做优先级排序，重仓高置信信号、轻仓或跳过低置信信号。"
            )
        elif sep < -0.05:
            lines += [
                f"⚠️ **关键发现：ML 置信度与缠论成功率负相关**（高置信 {gated.chan_ml_win_rate:.1%} "
                f"< 低置信 {gated.rejected_win_rate:.1%}，相差 {sep:.1%}）。",
                "",
                "**机理**：缠论买点多出现在回调/超卖的结构底部（buy the dip），此时近期动量为负；"
                "而 ML 主要学到的是动量与宏观模式，会对刚下跌的股票给低置信。"
                "**缠论的超额收益恰恰来自动量难看的位置——正是 ML 想过滤掉的地方。**",
                "",
                "**结论**：缠论（逆势/均值回归）与动量 ML 捕捉的是**相反的边**，"
                "不能用 ML 做缠论的确认层。两者应**并行独立**，而非串联过滤。"
                "若要叠加，方向应反过来：动量 ML 适合确认趋势突破型信号，不适合确认结构底部信号。",
            ]
        else:
            lines.append(
                f"➖ **ML 无额外判别力**：两个半区胜率接近（相差 {sep:+.1%}），"
                f"缠论信号质量已均匀，ML 置信度无法进一步区分赢家输家。"
            )
        lines.append("")

    # 走步折叠明细
    lines += [
        "## 走步前向验证 — 各折明细",
        "",
        "| 测试区间 | 训练样本 | 测试样本 | AUC | 精确率（胜率） | 均收益 | ML信号数 |",
        "|---------|---------|---------|-----|-------------|-------|---------|",
    ]
    for f in result.folds:
        lines.append(
            f"| {f.test_start}~{f.test_end} | {f.n_train:,} | {f.n_test:,} | "
            f"{f.auc:.3f} | {f.precision:.1%} | {f.avg_ret_model:+.2%} | {f.n_model_buys} |"
        )
    lines += [
        "",
        f"**综合 AUC（均值）**: {result.overall_auc:.3f}  ",
        f"**综合精确率（胜率）**: {result.overall_precision:.1%}  ",
        f"**综合均 5TD 收益**: {result.overall_avg_ret:+.2%}",
        "",
    ]

    # 特征重要性
    if not result.feature_importance.empty:
        lines += [
            "## 特征重要性 Top 25",
            "",
            "| 排名 | 特征 | 重要性 | 含义 |",
            "|------|------|--------|------|",
        ]
        fi_explain = {
            "ret_5d": "5日过去收益", "ret_10d": "10日过去收益",
            "ret_20d": "20日过去收益", "ret_60d": "60日过去收益",
            "rsi14": "RSI(14) 超买超卖",
            "roc20": "20日变化率", "roc5": "5日变化率",
            "macd_hist": "MACD 柱面积", "adx14": "ADX(14) 趋势强度",
            "sma20_ratio": "收盘/SMA20 偏离", "sma60_ratio": "收盘/SMA60 偏离",
            "sma200_ratio": "收盘/SMA200 偏离",
            "sma20_slope": "SMA20 斜率", "sma_20_60": "SMA20/SMA60 金叉",
            "vol_ratio": "成交量/均量 比",
            "obv_slope": "OBV 趋势斜率", "hl_ratio": "真实波幅/收盘",
            "rel_qqq_20d": "vs QQQ 20日超额", "rel_spy_20d": "vs SPY 20日超额",
            "rel_qqq_60d": "vs QQQ 60日超额", "rel_spy_60d": "vs SPY 60日超额",
            "rel_zscore": "相对强度 Z-score",
            "vix_level": "VIX 绝对值", "vix_regime": "VIX 制度档位",
            "vix_rising": "VIX 上升中", "vix_pct252": "VIX 历史分位",
            "vix_sma20": "VIX 20日均值", "yield_30_10": "30Y-10Y 利差",
            "tnx_level": "10Y 收益率",
            # 新增统计特征
            "log_ret_1d": "1日对数收益",
            "realized_vol_5d": "5日已实现波动率", "realized_vol_20d": "20日已实现波动率",
            "realized_vol_60d": "60日已实现波动率",
            "ret_mean_20d": "20日收益均值", "ret_mean_60d": "60日收益均值",
            "ret_std_20d": "20日收益标准差", "ret_std_60d": "60日收益标准差",
            "ret_skew_20d": "20日收益偏度", "ret_skew_60d": "60日收益偏度",
            "ret_median_20d": "20日收益中位数", "ret_median_60d": "60日收益中位数",
            "sharpe_20d": "20日滚动夏普", "sharpe_60d": "60日滚动夏普",
            "ewm_ret_10d": "EWM 收益(span10)", "ewm_vol_20d": "EWM 波动率(span20)",
            "ret_accel": "收益加速度(5日差分)", "vol_accel": "波动率加速度",
            "pos_in_range_20d": "20日区间位置", "pos_in_range_60d": "60日区间位置",
            "price_zscore_20d": "价格20日Z-score", "price_zscore_60d": "价格60日Z-score",
            "chan_is_buy": "缠论买点", "chan_is_sell": "缠论卖点",
            "chan_score": "缠论得分", "chan_type_enc": "缠论买卖点类型",
            "chan_days_since_buy": "距上次缠论买点天数",
            "chan_buy_count_20d": "20日缠论买点频率",
        }
        fi_df = result.feature_importance
        if "importance" not in fi_df.columns and len(fi_df.columns) >= 2:
            fi_df = fi_df.rename(columns={fi_df.columns[0]: "feature", fi_df.columns[1]: "importance"})
        for i, row in fi_df.iterrows():
            name   = str(row.get("feature", row.iloc[0]))
            imp    = float(row.get("importance", row.iloc[1]))
            explain = fi_explain.get(name, "—")
            bar    = "█" * int(imp * 100) + "░" * (10 - int(imp * 100))
            lines.append(f"| {int(i)+1} | `{name}` | {bar} {imp:.2%} | {explain} |")
        lines.append("")

    # 解读
    lines += _interpretation(result, rule_wr, rule_ret)

    lines += [
        "",
        "---",
        f"*生成时间: {date_str}  |  LightGBM {HOLD_DAYS}TD 分类  |  宇宙 {len(dataset.tickers)} 只*",
    ]
    return "\n".join(lines)


def _interpretation(result: WalkForwardResult, rule_wr: float, rule_ret: float) -> List[str]:
    lines = ["## 策略解读", ""]

    auc = result.overall_auc
    ml_wr = result.overall_precision

    if auc > 0.56:
        lines.append(f"- **AUC={auc:.3f}** > 0.56：特征对 5TD 方向具有显著预测力，模型优于随机。")
    elif auc > 0.52:
        lines.append(f"- **AUC={auc:.3f}** 轻微超过随机（0.50），预测力有限但存在。")
    else:
        lines.append(f"- **AUC={auc:.3f}** 接近随机，当前特征在该时间维度预测力不足。")

    gap = rule_wr - ml_wr
    if ml_wr > rule_wr + 0.05:
        lines.append(f"- ML 策略胜率（{ml_wr:.1%}）显著高于规则策略（{rule_wr:.1%}），ML 过滤有附加价值。")
    elif ml_wr > rule_wr:
        lines.append(f"- ML 策略胜率（{ml_wr:.1%}）略高于规则策略（{rule_wr:.1%}）。")
    elif gap > 0.10:
        lines.append(
            f"- ⚠️ 缠论规则策略（{rule_wr:.1%}）远优于 ML（{ml_wr:.1%}，差 {gap:.1%}）。"
            f" 原因：缠论信号稀少且为逆势结构底部，与动量型 ML 捕捉相反的边（见门控分析）。"
            f" **二者应并行独立运行**，缠论负责择时入场、ML 负责趋势型机会，不串联过滤。"
        )
    else:
        lines.append(
            f"- 缠论规则策略（{rule_wr:.1%}）优于 ML（{ml_wr:.1%}），"
            f"规则信号的稀少性本身就是质量保证。"
        )

    if result.overall_avg_ret > result.baseline_avg_ret:
        lift = result.overall_avg_ret - result.baseline_avg_ret
        lines.append(f"- ML 筛选后均 5TD 收益（{result.overall_avg_ret:+.2%}）比基准高 {lift:+.2%}。")

    # 建议
    lines += [
        "",
        "### 改进建议",
        "",
    ]
    if len(result.folds) >= 3:
        aucs = [f.auc for f in result.folds]
        if aucs[-1] > aucs[0]:
            lines.append("- 近期折 AUC 持续提升，当前市场结构对该特征组合友好。")
        elif aucs[-1] < aucs[0] - 0.05:
            lines.append("- 近期折 AUC 下降，市场结构可能发生变化，建议增加近期数据权重。")

    if not result.feature_importance.empty:
        fi = result.feature_importance
        if "importance" not in fi.columns:
            fi = fi.rename(columns={fi.columns[0]: "feature", fi.columns[1]: "importance"})
        top3 = fi.head(3)["feature"].tolist() if len(fi) >= 3 else []
        if "chan_is_buy" in top3 or "chan_score" in top3:
            lines.append("- 缠论特征进入 Top 3，结构性择时对短期收益有实质影响。")
        if any("rel_" in f for f in top3):
            lines.append("- 相对强度是核心驱动力，动量因子在该宇宙中有效。")
        if any("vix" in f for f in top3):
            lines.append("- 宏观波动率是重要过滤器，VIX 制度切换显著影响胜率。")

    return lines


def write_ml_report(result: WalkForwardResult, dataset: MLDataset,
                    output_dir: Path, date_str: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    content = build_ml_report(result, dataset, date_str)
    path = output_dir / "ml_backtest_report.md"
    path.write_text(content, encoding="utf-8")
    return path
