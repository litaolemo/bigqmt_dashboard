import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app as app_module


def admin_user():
    return {"username": "admin", "role": "admin", "account_id": "admin"}


class DataEndpointPerformanceTests(unittest.TestCase):
    def setUp(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.app.dependency_overrides.clear()

    def test_get_data_reuses_today_profit_info_for_summary_calculations(self):
        today_info = {"today_profit": 12.5, "today_profit_rate": 0.34}
        calls = []

        def fake_today(account_id):
            calls.append(account_id)
            return today_info

        def fake_total(account_id, precomputed_today_info=None):
            self.assertEqual("all", account_id)
            self.assertIs(precomputed_today_info, today_info)
            return {"total_profit": 100.0, "total_profit_rate": 1.2}

        def fake_month(account_id, precomputed_today_info=None):
            self.assertEqual("all", account_id)
            self.assertIs(precomputed_today_info, today_info)
            return {"month_profit": 50.0, "month_profit_rate": 0.8}

        with mock.patch.object(app_module, "get_positions", return_value=[]), \
             mock.patch.object(app_module, "get_trades", return_value=[]), \
             mock.patch.object(app_module, "get_asset", return_value={}), \
             mock.patch.object(app_module, "get_locked_positions_for_account", return_value=[]), \
             mock.patch.object(app_module, "calculate_today_profit_info", side_effect=fake_today), \
             mock.patch.object(app_module, "calculate_total_profit_info", side_effect=fake_total), \
             mock.patch.object(app_module, "calculate_month_profit_info", side_effect=fake_month):
            response = self.client.get("/api/data?account_id=all")

        self.assertEqual(200, response.status_code)
        self.assertEqual(["all"], calls)
        data = response.json()
        self.assertEqual(12.5, data["today_profit"])
        self.assertEqual(100.0, data["total_profit"])
        self.assertEqual(50.0, data["month_profit"])

    def test_trade_queries_have_account_time_indexes(self):
        source = Path(app_module.__file__).read_text(encoding="utf-8")

        self.assertIn("idx_trades_account_time", source)
        self.assertIn("idx_trades_time", source)


if __name__ == "__main__":
    unittest.main()
