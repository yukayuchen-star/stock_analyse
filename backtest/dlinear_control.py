"""DLinear 对照组（R10 §2.2）。

**这不是候选因子，是对照。** 它只回答一个问题：TimesFM 的预训练在漂移通道上有没有
带来增量？若 `dlin_drift_5d` 与 `tfm_drift_5d` 表现相当，则那部分能力用一个线性层
就能复现，预训练无贡献。

依据 Zeng et al., "Are Transformers Effective for Time Series Forecasting?"（AAAI'23）：
单层线性（序列分解 + 两个线性层）在全部标准 LTSF 基准上胜过 Informer / Autoformer /
FEDformer。这是本项目否决 Informer 的核心依据之一，因此把它作为基线跑一遍是必要的
自我检验，而不是走过场。

────────────────────────────────────────────────────────────────────────
点位时序正确性（比 TimesFM 侧更严格）
────────────────────────────────────────────────────────────────────────
TimesFM 是零样本的，只需保证**输入窗口** ≤ t。DLinear 需要训练，于是多出一条约束：
训练样本的**标签窗口**也必须 ≤ 重训日。一个在 r 日重训的模型，只能用结束于
`r - horizon` 及更早的窗口——否则它的训练标签落在了预测期之内，是教科书式的前视。

实现为扩展窗口 + 定期重训：每 `refit_every` 个交易日用截至该日的全部合格窗口重训，
再用它预测其后 `refit_every` 天。通道独立（所有标的池化进同一个模型），这是
DLinear / PatchTST 的标准做法，也最大化样本量。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import torch
from loguru import logger
from torch import nn

FEATURE_COLUMNS = ["dlin_drift_5d"]


@dataclass(frozen=True)
class DLinearConfig:
    horizon: int = 5
    context: int = 256      # 与 TimesFMFeatureConfig.context 对齐，保证可比
    kernel: int = 25        # 移动平均分解窗口（DLinear 原文默认）
    refit_every: int = 63   # 约一个季度重训一次
    epochs: int = 80
    lr: float = 0.01
    batch_size: int = 512
    seed: int = 0


class DLinear(nn.Module):
    """序列分解 + 两个线性层。参数量 = 2 × context × horizon。"""

    def __init__(self, context: int, horizon: int, kernel: int):
        super().__init__()
        self.kernel = kernel
        self.trend_linear = nn.Linear(context, horizon)
        self.seasonal_linear = nn.Linear(context, horizon)

    def _moving_avg(self, x: torch.Tensor) -> torch.Tensor:
        # 两端各复制端点填充，保持长度不变（DLinear 原实现的做法）。
        pad_left = x[:, :1].repeat(1, (self.kernel - 1) // 2)
        pad_right = x[:, -1:].repeat(1, self.kernel // 2)
        padded = torch.cat([pad_left, x, pad_right], dim=1)
        return torch.nn.functional.avg_pool1d(
            padded.unsqueeze(1), kernel_size=self.kernel, stride=1
        ).squeeze(1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        trend = self._moving_avg(x)
        seasonal = x - trend
        return self.trend_linear(trend) + self.seasonal_linear(seasonal)


# ── 样本构造 ──────────────────────────────────────────────────────────


def _windows(
    panel: pd.DataFrame,
    cfg: DLinearConfig,
    end_pos: int,
    with_label: bool,
    start_pos: int | None = None,
) -> tuple[np.ndarray, np.ndarray, list[tuple[int, str]]]:
    """抽取结束位置落在 [start_pos, end_pos] 内的所有合格窗口。

    Args:
      end_pos: 允许的最后一个窗口结束位置（含）。调用方负责把标签期也算进去。
      with_label: True 时同时返回 t+horizon 的归一化目标。
      start_pos: 第一个窗口结束位置（含），默认 `context-1`（即面板最早可成窗处）。
        预测阶段只需要本块内的日期，传 `block_start` 可把工作量从
        O(块数 × 全部窗口) 降到 O(refit_every × 标的数)；训练阶段留空（要全部历史）。
        ⚠️ 只影响**枚举范围**，不影响任何一个窗口的内容 —— 窗口 t 恒取
        `col[t-context+1 : t+1]`，与 start_pos 无关。

    归一化：`win/last - 1`、`fut/last - 1`，即相对最后收盘价的**收益率**，输入与
    目标都中心化在 0。若改用 `win/last`（中心在 1.0），线性层的初始输出在 0 附近，
    需要先把偏置学到 1.0 才谈得上拟合信号 —— 实测会得到 ±22% 的荒唐 5 日漂移预测。
    模型因此直接输出 5 日收益率，与 `tfm_drift_5d` 的 `q50/close_t - 1` 同尺度。
    """
    values = panel.to_numpy(dtype=np.float64)
    tickers = list(panel.columns)
    xs, ys, keys = [], [], []

    for j, ticker in enumerate(tickers):
        col = values[:, j]
        lo = cfg.context - 1 if start_pos is None else max(cfg.context - 1, start_pos)
        for t in range(lo, end_pos + 1):
            win = col[t - cfg.context + 1 : t + 1]
            if not np.isfinite(win).all():
                continue
            last = win[-1]
            if not np.isfinite(last) or abs(last) < 1e-9:
                continue
            if with_label:
                fut_end = t + cfg.horizon
                if fut_end >= len(col):
                    continue
                fut = col[t + 1 : fut_end + 1]
                if not np.isfinite(fut).all():
                    continue
                ys.append(fut / last - 1.0)
            xs.append(win / last - 1.0)
            keys.append((t, ticker))

    x = np.asarray(xs, dtype=np.float32) if xs else np.zeros((0, cfg.context), np.float32)
    y = np.asarray(ys, dtype=np.float32) if ys else np.zeros((0, cfg.horizon), np.float32)
    return x, y, keys


def _fit(x: np.ndarray, y: np.ndarray, cfg: DLinearConfig) -> DLinear:
    torch.manual_seed(cfg.seed)
    model = DLinear(cfg.context, cfg.horizon, cfg.kernel)
    opt = torch.optim.Adam(model.parameters(), lr=cfg.lr)
    loss_fn = nn.MSELoss()
    xt, yt = torch.from_numpy(x), torch.from_numpy(y)
    n = len(xt)

    model.train()
    for _ in range(cfg.epochs):
        perm = torch.randperm(n)
        for i in range(0, n, cfg.batch_size):
            idx = perm[i : i + cfg.batch_size]
            opt.zero_grad()
            loss = loss_fn(model(xt[idx]), yt[idx])
            loss.backward()
            opt.step()
    model.eval()
    return model


# ── 主入口 ────────────────────────────────────────────────────────────


def compute_dlinear_features(
    close_panel: pd.DataFrame,
    config: DLinearConfig | None = None,
    start: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """扩展窗口 + 定期重训，产出 `dlin_drift_5d`。

    Returns:
      MultiIndex (date, ticker) 的 DataFrame，列为 `FEATURE_COLUMNS`。
    """
    cfg = config or DLinearConfig()
    if close_panel is None or close_panel.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS)

    index = close_panel.index
    first_pos = cfg.context - 1
    if start is not None:
        start_ts = pd.Timestamp(start)
        first_pos = max(first_pos, int(index.searchsorted(start_ts, side="left")))
    if first_pos >= len(index):
        logger.warning("[DLinear] 历史不足以产生任何预测窗口")
        return pd.DataFrame(columns=FEATURE_COLUMNS)

    rows: list[pd.DataFrame] = []
    n_refits = 0
    covered: set = set()
    skipped: list[tuple] = []   # (块起, 块止, 原因) —— 见函数末尾的缺口报告

    for block_start in range(first_pos, len(index), cfg.refit_every):
        block_end = min(block_start + cfg.refit_every - 1, len(index) - 1)

        # 训练集只能用标签期也已落定的窗口：结束于 block_start-1-horizon 及更早。
        train_end = block_start - 1 - cfg.horizon
        if train_end < cfg.context - 1:
            # ⚠️ 这里原本是无声 `continue`（隔壁 len(x)<100 那条反而有日志）。
            # 无声跳过在 context 偏大时会砍掉回测前若干块，而 TimesFM 侧是零样本、
            # 不需要训练期，那几天照样有特征 —— 于是"TimesFM 打赢 DLinear"是在
            # **不等样本**上比出来的，而这恰恰是对照组存在的唯一理由。
            skipped.append((block_start, block_end, "训练样本期未到"))
            continue
        x, y, _ = _windows(close_panel, cfg, train_end, with_label=True)
        if len(x) < 100:
            logger.info(f"[DLinear] {index[block_start].date()} 前样本仅 {len(x)} 条，跳过该块")
            skipped.append((block_start, block_end, f"训练样本仅 {len(x)} 条"))
            continue

        model = _fit(x, y, cfg)
        n_refits += 1

        # 预测块内每一天。窗口本身只用 ≤ 该日的数据。
        px, _, keys = _windows(
            close_panel, cfg, block_end, with_label=False, start_pos=block_start
        )
        if not keys:
            skipped.append((block_start, block_end, "块内无合格预测窗口"))
            continue
        with torch.no_grad():
            pred = model(torch.from_numpy(px)).numpy()

        covered.update(index[t] for t, _ in keys)
        rows.append(
            pd.DataFrame(
                {"dlin_drift_5d": pred[:, -1].astype(np.float64)},
                index=pd.MultiIndex.from_tuples(
                    [(index[t], tk) for t, tk in keys], names=["date", "ticker"]
                ),
            )
        )

    _log_coverage_gap(index, first_pos, covered, skipped)

    if not rows:
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    out = pd.concat(rows).sort_index()
    logger.info(f"[DLinear] 生成 {len(out)} 行（{n_refits} 次重训）")
    return out


def _log_coverage_gap(index, first_pos: int, covered: set, skipped: list) -> None:
    """报告对照组盖不到的日期区间。

    **这是对照组特有的义务**：TimesFM 零样本、从 `context` 攒够那天起就有特征；
    DLinear 还要再等一个训练期（`context-1+horizon+1` 根）才出第一块。两者的
    可用日期天然不等长，若不报出来，后续"谁更强"的比较就是在两个不同样本上做的。

    正确的用法是**在比较时取两侧日期的交集**，不是让这里放松约束。
    """
    want = set(index[first_pos:])
    miss = sorted(want - covered)
    if not miss:
        logger.info(f"[DLinear] 覆盖 {len(covered)} 个日期，与请求区间一致")
        return
    logger.warning(
        f"[DLinear] 对照组覆盖 {len(covered)}/{len(want)} 个日期，"
        f"缺 {len(miss)} 天（{miss[0].date()} ~ {miss[-1].date()}）。"
        f" TimesFM 侧零样本、这些日子可能是有特征的 —— "
        f"**比较两者时务必先取日期交集**，否则是在不等样本上比。"
    )
    for a, b, why in skipped[:8]:
        logger.warning(f"[DLinear]   跳过块 {index[a].date()}~{index[b].date()}：{why}")
    if len(skipped) > 8:
        logger.warning(f"[DLinear]   …另有 {len(skipped) - 8} 块未列出")
