import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import instruments as I


class CodeResolutionTests(unittest.TestCase):
    def test_bare_shanghai_bond_code_is_not_misfiled_to_shenzhen(self):
        # 桥接层 code_utils.normalize_stock_code 按「5/6 开头 = SH」判断，
        # 110043 这类沪市转债会被错判成 SZ。
        self.assertEqual(I.normalize_code("110043"), "110043.SH")
        self.assertEqual(I.normalize_code("113050"), "113050.SH")
        self.assertEqual(I.normalize_code("132018"), "132018.SH")
        self.assertEqual(I.normalize_code("123456"), "123456.SZ")

    def test_prefixed_and_suffixed_forms_normalize_the_same(self):
        for raw in ("600000.SH", "SH600000", "600000"):
            self.assertEqual(I.normalize_code(raw), "600000.SH")

    def test_kind_detection_covers_every_board(self):
        cases = {
            "113050.SH": I.KIND_BOND, "110043.SH": I.KIND_BOND,
            "128136.SZ": I.KIND_BOND, "123120.SZ": I.KIND_BOND,
            "600000.SH": I.KIND_STOCK, "000001.SZ": I.KIND_STOCK,
            "688981.SH": I.KIND_STAR, "920819.BJ": I.KIND_BJ,
            "510300.SH": I.KIND_ETF, "159915.SZ": I.KIND_ETF,
        }
        for code, expected in cases.items():
            self.assertEqual(I.instrument_kind(code), expected, code)


class VolumeRoundingTests(unittest.TestCase):
    def test_convertible_bond_ten_lot_survives_rounding(self):
        # 这是换掉 code_utils.min_lot 的根本原因：(10 // 100) * 100 == 0
        self.assertEqual(I.round_volume("113050.SH", 10), 10)
        self.assertEqual(I.round_volume("113050.SH", 15), 10)
        self.assertEqual(I.round_volume("113050.SH", 37), 30)
        self.assertEqual(I.round_volume("113050.SH", 9), 0)

    def test_star_board_increments_by_one_share_above_two_hundred(self):
        self.assertEqual(I.round_volume("688981.SH", 250), 250)
        self.assertEqual(I.round_volume("688981.SH", 200), 200)
        self.assertEqual(I.round_volume("688981.SH", 199), 0)

    def test_ordinary_stock_rounds_down_to_whole_lots(self):
        self.assertEqual(I.round_volume("600000.SH", 250), 200)
        self.assertEqual(I.round_volume("600000.SH", 99), 0)

    def test_sell_all_keeps_odd_lots(self):
        # 零股必须一次性卖出，清仓时不能规整
        self.assertEqual(I.round_volume("600000.SH", 137, sell_all=True), 137)
        self.assertEqual(I.round_volume("113050.SH", 7, sell_all=True), 7)

    def test_non_numeric_volume_is_rejected_not_raised(self):
        self.assertEqual(I.round_volume("600000.SH", None), 0)
        self.assertEqual(I.round_volume("600000.SH", "abc"), 0)
        self.assertEqual(I.round_volume("600000.SH", -100), 0)


class PriceTests(unittest.TestCase):
    def test_bond_and_fund_quote_to_three_decimals(self):
        self.assertEqual(I.price_tick("113050.SH"), 0.001)
        self.assertEqual(I.price_tick("510300.SH"), 0.001)
        self.assertEqual(I.round_price("113050.SH", 123.4567), 123.457)

    def test_stock_quotes_to_two_decimals(self):
        self.assertEqual(I.price_tick("600000.SH"), 0.01)
        self.assertEqual(I.round_price("600000.SH", 10.4567), 10.46)

    def test_invalid_price_returns_zero(self):
        self.assertEqual(I.round_price("600000.SH", None), 0.0)
        self.assertEqual(I.round_price("600000.SH", -1), 0.0)


class T0Tests(unittest.TestCase):
    def test_only_convertible_bonds_are_t0(self):
        self.assertTrue(I.is_t0("113050.SH"))
        self.assertTrue(I.is_t0("128136.SZ"))
        self.assertFalse(I.is_t0("600000.SH"))
        self.assertFalse(I.is_t0("510300.SH"))


if __name__ == "__main__":
    unittest.main()
