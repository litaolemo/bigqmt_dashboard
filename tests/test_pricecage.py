"""价格笼子：限价申报的有效价格范围。

交易所对限价单有「有效竞价范围」，超出直接废单。两个时段口径不同：
连续竞价 ±2%（基准取最新成交价），集合竞价 ±10%（基准取昨收，开盘前没有成交价）。
再往外还有涨跌停这层硬边界，两者取交集。

纯计算，不联网。
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import pricecage as pc


def _at(hour, minute):
    return datetime(2026, 9, 1, hour, minute)


class SessionTests(unittest.TestCase):
    def test_open_auction_window(self):
        self.assertEqual(pc.session_of(_at(9, 15)), pc.SESSION_AUCTION)
        self.assertEqual(pc.session_of(_at(9, 24)), pc.SESSION_AUCTION)

    def test_close_auction_window(self):
        self.assertEqual(pc.session_of(_at(14, 57)), pc.SESSION_AUCTION)
        self.assertEqual(pc.session_of(_at(14, 59)), pc.SESSION_AUCTION)

    def test_continuous_windows(self):
        for hm in ((9, 30), (10, 0), (11, 29), (13, 0), (14, 56)):
            self.assertEqual(pc.session_of(_at(*hm)), pc.SESSION_CONTINUOUS, hm)

    def test_gaps_are_closed(self):
        # 09:25-09:30 撮合与休整、午休、收盘后都不能报单
        for hm in ((9, 10), (9, 25), (9, 29), (11, 30), (12, 0), (15, 0), (15, 30)):
            self.assertEqual(pc.session_of(_at(*hm)), pc.SESSION_CLOSED, hm)

    def test_band_follows_the_session(self):
        self.assertAlmostEqual(pc.band_of(pc.SESSION_AUCTION), 0.10)
        self.assertAlmostEqual(pc.band_of(pc.SESSION_CONTINUOUS), 0.02)
        # 非交易时段按连续竞价口径给，便于盘前预估
        self.assertAlmostEqual(pc.band_of(pc.SESSION_CLOSED), 0.02)


class BasePriceTests(unittest.TestCase):
    def test_continuous_prefers_last_traded_price(self):
        self.assertEqual(
            pc.base_price_of(pc.SESSION_CONTINUOUS, last_price=9.2, last_close=9.0), 9.2)

    def test_auction_prefers_previous_close(self):
        # 集合竞价开盘前还没有成交价，基准必须是昨收
        self.assertEqual(
            pc.base_price_of(pc.SESSION_AUCTION, last_price=9.2, last_close=9.0), 9.0)

    def test_each_falls_back_to_the_other(self):
        self.assertEqual(pc.base_price_of(pc.SESSION_AUCTION, 9.2, None), 9.2)
        self.assertEqual(pc.base_price_of(pc.SESSION_CONTINUOUS, None, 9.0), 9.0)

    def test_no_usable_base_returns_none(self):
        self.assertIsNone(pc.base_price_of(pc.SESSION_CONTINUOUS, None, None))
        self.assertIsNone(pc.base_price_of(pc.SESSION_CONTINUOUS, 0, -1))


class BandComputationTests(unittest.TestCase):
    def test_continuous_band_is_two_percent(self):
        cage = pc.compute("600000.SH", "buy", 9.00, pc.SESSION_CONTINUOUS)
        self.assertAlmostEqual(cage["low"], 8.82, places=2)
        self.assertAlmostEqual(cage["high"], 9.18, places=2)

    def test_auction_band_is_ten_percent(self):
        cage = pc.compute("600000.SH", "buy", 10.00, pc.SESSION_AUCTION)
        self.assertAlmostEqual(cage["low"], 9.00, places=2)
        self.assertAlmostEqual(cage["high"], 11.00, places=2)

    def test_limits_are_intersected_with_the_daily_stops(self):
        # 集合竞价 ±10% 会越过涨跌停，必须被夹回来
        cage = pc.compute("600000.SH", "buy", 10.00, pc.SESSION_AUCTION,
                          up_stop=10.50, down_stop=9.50)
        self.assertAlmostEqual(cage["high"], 10.50, places=2)
        self.assertAlmostEqual(cage["low"], 9.50, places=2)

    def test_stops_wider_than_the_cage_do_not_widen_it(self):
        cage = pc.compute("600000.SH", "buy", 9.00, pc.SESSION_CONTINUOUS,
                          up_stop=99.0, down_stop=0.01)
        self.assertAlmostEqual(cage["high"], 9.18, places=2)
        self.assertAlmostEqual(cage["low"], 8.82, places=2)

    def test_bond_limits_use_three_decimals(self):
        cage = pc.compute("113050.SH", "buy", 163.19, pc.SESSION_CONTINUOUS)
        self.assertEqual(cage["low"], 159.926)
        self.assertEqual(cage["high"], 166.454)

    def test_missing_base_price_yields_no_limits(self):
        cage = pc.compute("600000.SH", "buy", None, pc.SESSION_CONTINUOUS)
        self.assertIsNone(cage["low"])
        self.assertIsNone(cage["high"])
        self.assertIn("基准价", cage["reason"])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.cage = pc.compute("600000.SH", "buy", 9.00, pc.SESSION_CONTINUOUS)

    def test_price_inside_the_cage_passes(self):
        for price in (8.82, 9.00, 9.18):
            ok, message = pc.check("600000.SH", "buy", price, self.cage)
            self.assertTrue(ok, "%s -> %s" % (price, message))

    def test_price_above_the_cage_is_rejected_with_the_limit(self):
        ok, message = pc.check("600000.SH", "buy", 9.30, self.cage)
        self.assertFalse(ok)
        self.assertIn("上限", message)
        self.assertIn("9.180", message)
        self.assertIn("连续竞价", message)

    def test_price_below_the_cage_is_rejected(self):
        ok, message = pc.check("600000.SH", "buy", 8.00, self.cage)
        self.assertFalse(ok)
        self.assertIn("下限", message)

    def test_missing_price_is_rejected(self):
        ok, message = pc.check("600000.SH", "buy", None, self.cage)
        self.assertFalse(ok)
        self.assertIn("有效价格", message)

    def test_unknown_cage_lets_the_order_through(self):
        # 算不出范围时放行 —— 宁可让交易所去拒，也不能凭猜的基准价拦下用户的单
        blind = pc.compute("600000.SH", "buy", None, pc.SESSION_CONTINUOUS)
        ok, _ = pc.check("600000.SH", "buy", 999.0, blind)
        self.assertTrue(ok)

    def test_auction_message_names_the_session(self):
        cage = pc.compute("600000.SH", "buy", 10.00, pc.SESSION_AUCTION)
        ok, message = pc.check("600000.SH", "buy", 12.0, cage)
        self.assertFalse(ok)
        self.assertIn("集合竞价", message)
        self.assertIn("10%", message)


class ClampTests(unittest.TestCase):
    def setUp(self):
        self.cage = pc.compute("600000.SH", "buy", 9.00, pc.SESSION_CONTINUOUS)

    def test_clamps_to_the_nearest_limit(self):
        self.assertEqual(pc.clamp("600000.SH", 9.30, self.cage), (9.18, True))
        self.assertEqual(pc.clamp("600000.SH", 8.00, self.cage), (8.82, True))

    def test_leaves_a_valid_price_alone(self):
        self.assertEqual(pc.clamp("600000.SH", 9.05, self.cage), (9.05, False))

    def test_unknown_cage_does_not_touch_the_price(self):
        blind = pc.compute("600000.SH", "buy", None, pc.SESSION_CONTINUOUS)
        self.assertEqual(pc.clamp("600000.SH", 9.05, blind), (9.05, False))


if __name__ == "__main__":
    unittest.main()
