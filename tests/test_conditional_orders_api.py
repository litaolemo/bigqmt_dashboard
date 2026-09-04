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


class ConditionalOrderPreferenceTests(unittest.TestCase):
    """条件单记住自己那套报价方式/交易类型，和手动下单的记忆完全隔开。

    为什么不共用一张表：两边合适的默认值根本不同。手动下单默认最新价（限价，
    报不掉当场看得见）；条件单默认对手方最优，因为止损单报不掉等于没止损，
    而它触发的时候人多半不在。把手动的「最新价」习惯继承给止损单是危险的。
    """

    account = "conditional-pref-test"

    def setUp(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        self.client = TestClient(app_module.app)
        self.bridge = FakeBridge([self.account]).__enter__()
        store.reset_for_tests()
        self._clean()

    def tearDown(self):
        self.bridge.__exit__(None, None, None)
        self.client.close()
        app_module.app.dependency_overrides.clear()
        store.reset_for_tests()
        self._clean()

    def _clean(self):
        conn = app_module.get_db_connection()
        for table in ("conditional_order_preferences", "order_preferences"):
            conn.execute("DELETE FROM %s WHERE account_id = ?" % table, (self.account,))
        conn.commit()
        conn.close()

    def _create(self, **extra):
        payload = {"account_id": self.account, "stock_code": "600000.SH",
                  "trigger_type": "stop_loss", "trigger_price": 8.5, "volume": 500}
        payload.update(extra)
        return self.client.post("/api/conditional-orders", json=payload)

    def _last_order(self):
        orders = self.client.get(
            "/api/conditional-orders?account_id=%s&include_inactive=true"
            % self.account).json()["orders"]
        return orders[0]

    def _instrument(self, query):
        return self.client.get("/api/instrument/600000.SH?%s" % query).json()["instrument"]

    # ---- 默认值 ----

    def test_without_a_choice_the_first_one_falls_back_to_peer(self):
        self.assertEqual(self._create().status_code, 200)
        self.assertEqual(self._last_order()["price_type"], "peer")

    def test_the_conditional_default_is_not_the_manual_default(self):
        # 手动面板是最新价，条件单是对手方最优 —— 这个差异是故意的，守住它
        self.assertEqual(self._instrument("side=sell")["default_price_type"], "latest")
        self.assertEqual(
            self._instrument("side=sell&scope=conditional")["default_price_type"], "peer")

    # ---- 记忆 ----

    def test_an_explicit_choice_comes_back_next_time(self):
        self.assertEqual(self._create(price_type="sh_five_cancel").status_code, 200)
        store.reset_for_tests()
        self.assertEqual(self._create().status_code, 200)
        self.assertEqual(self._last_order()["price_type"], "sh_five_cancel")

    def test_the_dialog_reads_the_conditional_memory(self):
        self.assertEqual(self._create(price_type="sh_five_cancel").status_code, 200)
        body = self._instrument(
            "account_id=%s&side=sell&scope=conditional" % self.account)
        self.assertEqual(body["default_price_type"], "sh_five_cancel")

    def test_buy_and_sell_are_remembered_separately(self):
        self.assertEqual(self._create(price_type="sh_five_cancel").status_code, 200)
        self.assertEqual(
            self._create(trigger_type="buy_dip", price_type="sh_five_limit").status_code,
            200)
        sell = self._instrument("account_id=%s&side=sell&scope=conditional" % self.account)
        buy = self._instrument("account_id=%s&side=buy&scope=conditional" % self.account)
        self.assertEqual(sell["default_price_type"], "sh_five_cancel")
        self.assertEqual(buy["default_price_type"], "sh_five_limit")

    def test_a_rejected_creation_is_not_remembered(self):
        # 触发价缺失，create 校验不过 —— 这次的选择不算「用过」
        response = self._create(trigger_price=0, price_type="sh_five_cancel")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._create().status_code, 200)
        self.assertEqual(self._last_order()["price_type"], "peer")

    # ---- 两套记忆互不污染 ----

    def test_the_manual_habit_is_not_inherited_by_conditional_orders(self):
        # 手动下单习惯用最新价，不该让止损单也变成最新价
        conn = app_module.get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO order_preferences "
            "(username, account_id, side, trade_mode, price_type) VALUES (?,?,?,?,?)",
            ("admin", self.account, "sell", "", "latest"))
        conn.commit()
        conn.close()
        self.assertEqual(self._create().status_code, 200)
        self.assertEqual(self._last_order()["price_type"], "peer")

    def test_building_a_conditional_order_does_not_touch_the_manual_memory(self):
        self.assertEqual(self._create(price_type="sh_five_cancel").status_code, 200)
        # 手动下单弹窗（scope 省略）该看到的还是它自己的默认值
        self.assertEqual(
            self._instrument("account_id=%s&side=sell" % self.account)
            ["default_price_type"], "latest")

    # ---- 继承来的值这次用不了就别用 ----

    def test_a_remembered_type_the_instrument_cannot_use_falls_back(self):
        conn = app_module.get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO conditional_order_preferences "
            "(username, account_id, side, trade_mode, price_type) VALUES (?,?,?,?,?)",
            ("admin", self.account, "sell", "", "sz_fok"))   # 深市专有
        conn.commit()
        conn.close()
        # 建的是沪市票：不该因为一个用户这次没选的「记忆」把建单卡掉
        response = self._create(stock_code="600000.SH")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._last_order()["price_type"], "peer")

    def test_a_remembered_mode_the_account_cannot_use_falls_back(self):
        conn = app_module.get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO conditional_order_preferences "
            "(username, account_id, side, trade_mode, price_type) VALUES (?,?,?,?,?)",
            ("admin", self.account, "sell", "margin", ""))   # 信用账户才有
        conn.commit()
        conn.close()
        self.assertEqual(self._create().status_code, 200)
        # 普通账户用不了融资融券，留空让触发时按账户类型取默认，
        # 而不是带着一个触发那天才会失败的值躺进库里
        self.assertEqual(self._last_order()["trade_mode"], "")

    def test_an_explicit_choice_wins_over_the_remembered_one(self):
        self.assertEqual(self._create(price_type="sh_five_cancel").status_code, 200)
        store.reset_for_tests()
        self.assertEqual(self._create(price_type="latest").status_code, 200)
        self.assertEqual(self._last_order()["price_type"], "latest")


if __name__ == "__main__":
    unittest.main()
