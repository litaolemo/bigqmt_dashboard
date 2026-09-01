"""报价方式、价格笼子、未成交委托与撤单状态。

限价单超出交易所的有效申报范围会被直接废单。与其让它出去撞墙，不如在服务端说清楚
超了多少、上下限是多少 —— 交易所的废单回执通常只有一个代码。

未成交委托是另一件事：成交流水只看得到「已经成交的」，下单到成交之间那段是盲区，
报进去没有、被没被废单、还挂着多少，都要靠 orders 表。
"""

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
from tests.fake_bridge import FakeBridge, FakePosition


def admin_user():
    return {"username": "admin", "role": "admin", "account_id": "admin"}


class PriceTypeAndCageTests(unittest.TestCase):
    account = "price-cage-test"
    # xtconstant: LATEST_PRICE=5, FIX_PRICE=11, PEER=44, MINE=45
    CONSTS = {"latest": 5, "fix": 11, "peer": 44, "mine": 45}

    def setUp(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        self.client = TestClient(app_module.app)
        self.bridge = FakeBridge([self.account]).__enter__()
        self._saved_reference = app_module.bridge_market.price_reference
        # 基准价 9.00、涨停 9.90、跌停 8.10 -> 连续竞价笼子 8.82 ~ 9.18
        app_module.bridge_market.price_reference = lambda code: {
            "code": code, "last_price": 9.00, "last_close": 9.00,
            "up_stop": 9.90, "down_stop": 8.10}
        self._clean()

    def tearDown(self):
        app_module.bridge_market.price_reference = self._saved_reference
        self.bridge.__exit__(None, None, None)
        self.client.close()
        app_module.app.dependency_overrides.clear()
        self._clean()

    def _clean(self):
        conn = app_module.get_db_connection()
        cur = conn.cursor()
        for table in ("trading_status", "position_locks", "order_audit", "orders"):
            cur.execute("DELETE FROM %s WHERE account_id = ?" % table, (self.account,))
        conn.commit()
        conn.close()

    def _buy(self, **extra):
        payload = {"account_id": self.account, "stock_code": "600000.SH", "amount": 100}
        payload.update(extra)
        return self.client.post("/api/position/buy_new", json=payload)

    def test_each_price_type_maps_to_its_xtconstant(self):
        for name, const in self.CONSTS.items():
            self.bridge.orders[:] = []
            extra = {"price_type": name}
            if name == "fix":
                extra["price"] = 9.05          # 笼子内
            response = self._buy(**extra)
            self.assertEqual(response.status_code, 200, "%s -> %s" % (name, response.text))
            self.assertEqual(self.bridge.orders[0]["price_type"], const, name)

    def test_limit_price_above_the_cage_is_rejected_with_the_limit(self):
        response = self._buy(price_type="fix", price=9.30)
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("上限", detail)
        self.assertIn("9.180", detail)
        self.assertEqual(self.bridge.orders, [])

    def test_limit_price_below_the_cage_is_rejected(self):
        response = self._buy(price_type="fix", price=8.00)
        self.assertEqual(response.status_code, 400)
        self.assertIn("下限", response.json()["detail"])
        self.assertEqual(self.bridge.orders, [])

    def test_limit_price_inside_the_cage_goes_through(self):
        response = self._buy(price_type="fix", price=9.18)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertAlmostEqual(self.bridge.orders[0]["price"], 9.18, places=2)

    def test_market_order_types_skip_the_cage(self):
        # 最新价 / 对手价 / 本方价由交易所定价，笼子不该拦
        for name in ("latest", "peer", "mine"):
            self.bridge.orders[:] = []
            self.assertEqual(self._buy(price_type=name).status_code, 200, name)

    def test_limit_order_without_a_price_is_rejected(self):
        response = self._buy(price_type="fix", price=0)
        self.assertEqual(response.status_code, 400)
        self.assertIn("有效价格", response.json()["detail"])

    def test_cage_falls_open_when_the_base_price_is_unknown(self):
        # 取不到基准价时不能凭猜的价格拦下用户的单
        app_module.bridge_market.price_reference = lambda code: {
            "code": code, "last_price": None, "last_close": None,
            "up_stop": None, "down_stop": None}
        response = self._buy(price_type="fix", price=999.0)
        self.assertEqual(response.status_code, 200, response.text)

    def test_sell_side_also_honours_the_cage(self):
        self.bridge.trader(self.account).positions["600000.SH"] = \
            FakePosition("600000.SH", 1000)
        response = self.client.post("/api/position/sell", json={
            "account_id": self.account, "stock_code": "600000.SH",
            "percentage": 50, "price_type": "fix", "price": 8.00})
        self.assertEqual(response.status_code, 400)
        self.assertIn("下限", response.json()["detail"])

    def test_instrument_endpoint_exposes_the_cage_and_the_choices(self):
        body = self.client.get("/api/instrument/600000.SH").json()["instrument"]
        common = [c["value"] for c in body["price_types"] if c["group"] == "常用"]
        self.assertEqual(common, ["latest", "fix", "peer", "mine", "stop"])
        buy_cage = body["price_cage"]["buy"]
        self.assertAlmostEqual(buy_cage["low"], 8.82, places=2)
        self.assertAlmostEqual(buy_cage["high"], 9.18, places=2)


class PendingOrderTests(unittest.TestCase):
    account = "pending-order-test"

    def setUp(self):
        for dep in (app_module.get_current_user, app_module.get_user_or_viewer):
            app_module.app.dependency_overrides[dep] = admin_user
        self.client = TestClient(app_module.app)
        self._clean()
        app_module.save_orders(self.account, [
            {"order_id": "P1", "order_sysid": "S1", "stock_code": "600000.SH",
             "instrument_name": "浦发银行", "order_type": 23, "order_status": 50,
             "order_volume": 1000, "traded_volume": 0, "price": 9.0,
             "traded_price": 0, "order_time": 1788000000, "strategy_name": "demo",
             "order_remark": "", "status_msg": ""},
            {"order_id": "P2", "order_sysid": "S2", "stock_code": "113050.SH",
             "instrument_name": "测试转债", "order_type": 24, "order_status": 55,
             "order_volume": 100, "traded_volume": 40, "price": 120.5,
             "traded_price": 120.5, "order_time": 1788000100, "strategy_name": "demo",
             "order_remark": "", "status_msg": ""},
            {"order_id": "D1", "order_sysid": "S3", "stock_code": "600000.SH",
             "instrument_name": "浦发银行", "order_type": 23, "order_status": 56,
             "order_volume": 500, "traded_volume": 500, "price": 9.0,
             "traded_price": 9.0, "order_time": 1788000200, "strategy_name": "demo",
             "order_remark": "", "status_msg": ""},
        ])

    def tearDown(self):
        self.client.close()
        app_module.app.dependency_overrides.clear()
        self._clean()

    def _clean(self):
        conn = app_module.get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM orders WHERE account_id = ?", (self.account,))
        conn.commit()
        conn.close()

    def _pending(self, query=""):
        response = self.client.get(
            "/api/trade-flow?account_id=%s&days=30&%s" % (self.account, query))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["pending"]

    def test_finished_orders_are_not_listed_as_pending(self):
        ids = {row["order_id"] for row in self._pending()}
        self.assertEqual(ids, {"P1", "P2"}, "已成的 D1 不该出现在未成交里")

    def test_status_is_labelled_and_cancelable_is_flagged(self):
        by_id = {row["order_id"]: row for row in self._pending()}
        self.assertEqual(by_id["P1"]["status"], "已报")
        self.assertEqual(by_id["P2"]["status"], "部成")
        self.assertTrue(by_id["P1"]["cancelable"])
        self.assertTrue(by_id["P2"]["cancelable"])

    def test_remaining_volume_is_computed(self):
        by_id = {row["order_id"]: row for row in self._pending()}
        self.assertEqual(by_id["P1"]["left_volume"], 1000)
        self.assertEqual(by_id["P2"]["left_volume"], 60)      # 100 - 40

    def test_units_follow_the_instrument(self):
        by_id = {row["order_id"]: row for row in self._pending()}
        self.assertEqual(by_id["P2"]["unit"], "张")
        self.assertTrue(by_id["P2"]["is_bond"])
        self.assertEqual(by_id["P1"]["unit"], "股")

    def test_side_filter_applies_to_pending_too(self):
        self.assertEqual({r["order_id"] for r in self._pending("side=buy")}, {"P1"})
        self.assertEqual({r["order_id"] for r in self._pending("side=sell")}, {"P2"})

    def test_orders_endpoint_labels_status_as_well(self):
        response = self.client.get("/api/orders?account_id=%s" % self.account)
        self.assertEqual(response.status_code, 200, response.text)
        by_id = {row["order_id"]: row for row in response.json()["orders"]}
        self.assertEqual(by_id["P1"]["status"], "已报")
        self.assertTrue(by_id["P1"]["cancelable"])
        self.assertEqual(by_id["P1"]["side_label"], "买入")


class OrderStatusLabelTests(unittest.TestCase):
    def test_states_already_being_cancelled_are_not_cancelable_again(self):
        for status, label, cancelable in (
                (48, "未报", True), (49, "待报", True), (50, "已报", True),
                (51, "已报待撤", False), (52, "部成待撤", False),
                (53, "部撤", False), (54, "已撤", False), (55, "部成", True),
                (56, "已成", False), (57, "废单", False)):
            described = app_module.describe_order_status(status)
            self.assertEqual(described["label"], label, status)
            self.assertEqual(described["cancelable"], cancelable, label)

    def test_unknown_status_degrades_instead_of_crashing(self):
        for value in (None, "", "abc", 999):
            described = app_module.describe_order_status(value)
            self.assertFalse(described["cancelable"])
            self.assertIsInstance(described["label"], str)


class TradeModeTests(unittest.TestCase):
    """买卖指令类型要跟着账户类型走。

    信用账户的「买」发 23，交易所收到的是一笔真实但业务类型不同的单。
    """

    account = "trade-mode-test"

    def setUp(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        self.client = TestClient(app_module.app)
        self._clean()

    def tearDown(self):
        self.client.close()
        app_module.app.dependency_overrides.clear()
        self._clean()

    def _clean(self):
        conn = app_module.get_db_connection()
        cur = conn.cursor()
        for table in ("trading_status", "position_locks", "order_audit", "orders"):
            cur.execute("DELETE FROM %s WHERE account_id = ?" % table, (self.account,))
        conn.commit()
        conn.close()

    def _buy(self, bridge, **extra):
        payload = {"account_id": self.account, "stock_code": "600000.SH", "amount": 100}
        payload.update(extra)
        return self.client.post("/api/position/buy_new", json=payload)

    def test_stock_account_sends_the_ordinary_buy_and_sell(self):
        with FakeBridge([self.account]) as bridge:
            self.assertEqual(self._buy(bridge).status_code, 200)
            self.assertEqual(bridge.orders[0]["order_type"], 23)

            bridge.trader(self.account).positions["600000.SH"] = FakePosition("600000.SH", 1000)
            bridge.orders[:] = []
            response = self.client.post("/api/position/sell", json={
                "account_id": self.account, "stock_code": "600000.SH", "percentage": 100})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(bridge.orders[0]["order_type"], 24)

    def test_credit_account_defaults_to_the_collateral_op_types(self):
        with FakeBridge([self.account], account_type="CREDIT") as bridge:
            self.assertEqual(self._buy(bridge).status_code, 200)
            self.assertEqual(bridge.orders[0]["order_type"], 33, "担保品买入是 33 不是 23")

            bridge.trader(self.account).positions["600000.SH"] = FakePosition("600000.SH", 1000)
            bridge.orders[:] = []
            response = self.client.post("/api/position/sell", json={
                "account_id": self.account, "stock_code": "600000.SH", "percentage": 100})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(bridge.orders[0]["order_type"], 34)

    def test_margin_mode_is_explicit_never_the_default(self):
        with FakeBridge([self.account], account_type="CREDIT") as bridge:
            self.assertEqual(self._buy(bridge, trade_mode="margin").status_code, 200)
            self.assertEqual(bridge.orders[0]["order_type"], 27)   # 融资买入

    def test_a_mode_the_account_cannot_use_is_rejected_before_the_bridge(self):
        with FakeBridge([self.account]) as bridge:   # 普通账户
            response = self._buy(bridge, trade_mode="margin")
            self.assertEqual(response.status_code, 400)
            self.assertIn("CREDIT", response.json()["detail"])
            self.assertEqual(bridge.orders, [], "拒单了就不能有报单")

    def test_unknown_mode_is_rejected_rather_than_defaulted(self):
        with FakeBridge([self.account]) as bridge:
            response = self._buy(bridge, trade_mode="whatever")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(bridge.orders, [])

    def test_instrument_endpoint_reports_the_modes_for_the_account(self):
        with FakeBridge([self.account], account_type="CREDIT"):
            body = self.client.get(
                "/api/instrument/600000.SH?account_id=%s" % self.account).json()["instrument"]
            self.assertEqual(body["account_type"], "CREDIT")
            self.assertEqual(body["default_trade_mode"], "collateral")
            self.assertEqual([m["value"] for m in body["trade_modes"]],
                             ["normal", "collateral", "margin", "repay"])

    def test_instrument_endpoint_without_an_account_assumes_a_plain_one(self):
        with FakeBridge([self.account]):
            body = self.client.get("/api/instrument/600000.SH").json()["instrument"]
            self.assertEqual([m["value"] for m in body["trade_modes"]], ["normal"])


class OrderPreferenceTests(unittest.TestCase):
    """记住上一次真正报出去用的交易类型 / 报价方式。

    按（人、账号、方向）分开存 —— 买习惯挂单、卖习惯对手价是常见组合。
    """

    account = "order-pref-test"

    def setUp(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        self.client = TestClient(app_module.app)
        self.bridge = FakeBridge([self.account]).__enter__()
        self._clean()

    def tearDown(self):
        self.bridge.__exit__(None, None, None)
        self.client.close()
        app_module.app.dependency_overrides.clear()
        self._clean()

    def _clean(self):
        conn = app_module.get_db_connection()
        cur = conn.cursor()
        for table in ("trading_status", "position_locks", "order_audit", "orders"):
            cur.execute("DELETE FROM %s WHERE account_id = ?" % table, (self.account,))
        cur.execute("DELETE FROM order_preferences WHERE account_id = ?", (self.account,))
        conn.commit()
        conn.close()

    def _instrument(self, side):
        return self.client.get(
            "/api/instrument/600000.SH?account_id=%s&side=%s" % (self.account, side)
        ).json()["instrument"]

    def _buy(self, **extra):
        payload = {"account_id": self.account, "stock_code": "600000.SH", "amount": 100}
        payload.update(extra)
        return self.client.post("/api/position/buy_new", json=payload)

    def test_the_choice_used_last_time_comes_back_as_the_default(self):
        self.assertEqual(self._instrument("buy")["default_price_type"], "latest")
        self.assertEqual(self._buy(price_type="peer").status_code, 200)
        self.assertEqual(self._instrument("buy")["default_price_type"], "peer")

    def test_buy_and_sell_are_remembered_separately(self):
        self.assertEqual(self._buy(price_type="peer").status_code, 200)
        self.bridge.trader(self.account).positions["600000.SH"] =             FakePosition("600000.SH", 1000)
        response = self.client.post("/api/position/sell", json={
            "account_id": self.account, "stock_code": "600000.SH",
            "percentage": 100, "price_type": "mine"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._instrument("buy")["default_price_type"], "peer")
        self.assertEqual(self._instrument("sell")["default_price_type"], "mine")

    def test_a_rejected_order_is_not_remembered(self):
        # 被价格笼子拦下的那次不算「用过」
        self.assertEqual(self._buy(price_type="fix", price=0).status_code, 400)
        self.assertEqual(self._instrument("buy")["default_price_type"], "latest")

    def test_a_remembered_choice_the_instrument_cannot_use_falls_back(self):
        # 深市专有的市价指令记在了偏好里，下次开的是沪市票
        conn = app_module.get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO order_preferences "
            "(username, account_id, side, trade_mode, price_type) VALUES (?,?,?,?,?)",
            ("admin", self.account, "buy", "", "sz_fok"))
        conn.commit()
        conn.close()
        self.assertEqual(self._instrument("buy")["default_price_type"], "latest")

    def test_trade_mode_is_remembered_too(self):
        with FakeBridge([self.account], account_type="CREDIT"):
            self.assertEqual(self._buy(trade_mode="margin").status_code, 200)
            body = self.client.get(
                "/api/instrument/600000.SH?account_id=%s&side=buy" % self.account
            ).json()["instrument"]
            self.assertEqual(body["default_trade_mode"], "margin")

    def test_a_remembered_mode_the_account_cannot_use_falls_back(self):
        # 先在信用账户上选了融资买入，后来账户改回普通
        conn = app_module.get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO order_preferences "
            "(username, account_id, side, trade_mode, price_type) VALUES (?,?,?,?,?)",
            ("admin", self.account, "buy", "margin", ""))
        conn.commit()
        conn.close()
        self.assertEqual(self._instrument("buy")["default_trade_mode"], "normal")

    def test_without_a_side_nothing_is_restored(self):
        self.assertEqual(self._buy(price_type="peer").status_code, 200)
        body = self.client.get(
            "/api/instrument/600000.SH?account_id=%s" % self.account).json()["instrument"]
        self.assertEqual(body["default_price_type"], "latest")


if __name__ == "__main__":
    unittest.main()
