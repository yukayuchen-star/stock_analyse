"""`backtest.timesfm_features` 的测试。

运行：
    .venv/bin/python -m unittest tests.test_timesfm_features -v

用 stdlib unittest 而非 pytest：本项目此前无测试套件，不额外引入依赖。

最重要的两条断言是 `PointInTimeTest`：
  1. 追加未来数据不得改变历史特征值（前视泄漏）
  2. 改变批处理粒度不得改变特征值（数值可复现）
这两条是正交的失败模式，分开断言才能在失败时立刻定位是哪一类。
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from backtest import timesfm_features as tfm
from backtest.timesfm_features import (
    FEATURE_COLUMNS,
    TimesFMFeatureConfig,
    _covariate_ready,
    _features_for_date,
    _group_by_ticker_set,
    _panel_fingerprint,
    _qualified_mask,
    build_close_panel,
    compute_timesfm_features,
)


def _synthetic_panel(n: int = 300, seed: int = 0) -> pd.DataFrame:
    """三支合成价格序列；C 前 150 根缺失，模拟 ARM / SNDK 这类晚上市标的。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-02", periods=n)

    def walk(s0: float) -> np.ndarray:
        return s0 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))

    panel = pd.DataFrame({"A": walk(100), "B": walk(50), "C": walk(200)}, index=idx)
    panel.loc[panel.index[:150], "C"] = np.nan
    return panel


class QualifiedMaskTest(unittest.TestCase):
    """合格掩码决定了什么数据会被喂进模型，是防伪造历史的第一道闸。"""

    def test_leading_nan_ticker_never_qualifies_early(self):
        panel = _synthetic_panel()
        mask = _qualified_mask(panel, context=64)
        first_c = mask["C"].idxmax()
        # C 从第 150 根才有数据，需再攒满 64 根才合格。
        self.assertGreaterEqual(panel.index.get_loc(first_c), 150 + 64 - 1)

    def test_interior_gap_disqualifies(self):
        # cumsum 式的"累计有效根数"会放过窗口内部的缺口，rolling sum 不会。
        idx = pd.bdate_range("2023-01-02", periods=50)
        panel = pd.DataFrame({"A": np.arange(50.0)}, index=idx)
        panel.loc[idx[30], "A"] = np.nan
        mask = _qualified_mask(panel, context=10)
        # 缺口落在窗口内的那些日期必须不合格。
        self.assertFalse(bool(mask["A"].iloc[30]))
        self.assertFalse(bool(mask["A"].iloc[35]))
        # 缺口滑出窗口之后重新合格。
        self.assertTrue(bool(mask["A"].iloc[41]))

    def test_exact_boundary(self):
        idx = pd.bdate_range("2023-01-02", periods=20)
        panel = pd.DataFrame({"A": np.arange(20.0)}, index=idx)
        mask = _qualified_mask(panel, context=10)
        self.assertFalse(bool(mask["A"].iloc[8]))   # 只有 9 根
        self.assertTrue(bool(mask["A"].iloc[9]))    # 恰好 10 根

    def test_group_by_ticker_set_splits_on_change(self):
        panel = _synthetic_panel()
        mask = _qualified_mask(panel, context=64)
        dates = [d for d in panel.index if bool(mask.loc[d].any())]
        groups = list(_group_by_ticker_set(mask, dates))
        # 至少两组：C 合格前 {A,B}，之后 {A,B,C}。
        self.assertGreaterEqual(len(groups), 2)
        self.assertEqual(groups[0][0], ("A", "B"))
        self.assertEqual(groups[-1][0], ("A", "B", "C"))
        # 分组必须无损覆盖全部日期。
        self.assertEqual(sum(len(g[1]) for g in groups), len(dates))


class FeatureDerivationTest(unittest.TestCase):
    """特征公式本身，用手工构造的分位数验证，不需要模型。"""

    def _quantiles(self, q10: float, q50: float, q90: float) -> np.ndarray:
        # (n_ticker=1, horizon=5, n_quantile=9)，只有末端一步被读取。
        q = np.zeros((1, 5, 9), dtype=np.float32)
        q[0, -1, 0] = q10
        q[0, -1, 4] = q50
        q[0, -1, 8] = q90
        return q

    def test_drift_and_spread(self):
        out = _features_for_date(
            date=pd.Timestamp("2024-01-02"),
            tickers=["A"],
            last_close=np.array([100.0]),
            joint_q=self._quantiles(95.0, 102.0, 110.0),
            solo_q=self._quantiles(95.0, 101.0, 110.0),
        )
        row = out.iloc[0]
        self.assertAlmostEqual(row["tfm_drift_5d"], 0.02, places=6)
        self.assertAlmostEqual(row["tfm_qspread_5d"], 0.15, places=6)
        # (110 + 95 - 2*102) / 15 = 1/15
        self.assertAlmostEqual(row["tfm_skew_5d"], 1.0 / 15.0, places=6)
        # |102 - 101| / 100
        self.assertAlmostEqual(row["tfm_joint_gap_5d"], 0.01, places=6)

    def test_symmetric_forecast_has_zero_skew(self):
        out = _features_for_date(
            date=pd.Timestamp("2024-01-02"),
            tickers=["A"],
            last_close=np.array([100.0]),
            joint_q=self._quantiles(90.0, 100.0, 110.0),
            solo_q=None,
        )
        self.assertAlmostEqual(out.iloc[0]["tfm_skew_5d"], 0.0, places=6)
        # 没有独立预测时该列必须是 NaN，而不是 0 —— 0 会被误读成"无横截面效应"。
        self.assertTrue(np.isnan(out.iloc[0]["tfm_joint_gap_5d"]))

    def test_columns_and_order_are_stable(self):
        out = _features_for_date(
            date=pd.Timestamp("2024-01-02"),
            tickers=["A", "B"],
            last_close=np.array([100.0, 50.0]),
            joint_q=np.tile(self._quantiles(95.0, 100.0, 105.0), (2, 1, 1)),
            solo_q=None,
        )
        self.assertEqual(list(out.columns), FEATURE_COLUMNS)
        self.assertEqual(list(out.index.names), ["date", "ticker"])


class BuildPanelTest(unittest.TestCase):
    def test_missing_ticker_is_skipped_not_faked(self):
        idx = pd.bdate_range("2023-01-02", periods=10)
        raw = {
            "A": pd.DataFrame({"Close": np.arange(10.0)}, index=idx),
            "B": pd.DataFrame({"Close": np.arange(10.0) * 2}, index=idx),
        }
        panel = build_close_panel(raw, ["A", "B", "MISSING"])
        self.assertEqual(list(panel.columns), ["A", "B"])

    def test_empty_input(self):
        self.assertTrue(build_close_panel({}, ["A"]).empty)


class PointInTimeTest(unittest.TestCase):
    """需要真实 checkpoint。两条断言是本模块存在的理由。"""

    @classmethod
    def setUpClass(cls):
        cls.panel = _synthetic_panel(n=260, seed=7)
        cls.cfg_serial = TimesFMFeatureConfig(
            horizon=5, context=64, date_batch=1, include_joint_gap=True
        )
        cls.start = cls.panel.index[200]

    def test_future_data_does_not_change_past_features(self):
        """把面板往后延长 30 天，历史特征值必须逐位不变。

        这是特征构造期前视泄漏的直接检验。`run_walk_forward` 的 label
        purge/embargo 保护不了这一层：它净化的是标签跨界，不是特征输入窗口。
        """
        cut = 230
        short = self.panel.iloc[:cut]
        full = self.panel

        feats_short = compute_timesfm_features(
            short, None, self.cfg_serial, start=self.start
        )
        feats_full = compute_timesfm_features(
            full, None, self.cfg_serial, start=self.start
        )

        overlap = feats_short.index.intersection(feats_full.index)
        self.assertGreater(len(overlap), 0, "重叠区为空，测试没有实际检验任何东西")
        pd.testing.assert_frame_equal(
            feats_short.loc[overlap].sort_index(),
            feats_full.loc[overlap].sort_index(),
            check_exact=True,
        )

    def test_date_batching_does_not_change_features(self):
        """批处理只是性能手段，不得改变数值。

        TimesFM 的预测对同批次内的序列长度组成敏感（RoPE 在 QK-norm 之前，破坏了
        相对位置不变性）。本模块的所有上下文等长、无填充，故应当免疫 —— 但这是
        必须验证的性质，不是可以假设的性质。
        """
        serial = compute_timesfm_features(
            self.panel, None, self.cfg_serial, start=self.start
        )
        batched = compute_timesfm_features(
            self.panel,
            None,
            TimesFMFeatureConfig(
                horizon=5, context=64, date_batch=16, include_joint_gap=True
            ),
            start=self.start,
        )
        pd.testing.assert_frame_equal(
            serial.sort_index(), batched.sort_index(), check_exact=True
        )


class DLinearControlTest(unittest.TestCase):
    """DLinear 对照组。它必须自身可信，否则"TimesFM 胜过 DLinear"只是基线坏了。"""

    @classmethod
    def setUpClass(cls):
        from backtest.dlinear_control import DLinearConfig

        rng = np.random.default_rng(11)
        n = 600
        idx = pd.bdate_range("2022-01-03", periods=n)

        def walk(s0: float) -> np.ndarray:
            return s0 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n)))

        cls.panel = pd.DataFrame(
            {t: walk(100 + i * 17) for i, t in enumerate(["A", "B", "C", "D"])},
            index=idx,
        )
        cls.cfg = DLinearConfig(context=64, refit_every=63, epochs=20)
        cls.start = idx[200]

    def test_rewriting_future_does_not_change_past_predictions(self):
        """改写未来数据，历史预测必须不变。

        比"延长面板"更强：它同时覆盖两类泄漏 —— 预测输入窗口越界，以及**训练标签**
        伸进了预测期（DLinear 特有，TimesFM 零样本没有这条风险）。
        """
        from backtest.dlinear_control import compute_dlinear_features

        cut = 450
        base = compute_dlinear_features(self.panel, self.cfg, start=self.start)

        tampered = self.panel.copy()
        tampered.iloc[cut:] = tampered.iloc[cut:] * 3.0
        after = compute_dlinear_features(tampered, self.cfg, start=self.start)

        cutoff = self.panel.index[cut]
        past = base.index.get_level_values("date") < cutoff
        self.assertGreater(int(past.sum()), 0, "重叠区为空，测试没有检验任何东西")
        pd.testing.assert_frame_equal(
            base[past].sort_index(),
            after[after.index.get_level_values("date") < cutoff].sort_index(),
            check_exact=True,
        )

    def test_windows_are_centred_on_zero(self):
        """输入与目标都必须是相对最后收盘价的收益率。

        若中心落在 1.0，线性层需先把偏置学到 1.0 才谈得上拟合信号，实测会产出
        ±22% 的荒唐 5 日漂移。这条断言把那个回归钉死。
        """
        from backtest.dlinear_control import _windows

        x, y, keys = _windows(self.panel, self.cfg, 300, with_label=True)
        self.assertGreater(len(x), 0)
        # 每个窗口的最后一个元素按定义恰好是 0。
        np.testing.assert_allclose(x[:, -1], 0.0, atol=1e-6)
        self.assertLess(abs(float(np.mean(y))), 0.05)
        self.assertTrue(all(t <= 300 for t, _ in keys))

    def test_labelled_windows_never_run_past_data_end(self):
        from backtest.dlinear_control import _windows

        last = len(self.panel) - 1
        _, _, keys = _windows(self.panel, self.cfg, last, with_label=True)
        # 有标签的窗口，其 t+horizon 必须仍在面板内。
        self.assertTrue(all(t + self.cfg.horizon <= last for t, _ in keys))

    def test_does_not_beat_random_walk_on_pure_noise(self):
        """合成数据本就是几何布朗运动，可预测的条件均值为零。

        对照组若在这种数据上"跑赢"随机游走，说明实现有泄漏，而不是发现了信号。
        这是对照组自身的体检。
        """
        import torch

        from backtest.dlinear_control import _fit, _windows

        TRAIN_END = 350
        x, y, _ = _windows(self.panel, self.cfg, TRAIN_END, with_label=True)
        xo, yo, keys_o = _windows(self.panel, self.cfg, 550, with_label=True)

        # ⚠️ `_windows` 一律从 context-1 起枚举，所以 550 的结果**包含了 350 的全部训练窗口**。
        # 不切掉就是样本内评估，而且方向正好最坏：拟合越好 mse_model 越低，
        # 越朝着触发下面 assertGreater 的方向走 —— 于是本测试可能因**过拟合**而失败，
        # 而不是因为它声称要查的前视泄漏。届时最"自然"的修法是放宽那个阈值，
        # 而那个阈值正是本测试的全部意义。（2026-09-14 code-review 查出）
        oos = np.array([t > TRAIN_END for t, _ in keys_o])
        self.assertTrue(
            oos.any(),
            "切出的样本外窗口为空 —— 测试失去意义，应调大 550 或调小 TRAIN_END",
        )
        xo, yo = xo[oos], yo[oos]

        model = _fit(x, y, self.cfg)
        with torch.no_grad():
            pred = model(torch.from_numpy(xo)).numpy()
        mse_model = float(np.mean((pred[:, -1] - yo[:, -1]) ** 2))
        mse_zero = float(np.mean(yo[:, -1] ** 2))
        self.assertGreater(
            mse_model,
            mse_zero * 0.90,
            f"DLinear 在纯噪声上显著跑赢随机游走 (MSE {mse_model:.3e} vs "
            f"{mse_zero:.3e})，几乎必然是前视泄漏而非真实信号",
        )


if __name__ == "__main__":
    unittest.main()


# ── 2026-09-16 补：审查发现的三条，全部做成**不需要 checkpoint** 的测试 ──────
#
# 教训先写在这里：原来的 `test_date_batching_does_not_change_features` 是为
# 「批处理粒度不得改变特征值」写的，但它传 `cov_panel=None`，而当时唯一会破坏
# 该不变量的代码路径**只在有协变量时才走到**。⇒ 一个守卫测试若不覆盖它要守的
# 那条路径，它的绿灯只是在证明别的事。且它需要真 checkpoint，本机根本跑不了
# （`timesfm3` 不在 .venv 里），于是连"绿灯"都没有。
#
# 下面用假预测器把模型换掉，让这些不变量**在没有 checkpoint 的机器上也能被检验**。


def _fake_quantiles(contexts, past_only, horizon):
    """确定性假预测：数值同时依赖上下文与"有没有协变量"。

    后者是关键 —— 若哪天又有人让某些日期悄悄丢掉协变量，产出必然变化，测试才抓得住。
    """
    out = []
    for i, ctx in enumerate(contexts):
        n = ctx.shape[0]
        base = ctx[:, -1].astype(np.float64)
        bump = 0.0 if past_only is None else float(np.nansum(past_only[i][:, -1])) * 1e-9
        q = np.stack(
            [base * (1.0 + 0.001 * (k - 4)) + bump for k in range(9)], axis=-1
        )
        out.append(np.repeat(q[:, None, :], horizon, axis=1))
    return out


class _FakeForecasterPatch:
    """把 `_load_forecaster` / `_predict` 换成不需要 checkpoint 的确定性替身。"""

    def __enter__(self):
        self._lf, self._pr = tfm._load_forecaster, tfm._predict
        tfm._load_forecaster = lambda cfg: object()
        tfm._predict = lambda f, contexts, cfg, past_only, *, univariate: _fake_quantiles(
            contexts, None if univariate else past_only, cfg.horizon
        )
        return self

    def __exit__(self, *exc):
        tfm._load_forecaster, tfm._predict = self._lf, self._pr
        return False


def _cov_panel(index, seed=3, bad_pos=None):
    rng = np.random.default_rng(seed)
    cov = pd.DataFrame(
        {
            "QQQ": 300 * np.exp(np.cumsum(rng.normal(0, 0.01, len(index)))),
            "SPY": 400 * np.exp(np.cumsum(rng.normal(0, 0.01, len(index)))),
        },
        index=index,
    )
    if bad_pos is not None:
        cov.iloc[bad_pos, cov.columns.get_loc("QQQ")] = np.nan  # 只打中一只，同 Yahoo 占位行
    return cov


class CovariateGateTest(unittest.TestCase):
    """协变量门。纯 pandas，无需模型 —— 这正是把它抽成独立函数的理由。"""

    def test_one_bad_covariate_day_disqualifies_exactly_context_days(self):
        idx = pd.bdate_range("2023-01-02", periods=400)
        bad = 200
        ok = _covariate_ready(_cov_panel(idx, bad_pos=bad), context=64)
        self.assertFalse(bool(ok.iloc[bad]))
        self.assertFalse(bool(ok.iloc[bad + 63]), "第 context-1 天仍应被连累")
        self.assertTrue(bool(ok.iloc[bad + 64]), "第 context 天应恢复")
        self.assertTrue(bool(ok.iloc[bad - 1]), "坏日之前不应受影响")
        self.assertEqual(int((~ok.iloc[63:]).sum()), 64)

    def test_missing_column_disqualifies_even_if_other_is_present(self):
        # 协变量是一组一起喂的，缺一列就不是同一个输入 —— all(axis=1) 而非 any。
        idx = pd.bdate_range("2023-01-02", periods=80)
        cov = _cov_panel(idx)
        cov.loc[idx[10], "SPY"] = np.nan
        self.assertFalse(bool(_covariate_ready(cov, context=32).iloc[40]))

    def test_gate_returns_empty_without_ever_loading_the_model(self):
        """全被门掉时必须在加载模型之前就返回 —— 也证明门在批处理之前生效。"""
        idx = pd.bdate_range("2023-01-02", periods=120)
        panel = pd.DataFrame({"A": np.linspace(100, 140, 120)}, index=idx)
        cov = _cov_panel(idx)
        cov.iloc[:, 0] = np.nan  # 协变量全废
        tripped = []
        orig = tfm._load_forecaster
        tfm._load_forecaster = lambda cfg: tripped.append(1)
        try:
            out = compute_timesfm_features(
                panel, cov, TimesFMFeatureConfig(context=32, horizon=5), start=idx[60]
            )
        finally:
            tfm._load_forecaster = orig
        self.assertTrue(out.empty)
        self.assertEqual(tripped, [], "不该为了发现「没有合格日期」而先把模型加载起来")


class BatchingWithCovariatesTest(unittest.TestCase):
    """🔴 审查发现的 HIGH：按 chunk 丢协变量会让特征值依赖 date_batch。

    原测试传 `cov_panel=None`，走不到出问题的分支。这里**带着协变量**测，
    且用假预测器，使它在没有 checkpoint 的机器上也能跑。
    """

    @classmethod
    def setUpClass(cls):
        cls.idx = pd.bdate_range("2023-01-02", periods=200)
        rng = np.random.default_rng(11)
        cls.panel = pd.DataFrame(
            {
                "A": 100 * np.exp(np.cumsum(rng.normal(0, 0.015, 200))),
                "B": 50 * np.exp(np.cumsum(rng.normal(0, 0.015, 200))),
            },
            index=cls.idx,
        )
        cls.start = cls.idx[120]

    def _run(self, cov, date_batch):
        with _FakeForecasterPatch():
            return compute_timesfm_features(
                self.panel,
                cov,
                TimesFMFeatureConfig(context=32, horizon=5, date_batch=date_batch),
                start=self.start,
            )

    def test_batching_invariant_holds_with_clean_covariates(self):
        cov = _cov_panel(self.idx)
        pd.testing.assert_frame_equal(
            self._run(cov, 1).sort_index(), self._run(cov, 16).sort_index(),
            check_exact=True,
        )

    def test_batching_invariant_holds_when_a_covariate_day_is_bad(self):
        """回归测试：这一条在按 chunk 丢协变量的旧实现下必然失败。

        坏日落在 [start, end] 中间，`date_batch=1` 时只毒害它自己那一批、
        `=16` 时会毒害整块 —— 旧实现下两者产出不同。
        """
        cov = _cov_panel(self.idx, bad_pos=150)
        a, b = self._run(cov, 1).sort_index(), self._run(cov, 16).sort_index()
        pd.testing.assert_frame_equal(a, b, check_exact=True)
        gated = set(self.idx[self.idx >= self.start]) - set(
            a.index.get_level_values("date")
        )
        self.assertTrue(gated, "坏协变量日必须真的门掉了一些日期，否则本测试是空转")

    def test_covariates_actually_reach_the_model(self):
        """若协变量根本没传进去，上面两条不变量测试就是在证明"两次都没用协变量"。"""
        cov = _cov_panel(self.idx)
        with_cov = self._run(cov, 8)
        without = self._run(None, 8)
        common = with_cov.index.intersection(without.index)
        self.assertGreater(len(common), 0)
        self.assertFalse(
            np.allclose(
                with_cov.loc[common, "tfm_drift_5d"].to_numpy(),
                without.loc[common, "tfm_drift_5d"].to_numpy(),
            ),
            "带/不带协变量产出相同 —— 协变量没有真的进入前向",
        )


class CacheFingerprintTest(unittest.TestCase):
    """缓存键必须编码面板**数值**，否则回溯复权会命中旧特征（10 年 TTL 下是永久的）。"""

    def test_retroactive_reprice_changes_the_key(self):
        idx = pd.bdate_range("2023-01-02", periods=50)
        p1 = pd.DataFrame({"A": np.linspace(100, 150, 50)}, index=idx)
        p2 = p1 * 0.5  # 2:1 拆股：列名/日期范围/行数逐字不变
        self.assertNotEqual(_panel_fingerprint(p1), _panel_fingerprint(p2))

    def test_identical_panels_share_a_key(self):
        idx = pd.bdate_range("2023-01-02", periods=50)
        p1 = pd.DataFrame({"A": np.linspace(100, 150, 50)}, index=idx)
        self.assertEqual(_panel_fingerprint(p1), _panel_fingerprint(p1.copy()))

    def test_yfinance_readjustment_jitter_does_not_change_the_key(self):
        """指纹必须容忍 auto_adjust 的浮点抖动，否则缓存永远冷。

        2026-09-16 实测：相隔 40 分钟的两次 yfinance 下载，31 只里 22 只（分红股）
        各有数百根收盘价发生相对 ~5e-7 的变化。那不是价格改变，是复权重算的舍入。
        """
        idx = pd.bdate_range("2023-01-02", periods=100)
        base = pd.DataFrame({"A": np.linspace(100, 500, 100)}, index=idx)
        rng = np.random.default_rng(0)
        jittered = base * (1 + rng.normal(0, 5e-7, (100, 1)))
        self.assertEqual(_panel_fingerprint(base), _panel_fingerprint(jittered))

    def test_real_corporate_action_still_changes_the_key(self):
        """容差不得大到吞掉真实公司行为。分红复权 ≈0.5~3%，远在 5e-5 容差之上。"""
        idx = pd.bdate_range("2023-01-02", periods=100)
        base = pd.DataFrame({"A": np.linspace(100, 500, 100)}, index=idx)
        for factor, what in ((0.995, "分红复权 0.5%"), (0.5, "2:1 拆股")):
            with self.subTest(what=what):
                self.assertNotEqual(
                    _panel_fingerprint(base), _panel_fingerprint(base * factor)
                )

    def test_nan_pattern_is_part_of_the_fingerprint(self):
        idx = pd.bdate_range("2023-01-02", periods=50)
        p1 = pd.DataFrame({"A": np.linspace(100, 150, 50)}, index=idx)
        p2 = p1.copy()
        p2.iloc[10, 0] = np.nan
        self.assertNotEqual(_panel_fingerprint(p1), _panel_fingerprint(p2))


class WindowStartPosTest(unittest.TestCase):
    """DLinear 的 start_pos 是性能修复，**必须逐位不改变结果**。"""

    def test_start_pos_matches_filtering_the_full_enumeration(self):
        from backtest.dlinear_control import DLinearConfig, _windows

        rng = np.random.default_rng(5)
        idx = pd.bdate_range("2022-01-03", periods=300)
        panel = pd.DataFrame(
            {"A": 100 * np.exp(np.cumsum(rng.normal(0, 0.015, 300))),
             "B": 80 * np.exp(np.cumsum(rng.normal(0, 0.015, 300)))},
            index=idx,
        )
        panel.iloc[:120, 1] = np.nan
        cfg = DLinearConfig(context=64, horizon=5)
        full_x, _, full_k = _windows(panel, cfg, 250, with_label=False)
        want = [i for i, (t, _) in enumerate(full_k) if 200 <= t <= 250]
        sub_x, _, sub_k = _windows(panel, cfg, 250, with_label=False, start_pos=200)
        self.assertEqual([full_k[i] for i in want], sub_k)
        np.testing.assert_array_equal(full_x[want], sub_x)
        self.assertLess(len(sub_k), len(full_k), "本测试须真的缩小了枚举范围")
