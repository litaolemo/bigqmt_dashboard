"""可选数据源全部缺失时，接口必须降级而不是 500。

README 承诺「没有任何配置也能启动」，这里守住那句话。真实发现过的问题：
/api/market-data/today 在没配 MySQL 时抛 500，把整个「今日报出」面板打挂。
"""

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
from plugins import mysql_client, tushare_client


def admin_user():
    return {"username": "admin", "role": "admin", "account_id": "degradation-test"}


class MissingOptionalSourceTests(unittest.TestCase):
    """把 Tushare / MySQL 都掐掉，确认接口仍然 200。"""

    def setUp(self):
        for dep in (app_module.get_current_user, app_module.get_current_admin,
                    app_module.get_user_or_viewer):
            app_module.app.dependency_overrides[dep] = admin_user
        self.client = TestClient(app_module.app)

        self._saved = {
            "stock_db_available": mysql_client.stock_db_available,
            "get_stock_db_connection": mysql_client.get_stock_db_connection,
            "sync_enabled": mysql_client.sync_enabled,
            "app_get_stock_db_connection": app_module.get_stock_db_connection,
            "tushare_get_pro": tushare_client.get_pro,
        }

        def no_mysql(*_a, **_kw):
            raise mysql_client.MySQLUnavailable("测试：未配置 MySQL")

        mysql_client.stock_db_available = lambda: False
        mysql_client.get_stock_db_connection = no_mysql
        mysql_client.sync_enabled = lambda: False
        app_module.get_stock_db_connection = no_mysql
        tushare_client.get_pro = lambda: None

    def tearDown(self):
        mysql_client.stock_db_available = self._saved["stock_db_available"]
        mysql_client.get_stock_db_connection = self._saved["get_stock_db_connection"]
        mysql_client.sync_enabled = self._saved["sync_enabled"]
        app_module.get_stock_db_connection = self._saved["app_get_stock_db_connection"]
        tushare_client.get_pro = self._saved["tushare_get_pro"]
        self.client.close()
        app_module.app.dependency_overrides.clear()

    def test_market_data_today_returns_empty_list_not_500(self):
        response = self.client.get("/api/market-data/today")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["records"], [])
        self.assertIn("未配置", body.get("note", ""))

    def test_stock_search_degrades_to_empty_results(self):
        response = self.client.get("/api/stocks/search?q=平安")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(response.json(), list)

    def test_research_board_still_lists(self):
        response = self.client.get("/api/research-board")
        self.assertEqual(response.status_code, 200, response.text)

    def test_dashboard_page_and_bundle_still_serve(self):
        for path in ("/", "/vue-app.js"):
            self.assertEqual(self.client.get(path).status_code, 200, path)


class MissingBridgeTests(unittest.TestCase):
    """没有配置任何大QMT 账号时，只读接口照常，交易接口给明确原因。"""

    def setUp(self):
        for dep in (app_module.get_current_user, app_module.get_current_admin):
            app_module.app.dependency_overrides[dep] = admin_user
        self.client = TestClient(app_module.app)
        self._account_ids = app_module.bridge_config.account_ids
        self._list_accounts = app_module.bridge_config.list_accounts
        app_module.bridge_config.account_ids = lambda enabled_only=True: []
        app_module.bridge_config.list_accounts = lambda enabled_only=True: []

    def tearDown(self):
        app_module.bridge_config.account_ids = self._account_ids
        app_module.bridge_config.list_accounts = self._list_accounts
        self.client.close()
        app_module.app.dependency_overrides.clear()

    def test_health_endpoint_reports_no_accounts_instead_of_failing(self):
        response = self.client.get("/api/accounts/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["accounts"], [])

    def test_orders_view_is_empty_not_broken(self):
        response = self.client.get("/api/orders?account_id=all")
        self.assertEqual(response.status_code, 200, response.text)

    def test_instrument_rules_work_without_any_connection(self):
        # 品种规则是纯代码段判断，不该依赖任何连接
        response = self.client.get("/api/instrument/113050.SH")
        self.assertEqual(response.status_code, 200)
        spec = response.json()["instrument"]
        self.assertEqual(spec["min_volume"], 10)
        self.assertEqual(spec["unit"], "张")

    def test_order_without_configured_account_is_rejected_with_a_reason(self):
        response = self.client.post("/api/position/buy_new", json={
            "account_id": "nosuch", "stock_code": "600000.SH", "amount": 100})
        self.assertEqual(response.status_code, 400)
        self.assertIn("未开启下单", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
