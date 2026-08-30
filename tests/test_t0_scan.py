import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module


class T0ScanCalculationTests(unittest.TestCase):
    def test_build_t0_scan_rows_calculates_buy_and_sell_actions(self):
        trades = [
            {
                "account_id": "A1",
                "stock_code": "000001.SZ",
                "instrument_name": "平安银行",
                "direction": 23,
                "traded_volume": 600,
                "traded_price": 10.0,
                "traded_time": 100,
            },
            {
                "account_id": "A2",
                "stock_code": "000002.SZ",
                "instrument_name": "万科A",
                "direction": 24,
                "traded_volume": 300,
                "traded_price": 12.0,
                "traded_time": 200,
            },
            {
                "account_id": "A3",
                "stock_code": "000003.SZ",
                "instrument_name": "已闭合",
                "order_type": 23,
                "traded_volume": 200,
                "traded_price": 8.0,
                "traded_time": 300,
            },
            {
                "account_id": "A3",
                "stock_code": "000003.SZ",
                "instrument_name": "已闭合",
                "order_type": 24,
                "traded_volume": 200,
                "traded_price": 8.5,
                "traded_time": 400,
            },
        ]
        positions = [
            {
                "account_id": "A1",
                "stock_code": "000001.SZ",
                "instrument_name": "平安银行",
                "volume": 1600,
                "can_use_volume": 500,
                "last_price": 10.5,
            },
            {
                "account_id": "A2",
                "stock_code": "000002.SZ",
                "instrument_name": "万科A",
                "volume": 700,
                "can_use_volume": 700,
                "last_price": 11.5,
            },
            {
                "account_id": "A3",
                "stock_code": "000003.SZ",
                "instrument_name": "已闭合",
                "volume": 1000,
                "can_use_volume": 1000,
                "last_price": 8.4,
            },
        ]

        rows = app_module.build_t0_scan_rows(
            trades,
            positions,
            {"A1": "账户1", "A2": "账户2", "A3": "账户3"},
        )
        by_key = {(row["account_id"], row["stock_code"]): row for row in rows}

        sell_row = by_key[("A1", "000001.SZ")]
        self.assertEqual("卖出", sell_row["action"])
        self.assertEqual("可卖T", sell_row["status"])
        self.assertEqual(500, sell_row["suggested_volume"])
        self.assertEqual(250.0, sell_row["expected_t_profit"])
        self.assertTrue(sell_row["is_candidate"])

        buy_row = by_key[("A2", "000002.SZ")]
        self.assertEqual("买入", buy_row["action"])
        self.assertEqual("可买T", buy_row["status"])
        self.assertEqual(300, buy_row["suggested_volume"])
        self.assertEqual(150.0, buy_row["expected_t_profit"])
        self.assertTrue(buy_row["is_candidate"])

        closed_row = by_key[("A3", "000003.SZ")]
        self.assertEqual("已闭合", closed_row["action"])
        self.assertEqual("已完成T", closed_row["status"])
        self.assertEqual(0, closed_row["suggested_volume"])
        self.assertEqual(100.0, closed_row["realized_t_profit"])


if __name__ == "__main__":
    unittest.main()
