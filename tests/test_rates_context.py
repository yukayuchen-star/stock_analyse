"""`signals.macro.rates_context` 的测试（只读利率上下文）。

运行：
    .venv/bin/python -m unittest tests.test_rates_context -v
"""
from __future__ import annotations

import unittest

import pandas as pd

from signals.macro import rates_context as rc


def _s(values, start="2026-01-01"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


class RatesContextTest(unittest.TestCase):
    def test_real_proxy_uses_same_observation_date(self):
        d = _s([4.0] * 30 + [5.0])          # 最后一天 BE 还没出
        b = _s([2.0] * 30)
        r = rc.compute_rates_context(d, b)
        self.assertEqual(r["dgs10"], 5.0)
        self.assertEqual(r["real_proxy"], 2.0)                       # 4.0 − 2.0，不是 5.0 − 2.0
        self.assertEqual(r["real_proxy_asof"], b.index[-1].strftime("%Y-%m-%d"))
        self.assertNotEqual(r["real_proxy_asof"], r["dgs10_asof"])

    def test_changes_in_bp(self):
        d = _s([4.0] * 10 + [4.0 + 0.025 * i for i in range(21)])     # 20 个观测日 +50bp
        b = _s([2.3] * 31)
        r = rc.compute_rates_context(d, b)
        self.assertEqual((r["dgs10_chg_bp"], r["t10yie_chg_bp"], r["real_proxy_chg_bp"]), (50, 0, 50))

    def test_level_last_seen_excludes_recent_window(self):
        vals = [5.3] + [3.0] * 200 + [4.0] * (rc.RECENT_EXCLUDE_OBS - 1) + [5.24]
        d = _s(vals, start="2007-01-01")
        r = rc.compute_rates_context(d, _s([2.3] * len(vals), start="2007-01-01"))
        self.assertEqual(r["dgs10_level_last_seen"], d.index[0].strftime("%Y-%m-%d"))
        top = d.copy(); top.iloc[0] = 1.0
        self.assertIsNone(rc.compute_rates_context(top, _s([2.3] * len(vals), start="2007-01-01"))["dgs10_level_last_seen"])

    def test_missing_inputs_return_none(self):
        self.assertIsNone(rc.compute_rates_context(pd.Series(dtype=float), _s([2.0])))
        self.assertIsNone(rc.compute_rates_context(_s([4.0]), _s([2.0], start="2030-01-01")))   # 无共同日
        self.assertIn("不可得", rc.rates_line(None))

    def test_not_in_macro_score_path(self):
        # 只读承诺：compute_macro_signal 的源码里不得引用 rates_context
        import inspect
        from signals.macro import macro_signal
        self.assertNotIn("rates_context", inspect.getsource(macro_signal.compute_macro_signal))


if __name__ == "__main__":
    unittest.main()
