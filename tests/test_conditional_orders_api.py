"""/api/conditional-orders：创建/列出/撤销止损止盈条件单。"""

import sys
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
from bridge import market as bridge_market
from tests.fake_bridge import FakeBridge
from triggers import store


def admin_user():
    return {"username": "admin", "role": "admin", "account_id": "admin"}


def ordinary_user(account_id):
    def fn():
        return {"username": account_id, "role": "user", "account_id": account_id}
    return fn


class ConditionalOrderApiTests(unittest.TestCase):
    account = "conditional-api-test"
    other_account = "conditional-api-other"

    def setUp(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        self.client = TestClient(app_module.app)
        self.bridge = FakeBridge([self.account, self.other_account]).__enter__()
        store.reset_for_tests()

    def tearDown(self):
        self.bridge.__exit__(None, None, None)
        self.client.close()
        app_module.app.dependency_overrides.clear()
        store.reset_for_tests()

    def _create(self, **extra):
        payload = {"account_id": self.account, "stock_code": "600000.SH",
                  "trigger_type": "stop_loss", "trigger_price": 8.5, "volume": 500}
        payload.update(extra)
        return self.client.post("/api/conditional-orders", json=payload)

    def test_create_returns_the_new_id(self):
        response = self._create()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertTrue(body["id"])

    def test_invalid_trigger_type_is_a_400_not_a_500(self):
        response = self._create(trigger_type="not_a_real_type")
        self.assertEqual(response.status_code, 400)
        self.assertIn("不支持", response.json()["detail"])

    def test_missing_volume_and_percentage_is_a_400(self):
        response = self._create(volume=0, percentage=0)
        self.assertEqual(response.status_code, 400)

    def test_ordinary_user_cannot_create_for_another_account(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = \
            ordinary_user(self.other_account)
        response = self._create()   # account_id 还是 self.account
        self.assertEqual(response.status_code, 403)

    def test_ordinary_user_can_create_for_their_own_account(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = \
            ordinary_user(self.account)
        response = self._create(account_id=self.account)
        self.assertEqual(response.status_code, 200, response.text)

    def test_list_returns_the_created_order_with_labels(self):
        self._create()
        response = self.client.get("/api/conditional-orders?account_id=%s" % self.account)
        self.assertEqual(response.status_code, 200)
        orders = response.json()["orders"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["trigger_label"], "止损")
        self.assertEqual(orders[0]["side_label"], "卖出")
        self.assertEqual(orders[0]["unit"], "股")

    def test_list_defaults_to_active_only(self):
        created = self._create().json()["id"]
        self.client.post("/api/conditional-orders/cancel", json={"id": created})
        active = self.client.get(
            "/api/conditional-orders?account_id=%s" % self.account).json()["orders"]
        self.assertEqual(active, [])
        history = self.client.get(
            "/api/conditional-orders?account_id=%s&include_inactive=true"
            % self.account).json()["orders"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "cancelled")

    def test_ordinary_user_only_sees_their_own_account(self):
        self._create()   # self.account
        self._create(account_id=self.other_account)
        app_module.app.dependency_overrides[app_module.get_current_user] = \
            ordinary_user(self.account)
        orders = self.client.get(
            "/api/conditional-orders?account_id=all").json()["orders"]
        self.assertEqual({o["account_id"] for o in orders}, {self.account})

    def test_cancel_succeeds_for_an_active_order(self):
        created = self._create().json()["id"]
        response = self.client.post("/api/conditional-orders/cancel", json={"id": created})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(store.get(created)["status"], store.STATUS_CANCELLED)

    def test_cancel_a_missing_order_is_a_404(self):
        response = self.client.post("/api/conditional-orders/cancel", json={"id": 999999})
        self.assertEqual(response.status_code, 404)

    def test_cancel_twice_is_a_400_the_second_time(self):
        created = self._create().json()["id"]
        self.client.post("/api/conditional-orders/cancel", json={"id": created})
        response = self.client.post("/api/conditional-orders/cancel", json={"id": created})
        self.assertEqual(response.status_code, 400)

    def test_ordinary_user_cannot_cancel_another_accounts_order(self):
        created = self._create(account_id=self.other_account).json()["id"]
        app_module.app.dependency_overrides[app_module.get_current_user] = \
            ordinary_user(self.account)
        response = self.client.post("/api/conditional-orders/cancel", json={"id": created})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(store.get(created)["status"], store.STATUS_ACTIVE)


class DynamicAndPercentTriggerApiTests(unittest.TestCase):
    """涨停破板卖出 / 涨停买入（触发价动态）、涨跌幅 % 触发（换算成绝对价）。"""

    account = "conditional-api-dynamic-test"

    def setUp(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        self.client = TestClient(app_module.app)
        self.bridge = FakeBridge([self.account]).__enter__()
        store.reset_for_tests()

    def tearDown(self):
        self.bridge.__exit__(None, None, None)
        self.client.close()
        app_module.app.dependency_overrides.clear()
        store.reset_for_tests()

    def _create(self, **extra):
        payload = {"account_id": self.account, "stock_code": "600000.SH", "volume": 500}
        payload.update(extra)
        return self.client.post("/api/conditional-orders", json=payload)

    def test_limit_up_break_needs_no_trigger_price(self):
        response = self._create(trigger_type="limit_up_break")
        self.assertEqual(response.status_code, 200, response.text)

    def test_limit_up_buy_needs_no_trigger_price(self):
        response = self._create(trigger_type="limit_up_buy")
        self.assertEqual(response.status_code, 200, response.text)

    def test_list_labels_the_dynamic_types(self):
        self._create(trigger_type="limit_up_break")
        orders = self.client.get(
            "/api/conditional-orders?account_id=%s" % self.account).json()["orders"]
        self.assertEqual(orders[0]["trigger_label"], "涨停破板卖出")

    def test_trigger_pct_resolves_to_an_absolute_price(self):
        with mock.patch.object(
                bridge_market, "price_reference",
                return_value={"code": "600000.SH", "last_price": 9.0,
                             "last_close": 10.0, "up_stop": 11.0, "down_stop": 9.0}):
            response = self._create(trigger_type="stop_loss", trigger_pct=8)
        self.assertEqual(response.status_code, 200, response.text)
        orders = self.client.get(
            "/api/conditional-orders?account_id=%s" % self.account).json()["orders"]
        self.assertAlmostEqual(orders[0]["trigger_price"], 9.2, places=3)

    def test_a_dynamic_type_with_a_trigger_price_is_rejected(self):
        response = self._create(trigger_type="limit_up_break", trigger_price=9.9)
        self.assertEqual(response.status_code, 400)

    def test_neither_price_nor_pct_for_an_ordinary_type_is_rejected(self):
        response = self._create(trigger_type="stop_loss")
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
