"""可转债指标。纯计算，可以对着真实数据点核。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cb import metrics


class ConversionValueTests(unittest.TestCase):
    def test_matches_a_real_market_data_point(self):
        # 强达转债 2026-08-19：正股 90.85，转股价 84.04，东财给的转股价值 108.1033
        self.assertAlmostEqual(
            metrics.conversion_value(90.85, 84.04), 108.1033, places=3)

    def test_zero_or_missing_conversion_price_yields_none(self):
        # 转股价拿不到时必须是 None，不能悄悄当成 0 或 100 算出个假数
        self.assertIsNone(metrics.conversion_value(90.85, 0))
        self.assertIsNone(metrics.conversion_value(90.85, None))
        self.assertIsNone(metrics.conversion_value(None, 84.04))


class PremiumTests(unittest.TestCase):
    def test_premium_is_positive_when_bond_trades_above_conversion_value(self):
        self.assertAlmostEqual(metrics.premium_rate(120.0, 100.0), 20.0, places=4)

    def test_premium_is_negative_when_bond_trades_below_conversion_value(self):
        self.assertAlmostEqual(metrics.premium_rate(90.0, 100.0), -10.0, places=4)

    def test_premium_needs_a_conversion_value(self):
        self.assertIsNone(metrics.premium_rate(120.0, None))
        self.assertIsNone(metrics.premium_rate(120.0, 0))

    def test_double_low_adds_price_and_premium(self):
        self.assertAlmostEqual(metrics.double_low(120.0, 20.0), 140.0, places=4)


class RedeemProgressTests(unittest.TestCase):
    def test_trigger_price_is_130_percent_of_conversion_price(self):
        self.assertAlmostEqual(metrics.redeem_trigger_price(10.0), 13.0, places=4)

    def test_fifteen_hits_in_thirty_days_triggers(self):
        closes = [12.0] * 15 + [14.0] * 15      # 后 15 天 >= 13
        status = metrics.redeem_progress(closes, 10.0)
        self.assertEqual(status["hits"], 15)
        self.assertTrue(status["triggered"])

    def test_fourteen_hits_does_not_trigger(self):
        closes = [12.0] * 16 + [14.0] * 14
        status = metrics.redeem_progress(closes, 10.0)
        self.assertEqual(status["hits"], 14)
        self.assertFalse(status["triggered"])

    def test_hits_are_counted_within_the_window_not_consecutively(self):
        # 条款是「30 个交易日中至少 15 日」，不要求连续
        closes = [14.0, 12.0] * 15              # 隔天满足，共 15 天
        status = metrics.redeem_progress(closes, 10.0)
        self.assertEqual(status["hits"], 15)
        self.assertTrue(status["triggered"])

    def test_only_the_most_recent_window_counts(self):
        # 更早的 30 天全部满足，但最近 30 天一天都不满足 -> 不触发
        closes = [20.0] * 30 + [11.0] * 30
        status = metrics.redeem_progress(closes, 10.0)
        self.assertEqual(status["hits"], 0)
        self.assertFalse(status["triggered"])

    def test_insufficient_history_never_triggers(self):
        # 只有 3 天数据且都满足 —— 不能据此判定强赎
        status = metrics.redeem_progress([20.0, 20.0, 20.0], 10.0)
        self.assertEqual(status["days_counted"], 3)
        self.assertFalse(status["triggered"])

    def test_no_conversion_price_never_triggers(self):
        status = metrics.redeem_progress([20.0] * 30, None)
        self.assertIsNone(status["trigger_price"])
        self.assertFalse(status["triggered"])

    def test_garbage_prices_are_skipped_not_crashed(self):
        status = metrics.redeem_progress([None, "abc", -1, 20.0], 10.0)
        self.assertEqual(status["days_counted"], 1)
        self.assertEqual(status["hits"], 1)


class EvaluateTests(unittest.TestCase):
    def test_evaluate_returns_every_field_even_when_inputs_are_missing(self):
        result = metrics.evaluate(bond_price=None, stock_price=None,
                                  conversion_price=None)
        for key in ("conversion_value", "premium_rate", "double_low"):
            self.assertIsNone(result[key], key)
        self.assertIn("redeem", result)

    def test_evaluate_full_path(self):
        result = metrics.evaluate(bond_price=118.5, stock_price=90.85,
                                  conversion_price=84.04,
                                  stock_closes=[120.0] * 30)
        self.assertAlmostEqual(result["conversion_value"], 108.1033, places=3)
        self.assertIsNotNone(result["premium_rate"])
        self.assertIsNotNone(result["double_low"])
        self.assertTrue(result["redeem"]["triggered"])


if __name__ == "__main__":
    unittest.main()
