"""桥接层对象 → dashboard 落库 dict 的翻译（sync/adapters.py）。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sync import adapters


class TradeToRowTests(unittest.TestCase):
    def _trade(self, **overrides):
        base = {"order_id": 1, "order_sysid": "SYS-1", "order_type": 23,
                "stock_code": "600000.SH", "traded_id": "T-1",
                "traded_price": 8.95, "traded_volume": 300,
                "traded_amount": 2685.0, "traded_time": 1000}
        base.update(overrides)
        return base

    def test_traded_id_passes_through_when_present(self):
        row = adapters.trade_to_row(self._trade(), "acc-1")
        self.assertEqual(row["traded_id"], "T-1")

    def test_falls_back_to_the_trade_id_alias(self):
        trade = self._trade()
        del trade["traded_id"]
        trade["trade_id"] = "ALT-9"
        row = adapters.trade_to_row(trade, "acc-1")
        self.assertEqual(row["traded_id"], "ALT-9")

    def test_missing_traded_id_becomes_none_not_empty_string(self):
        # (account_id, traded_id) 是 trades 表的唯一键（见 app.py save_trades）。
        # "" 会让所有取不到编号的成交互相覆盖；None 在 SQLite 里互不冲突。
        trade = self._trade()
        del trade["traded_id"]
        row = adapters.trade_to_row(trade, "acc-1")
        self.assertIsNone(row["traded_id"])

    def test_direction_and_order_type_both_carry_the_side(self):
        # 面板的统计 SQL 两边都认（direction = 23 OR order_type = 23）
        row = adapters.trade_to_row(self._trade(order_type=24), "acc-1")
        self.assertEqual(row["direction"], 24)
        self.assertEqual(row["order_type"], 24)

    def test_reads_a_dict_or_an_attribute_object_the_same_way(self):
        class Obj:
            pass
        obj = Obj()
        for key, value in self._trade().items():
            setattr(obj, key, value)
        row_from_dict = adapters.trade_to_row(self._trade(), "acc-1")
        row_from_obj = adapters.trade_to_row(obj, "acc-1")
        self.assertEqual(row_from_dict, row_from_obj)


if __name__ == "__main__":
    unittest.main()
