"""TimesFM 3.0 前瞻性特征（R10）。

产出现有特征体系中不存在的一类量：**前瞻**的预测分布。`compute_statistical_features`
里的 `realized_vol_*` / `ret_skew_*` 全部是后视已实现量；本模块给出的是模型对未来
`horizon` 个交易日的分位数预测。两者是否只是同一件事的两种写法，由验收环节的正交化
检验回答（见 PRD R10 §6.1），本模块不做判断。

────────────────────────────────────────────────────────────────────────
点位时序正确性（本模块唯一的核心风险）
────────────────────────────────────────────────────────────────────────
`compute_statistical_features` 靠 pandas rolling/ewm 天然后视，无需额外保证。
**本模块没有这个天然保证**：TimesFM 内部的 RevIN 与线性去趋势会用整个输入窗口做
归一化，若图省事对整条序列算一次，泄漏会渗进归一化统计量，肉眼完全看不出来。

因此本模块严格按日循环：日期 `t` 的特征只喂 `close_panel.loc[:t]` 的尾部窗口。
这与 `run_walk_forward` 的 label purge/embargo 是**正交的两个轴**——那一层保护
不了特征构造期的泄漏。`test_timesfm_features.py` 用"改写末尾数据不得影响历史特征值"
做断言。

────────────────────────────────────────────────────────────────────────
为什么喂价格而不是收益率
────────────────────────────────────────────────────────────────────────
TimesFM 的 RevIN + 线性去趋势是为水平序列设计的，预训练语料（Wikipedia Pageviews、
Google Trends、GiftEvalPretrain）也都是水平序列。近白噪的收益率序列对它属于分布外
输入。故喂已复权收盘价，收益率维度的量再由 `/ close_t` 归一化导出。

────────────────────────────────────────────────────────────────────────
上下文长度与覆盖率
────────────────────────────────────────────────────────────────────────
一个标的必须在窗口内有 `context` 根**连续无缺失**的 K 线才会进入当日面板。这是为了
避开 TimesFM 的 NaN 兜底：其 `linear_interpolation` 用 `np.interp`，边缘钳制，会用
上市首日价格向前填满 IPO 之前的历史（ARM 2023-09 上市、SNDK 2025 分拆），然后把
伪造出来的价格喂进 cross-variate attention。宁可少覆盖，不可喂假数据。

代价是短历史标的在够长之前没有特征。`context=256` 配合 `build_dataset` 默认的
`start="2020-11-01"` 可在 `backtest_start="2022-01-01"` 起提供覆盖；若要用
`context=512`，需把 `start` 提前到约 2019 年，否则回测第一年全部标的都没有特征
（本模块会就此告警，不静默降级）。

────────────────────────────────────────────────────────────────────────
协变量要么整段有，要么那一天不出特征（2026-09-16 加）
────────────────────────────────────────────────────────────────────────
四个特征**全部**取自 `joint` 预测，而 `joint` 是否带 QQQ/SPY 协变量会改变它们的含义
（`tfm_joint_gap_5d` 尤甚：带协变量时它同时含 cross-variate attention 与协变量影响，
不带时只剩 attention）。因此**协变量完整性与价格完整性同级**，走同一道 rolling 门：
`_covariate_ready` 判断截至该日的 `context` 根窗口内协变量是否全有效，不合格的日期
**整天不产出特征**，而不是"这天悄悄不带协变量"。

🔴 **早先的实现是按 chunk 丢**（`date_batch` 个日期里任一天有 NaN 就整块丢协变量），
后果有二：① 同一份面板在 `date_batch=1` 与 `=32` 下算出**不同的特征值**，
而 `test_date_batching_does_not_change_features` 正是为这条不变量写的 ——
它能通过只因为跑的时候 `cov_panel=None`；② 一个 Yahoo NaN 占位日会让其后 `context`
天的特征在无告警的情况下换一种含义（见 [[insight_yf_nan_tail_bar]]，那正是本仓已知的
数据缺陷形态）。**一根坏的协变量 K 线会废掉其后 context 天，这是设计意图不是 bug**
—— 该去修数据源，不是放松这道门。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
from loguru import logger

# 与 ml_backtest.HOLD_DAYS 对齐；此处独立定义以避免循环导入。
DEFAULT_HORIZON = 5
DEFAULT_CONTEXT = 256

FEATURE_COLUMNS = [
    "tfm_drift_5d",
    "tfm_qspread_5d",
    "tfm_skew_5d",
    "tfm_joint_gap_5d",
]


@dataclass(frozen=True)
class TimesFMFeatureConfig:
    """特征生成配置。改变其中任何一项都会改变缓存键。"""

    horizon: int = DEFAULT_HORIZON
    context: int = DEFAULT_CONTEXT
    checkpoint: str = "google/timesfm-3.0-pytorch"
    device: str = "cpu"          # 本机无 CUDA；MPS 在此规模下慢于 CPU
    per_core_batch_size: int = 64
    date_batch: int = 32         # 一次前向里塞多少个日期（同一 ticker 集合内）
    include_joint_gap: bool = True

    def cache_parts(self) -> tuple:
        return (
            "tfm_feat_v1",
            self.horizon,
            self.context,
            self.checkpoint,
            self.per_core_batch_size,
            self.include_joint_gap,
        )


# ── 面板构造 ──────────────────────────────────────────────────────────


def build_close_panel(
    raw: dict[str, pd.DataFrame], tickers: Sequence[str]
) -> pd.DataFrame:
    """把 `build_dataset` 的 `raw` 字典转成 (date × ticker) 收盘价宽表。

    只取 `tickers` 中确实存在于 `raw` 的标的；索引为各标的日期索引的并集，缺失留 NaN
    （由 `_qualified_mask` 负责排除，不做任何填充）。
    """
    cols = {t: raw[t]["Close"] for t in tickers if t in raw}
    if not cols:
        return pd.DataFrame()
    panel = pd.DataFrame(cols).sort_index()
    panel.index = pd.to_datetime(panel.index)
    return panel


def _qualified_mask(panel: pd.DataFrame, context: int) -> pd.DataFrame:
    """(date × ticker) 布尔表：该标的在截至该日的 `context` 根窗口内是否**全部有效**。

    用 rolling sum 而非 cumsum：后者只统计累计有效根数，会放过窗口内部的缺口。
    """
    valid = panel.notna().astype(float)
    return valid.rolling(context).sum() == context


def _covariate_ready(cov_panel: pd.DataFrame, context: int) -> pd.Series:
    """按日判断：截至该日的 `context` 根协变量窗口是否**全部列、全部有效**。

    与 `_qualified_mask` 同款 rolling 判据（而非 cumsum），理由相同：cumsum 会放过
    窗口内部的缺口。要求 `all(axis=1)` 是因为协变量是一组一起喂进去的，缺一列就不是
    同一个输入。

    ⚠️ 调用方必须先把 `cov_panel` reindex 到目标面板的索引上，否则这里判的是另一条
    时间轴 —— 见本函数唯一调用点。
    """
    if cov_panel is None or cov_panel.empty:
        return pd.Series(dtype=bool)
    per_day = cov_panel.notna().all(axis=1).astype(float)
    return (per_day.rolling(context).sum() == context).fillna(False)


# ── 主入口 ────────────────────────────────────────────────────────────


def compute_timesfm_features(
    close_panel: pd.DataFrame,
    cov_panel: pd.DataFrame | None = None,
    config: TimesFMFeatureConfig | None = None,
    start: str | pd.Timestamp | None = None,
    cache=None,
) -> pd.DataFrame:
    """逐日生成 TimesFM 前瞻性特征。

    Args:
      close_panel: (date × ticker) 已复权收盘价宽表。
      cov_panel:   (date × 基准) 过去协变量（QQQ/SPY）。用 past-only 而非
                   past-future——我们不知道未来的 QQQ。
      config:      见 `TimesFMFeatureConfig`。
      start:       只为该日期起的行生成特征（省算力）。之前的行不出现在结果中。
      cache:       可选 `data.cache.SQLiteCache`。

    Returns:
      MultiIndex (date, ticker) 的 DataFrame，列为 `FEATURE_COLUMNS`。
      未达 `context` 覆盖要求的 (date, ticker) 不出现在结果中（而非填 0）。
    """
    cfg = config or TimesFMFeatureConfig()
    if close_panel is None or close_panel.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS)

    cached = _cache_get(cache, close_panel, cov_panel, cfg, start)
    if cached is not None:
        logger.info(f"[TFM] 缓存命中：{len(cached)} 行特征")
        return cached

    qualified = _qualified_mask(close_panel, cfg.context)
    dates = close_panel.index
    if start is not None:
        dates = dates[dates >= pd.Timestamp(start)]

    # 协变量必须先对齐到目标面板的时间轴上再判缺口。两者的索引是各自
    # ticker 日期的并集，独立构造 —— 不 reindex 就会出现「两边都是 context 长、
    # 形状检查全过、覆盖的却是不同日期区间」的静默错位。
    cov_aligned = None
    if cov_panel is not None and not cov_panel.empty:
        cov_aligned = cov_panel.reindex(close_panel.index)

    price_ok = [d for d in dates if bool(qualified.loc[d].any())]
    usable = price_ok
    if cov_aligned is not None:
        cov_ok = _covariate_ready(cov_aligned, cfg.context)
        usable = [d for d in price_ok if bool(cov_ok.get(d, False))]
        _log_covariate_gate(price_ok, usable, cov_aligned, cfg)

    if not usable:
        if cov_aligned is not None and price_ok:
            logger.error(
                f"[TFM] {len(price_ok)} 个日期价格合格，但**没有一天**的协变量窗口完整"
                f"（context={cfg.context}，列 {list(cov_aligned.columns)}）。"
                f" 这几乎一定是基准数据有缺口 —— 先修数据源，不要绕过这道门。"
            )
        else:
            logger.warning(
                f"[TFM] 没有任何 (date, ticker) 满足 context={cfg.context} 的连续历史要求。"
                f" 面板共 {len(close_panel)} 行 —— 请把下载起点提前，或调小 context。"
            )
        return pd.DataFrame(columns=FEATURE_COLUMNS)

    _log_coverage(qualified, usable, cfg)

    forecaster = _load_forecaster(cfg)
    rows: list[pd.DataFrame] = []
    total_forwards = 0

    for ticker_set, group_dates in _group_by_ticker_set(qualified, usable):
        cols = list(ticker_set)
        for chunk in _chunks(group_dates, cfg.date_batch):
            contexts = [
                close_panel.loc[:d, cols].iloc[-cfg.context :].to_numpy(
                    dtype=np.float32
                ).T
                for d in chunk
            ]
            po = None
            if cov_aligned is not None:
                po = [
                    cov_aligned.loc[:d].iloc[-cfg.context :]
                    .to_numpy(dtype=np.float32).T
                    for d in chunk
                ]
                # 断言而非兜底：`usable` 已按 `_covariate_ready` 逐日筛过，这里再出
                # NaN 只可能是筛选与取数走了两条不同的路径 —— 那是 bug，必须炸出来，
                # 不能悄悄退化成"这一批不带协变量"（那会让特征值依赖 date_batch）。
                assert not any(np.isnan(a).any() for a in po), (
                    f"[TFM] 协变量门与取数不一致：{chunk[0]}~{chunk[-1]} 仍含 NaN"
                )

            joint = _predict(forecaster, contexts, cfg, po, univariate=False)
            total_forwards += 1
            solo = None
            if cfg.include_joint_gap:
                # univariate=True 时 evaluator 会丢弃协变量并把通道拆成独立序列，
                # 正是我们要的"无横截面信息"对照。
                solo = _predict(forecaster, contexts, cfg, None, univariate=True)
                total_forwards += 1

            for i, d in enumerate(chunk):
                last_close = close_panel.loc[d, cols].to_numpy(dtype=np.float64)
                rows.append(
                    _features_for_date(
                        date=d,
                        tickers=cols,
                        last_close=last_close,
                        joint_q=joint[i],
                        solo_q=None if solo is None else solo[i],
                    )
                )

    out = (
        pd.concat(rows).sort_index()
        if rows
        else pd.DataFrame(columns=FEATURE_COLUMNS)
    )
    logger.info(
        f"[TFM] 生成 {len(out)} 行 × {len(FEATURE_COLUMNS)} 特征"
        f"（{len(usable)} 个日期，{total_forwards} 次前向）"
    )
    _cache_set(cache, close_panel, cov_panel, cfg, start, out)
    return out


# ── 内部实现 ──────────────────────────────────────────────────────────


def _load_forecaster(cfg: TimesFMFeatureConfig):
    from timesfm3 import ModelConfig, TimesFM3Evaluator

    logger.info(f"[TFM] 加载 {cfg.checkpoint} (device={cfg.device}) ...")
    return TimesFM3Evaluator(
        ModelConfig(
            checkpoint_path=cfg.checkpoint,
            per_core_batch_size=cfg.per_core_batch_size,
            device=cfg.device,
        )
    )


def _predict(
    forecaster,
    contexts: list[np.ndarray],
    cfg: TimesFMFeatureConfig,
    past_only: list[np.ndarray] | None,
    *,
    univariate: bool,
) -> list[np.ndarray]:
    """返回每个输入的 quantiles，形状 (n_ticker, horizon, n_quantile)。

    `use_symmetric_averaging=False`：对称平均会把价格序列取负后再预测，对水平价格
    序列没有意义，且使前向次数翻倍。
    """
    outs = list(
        forecaster.predict_batch(
            contexts=contexts,
            horizon=cfg.horizon,
            past_only_covariates=past_only,
            return_quantiles=True,
            use_symmetric_averaging=False,
            univariate=univariate,
        )
    )
    return [o.quantiles for o in outs]


def _features_for_date(
    date,
    tickers: list[str],
    last_close: np.ndarray,
    joint_q: np.ndarray,
    solo_q: np.ndarray | None,
) -> pd.DataFrame:
    """由分位数预测导出特征。

    取 horizon 末端（第 `horizon` 个交易日）的分位数，与 `fwd_ret_5d` 标签同步：
    标签是 `Close.shift(-5)/Close - 1`，故对照的是 t+5 的预测分布。
    分位数顺序为 [0.1 … 0.9]，故 index 0/4/8 分别是 q10/q50/q90。
    """
    q10 = joint_q[:, -1, 0].astype(np.float64)
    q50 = joint_q[:, -1, 4].astype(np.float64)
    q90 = joint_q[:, -1, 8].astype(np.float64)

    denom = np.where(np.abs(last_close) > 1e-9, last_close, np.nan)
    spread = q90 - q10

    data = {
        # 5 日预测收益率。与动量类特征高度共线，先验预期 REJECT。
        "tfm_drift_5d": q50 / denom - 1.0,
        # 前瞻离散度。对照物是后视的 realized_vol_20d，须做正交化检验。
        "tfm_qspread_5d": spread / denom,
        # 预测分布的不对称度，落在 [-1, 1]。无后视类比项。
        "tfm_skew_5d": np.where(
            np.abs(spread) > 1e-12, (q90 + q10 - 2.0 * q50) / spread, 0.0
        ),
    }
    if solo_q is not None:
        solo50 = solo_q[:, -1, 4].astype(np.float64)
        # 联合预测与独立预测之差：度量横截面结构 + 市场协变量对该股当前的解释力。
        # 注意它同时包含 cross-variate attention 与协变量两种影响，不可分离。
        data["tfm_joint_gap_5d"] = np.abs(q50 - solo50) / denom
    else:
        data["tfm_joint_gap_5d"] = np.full(len(tickers), np.nan)

    idx = pd.MultiIndex.from_product([[date], tickers], names=["date", "ticker"])
    return pd.DataFrame(data, index=idx)[FEATURE_COLUMNS]


def _group_by_ticker_set(
    qualified: pd.DataFrame, dates: list
) -> Iterable[tuple[tuple[str, ...], list]]:
    """把连续、且合格标的集合相同的日期归为一组，供一次前向批量处理。

    合格集合只在某个标的历史攒够时才变化（全窗口内寥寥数次），所以绝大多数日期
    会落进同一组 —— 这是本模块能把上千次单日前向压到几十次批量前向的原因。
    """
    current: tuple[str, ...] | None = None
    bucket: list = []
    for d in dates:
        row = qualified.loc[d]
        cols = tuple(row.index[row.to_numpy(dtype=bool)])
        if cols != current:
            if bucket:
                yield current, bucket  # type: ignore[misc]
            current, bucket = cols, [d]
        else:
            bucket.append(d)
    if bucket:
        yield current, bucket  # type: ignore[misc]


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _log_coverage(
    qualified: pd.DataFrame, usable: list, cfg: TimesFMFeatureConfig
) -> None:
    """报告每个标的的覆盖率。

    短历史标的（IPO/分拆）会大面积缺特征，这是 §上下文长度与覆盖率 的既定代价，
    但必须让调用方看见 —— 否则 REJECT 结论会被误读成"因子无效"，实际是"没数据"。
    """
    sub = qualified.loc[usable]
    cov = sub.mean(axis=0).sort_values()
    thin = cov[cov < 0.95]
    logger.info(f"[TFM] 可用日期 {len(usable)}，标的覆盖率中位数 {cov.median():.1%}")
    for ticker, frac in thin.items():
        level = logger.warning if frac < 0.5 else logger.info
        level(
            f"[TFM]   {ticker} 仅覆盖 {frac:.1%} 的回测日期"
            f"（历史不足 context={cfg.context}）"
        )


def _log_covariate_gate(
    price_ok: list, usable: list, cov_aligned: pd.DataFrame, cfg: TimesFMFeatureConfig
) -> None:
    """报告协变量门砍掉了哪些日期。

    **必须点名区间**：一根坏的基准 K 线会连累其后 `context` 天（≈一年），
    只报一个百分比的话，"少了一年"和"少了三天"长得一模一样。
    """
    dropped = sorted(set(price_ok) - set(usable))
    if not dropped:
        logger.info(
            f"[TFM] 协变量门：{len(usable)} 个日期全部通过"
            f"（{list(cov_aligned.columns)}，context={cfg.context}）"
        )
        return

    # 把连续的被砍日期折成区间，便于一眼看出是"零星几天"还是"整段"
    spans, run = [], [dropped[0]]
    pos = {d: i for i, d in enumerate(price_ok)}
    for prev, cur in zip(dropped, dropped[1:]):
        if pos[cur] == pos[prev] + 1:
            run.append(cur)
        else:
            spans.append((run[0], run[-1], len(run)))
            run = [cur]
    spans.append((run[0], run[-1], len(run)))

    frac = len(dropped) / len(price_ok)
    level = logger.error if frac > 0.5 else logger.warning
    level(
        f"[TFM] 协变量门砍掉 {len(dropped)}/{len(price_ok)} 个日期（{frac:.1%}）："
        f"这些日子的 {list(cov_aligned.columns)} 窗口内有缺口，整天不出特征。"
        f" 根因通常是基准价格里的 NaN 占位行 —— 修数据源，别放松这道门。"
    )
    for a, b, n in spans[:10]:
        level(f"[TFM]   被砍区间 {a.date()} ~ {b.date()}（{n} 天）")
    if len(spans) > 10:
        level(f"[TFM]   …另有 {len(spans) - 10} 段未列出")


# ── 缓存 ──────────────────────────────────────────────────────────────


def _panel_fingerprint(panel: pd.DataFrame | None) -> str:
    """面板内容指纹（含数值，不只是形状）。

    🔴 只编码「列名 + 首末日期 + 行数」是不够的：`build_dataset` 用
    `auto_adjust=True` 下载，一次分红或拆股会**回溯重算整条历史价格**，而日期范围、
    长度、列名逐字不变 —— 旧键照样命中，配上 10 年 TTL 就会永久返回按旧价算出的特征。
    数值进指纹后，这类改写必然换键。
    """
    if panel is None or panel.empty:
        return "none"
    import hashlib

    # ⚠️ 先按**相对精度**量化到 5 位有效数字，再取指纹。理由是实测出来的：
    # yfinance 的 `auto_adjust=True` 每次下载都重算回溯复权价，2026-09-16 实测
    # 相隔 40 分钟的两次下载，31 只里 22 只（全是分红股）各有数百根收盘价发生
    # **相对 ~5e-7** 的变化 —— 纯浮点舍入，不是真实价格改变。
    # 不量化的话指纹每次都变，缓存永远冷；而 5e-5 的容差远大于这个抖动，
    # 又远小于任何真实公司行为（分红复权 ≈0.5~3%、拆股 = 整数倍），
    # 所以拆股/分红这类**该换键**的改写照样会换键。
    arr = np.ascontiguousarray(panel.to_numpy(dtype=np.float64))
    with np.errstate(divide="ignore", invalid="ignore"):
        mag = np.floor(np.log10(np.abs(arr)))
        mag = np.where(np.isfinite(mag), mag, 0.0)
        arr = np.round(arr / 10.0**mag, 4) * 10.0**mag
    h = hashlib.blake2b(arr.tobytes(), digest_size=16)
    h.update(",".join(map(str, panel.columns)).encode())
    h.update(f"{panel.index[0]}|{panel.index[-1]}|{len(panel)}".encode())
    return h.hexdigest()


def _cache_key(close_panel, cov_panel, cfg, start) -> str:
    from data.cache import SQLiteCache

    return SQLiteCache.make_key(
        *cfg.cache_parts(),
        _panel_fingerprint(close_panel),
        _panel_fingerprint(cov_panel),
        str(start),
    )


def _cache_get(cache, close_panel, cov_panel, cfg, start):
    if cache is None:
        return None
    try:
        df = cache.get(_cache_key(close_panel, cov_panel, cfg, start))
    except Exception as exc:  # 缓存问题绝不能挡住计算
        logger.warning(f"[TFM] 读缓存失败，改为重算: {exc}")
        return None
    if df is None or df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index(["date", "ticker"])[FEATURE_COLUMNS]


def _cache_set(cache, close_panel, cov_panel, cfg, start, out) -> None:
    if cache is None or out.empty:
        return
    try:
        # TTL 取极长：缓存键含**面板数值指纹**（`_panel_fingerprint`）+ 全部配置，
        # 任何输入变化（含 auto_adjust 的回溯复权改写）都会换键，不存在"过期"语义。
        # ⚠️ 这个理由只在指纹含数值时成立 —— 若哪天把指纹退回成"形状"，TTL 必须一起改短。
        cache.set(
            _cache_key(close_panel, cov_panel, cfg, start),
            out.reset_index(),
            ttl_hours=24 * 365 * 10,
        )
    except Exception as exc:
        logger.warning(f"[TFM] 写缓存失败（不影响结果）: {exc}")
