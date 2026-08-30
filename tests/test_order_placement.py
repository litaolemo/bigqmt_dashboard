"""下单链路：从 HTTP 接口到报单参数。

改造前这套接口做的是「往内存队列塞一条指令」，测试只能断言队列里有什么、以及
/api/data 的响应里回带了什么。现在下单是当场报的，所以测的是：报单参数对不对、
风控闸门拦不拦得住、审计有没有留痕。
"""

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
from tests.fake_bridge import FakeBridge, FakeOrder, FakePosition

BUY = 23
SELL = 24


def admin_user():
    return {"username": "admin", "role": "admin", "account_id": "admin"}


class OrderEndpointTests(unittest.TestCase):
    account = "order-test-account"

    def setUp(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        self.client = TestClient(app_module.app)
        self.bridge = FakeBridge([self.account]).__enter__()
        self._clean_db()

    def tearDown(self):
        self.bridge.__exit__(None, None, None)
        self.client.close()
        app_module.app.dependency_overrides.clear()
        self._clean_db()

    def _clean_db(self):
        conn = app_module.get_db_connection()
        cursor = conn.cursor()
        for table in ("trading_status", "position_locks", "order_audit", "orders"):
            cursor.execute("DELETE FROM %s WHERE account_id = ?" % table, (self.account,))
        conn.commit()
        conn.close()

    def _set_flag(self, column, value=1):
        conn = app_module.get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO trading_status (account_id, %s) VALUES (?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET %s = excluded.%s" % (column, column, column),
            (self.account, value))
        conn.commit()
        conn.close()

    def _audit_rows(self):
        return app_module.get_order_audit(self.account, 20)

    # ------------------------------------------------------------------ 买入
    def test_new_buy_places_order_and_returns_real_order_id(self):
        response = self.client.post("/api/position/buy_new", json={
            "account_id": self.account, "stock_code": "000001.SZ",
            "stock_name": "平安银行", "amount": 300,
        })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertTrue(body["order_sys_id"])

        self.assertEqual(len(self.bridge.orders), 1)
        order = self.bridge.orders[0]
        self.assertEqual(order["stock_code"], "000001.SZ")
        self.assertEqual(order["order_type"], BUY)
        self.assertEqual(order["order_volume"], 300)

    def test_convertible_bond_can_be_bought_in_ten_lots(self):
        # 改造前 buy_new 写死 `amount % 100 != 0`，转债 10 张会被判非法参数
        response = self.client.post("/api/position/buy_new", json={
            "account_id": self.account, "stock_code": "113050.SH", "amount": 10,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.bridge.orders[0]["order_volume"], 10)
        self.assertEqual(response.json()["unit"], "张")

    def test_convertible_bond_rejects_volume_below_one_lot(self):
        response = self.client.post("/api/position/buy_new", json={
            "account_id": self.account, "stock_code": "113050.SH", "amount": 5,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("10张", response.json()["detail"].replace(" ", ""))
        self.assertEqual(self.bridge.orders, [])

    def test_star_board_buy_below_two_hundred_is_rejected(self):
        response = self.client.post("/api/position/buy_new", json={
            "account_id": self.account, "stock_code": "688981.SH", "amount": 100,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.bridge.orders, [])

    # ------------------------------------------------------------ 风控闸门
    def test_stop_sell_blocks_the_order_at_the_server(self):
        # 改造前 stop_sell 只是随 /api/data 下发给客户端的一个布尔值，
        # 客户端不听服务端也没办法。现在是服务端闸门，根本报不出去。
        self._set_flag("sell_stopped")
        self.bridge.trader(self.account).positions["000001.SZ"] = \
            FakePosition("000001.SZ", 1000)

        response = self.client.post("/api/position/sell", json={
            "account_id": self.account, "stock_code": "000001.SZ", "percentage": 50,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("停止卖出", response.json()["detail"])
        self.assertEqual(self.bridge.orders, [])

    def test_locked_position_cannot_be_sold_until_unlocked(self):
        conn = app_module.get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO position_locks (account_id, stock_code, is_locked) VALUES (?, ?, 1)",
            (self.account, "000001.SZ"))
        conn.commit()
        conn.close()
        self.bridge.trader(self.account).positions["000001.SZ"] = \
            FakePosition("000001.SZ", 1000)

        response = self.client.post("/api/position/sell", json={
            "account_id": self.account, "stock_code": "000001.SZ", "percentage": 100,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("锁仓", response.json()["detail"])
        self.assertEqual(self.bridge.orders, [])

    def test_manual_new_buy_still_bypasses_stop_buy(self):
        # 保留老行为：手动新开仓忽略「停止买入」（老客户端的 ignore_stop_buy），
        # 但绕过动作要留在审计里。
        self._set_flag("buy_stopped")
        response = self.client.post("/api/position/buy_new", json={
            "account_id": self.account, "stock_code": "000001.SZ", "amount": 100,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.bridge.orders), 1)

    def test_percentage_buy_respects_stop_buy(self):
        # 加仓走的是普通路径，不带 bypass，停买必须拦住
        self._set_flag("buy_stopped")
        self.bridge.trader(self.account).positions["000001.SZ"] = \
            FakePosition("000001.SZ", 1000)
        response = self.client.post("/api/position/buy", json={
            "account_id": self.account, "stock_code": "000001.SZ", "percentage": 50,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("停止买入", response.json()["detail"])
        self.assertEqual(self.bridge.orders, [])

    # -------------------------------------------------------------- 卖出算量
    def test_sell_percentage_uses_live_available_volume(self):
        # 可用 800（另 200 是当日买入未解冻），50% 应该是 400 而不是按总量 1000 算
        self.bridge.trader(self.account).positions["000001.SZ"] = \
            FakePosition("000001.SZ", 1000, can_use_volume=800)
        response = self.client.post("/api/position/sell", json={
            "account_id": self.account, "stock_code": "000001.SZ", "percentage": 50,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.bridge.orders[0]["order_volume"], 400)
        self.assertEqual(self.bridge.orders[0]["order_type"], SELL)

    def test_full_sell_includes_odd_lot(self):
        # 137 股全卖：零股必须一次性卖出，不能被规整成 100
        self.bridge.trader(self.account).positions["000001.SZ"] = \
            FakePosition("000001.SZ", 137)
        response = self.client.post("/api/position/sell", json={
            "account_id": self.account, "stock_code": "000001.SZ", "percentage": 100,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.bridge.orders[0]["order_volume"], 137)

    def test_sell_without_available_volume_is_rejected(self):
        response = self.client.post("/api/position/sell", json={
            "account_id": self.account, "stock_code": "000001.SZ", "percentage": 50,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("无可用持仓", response.json()["detail"])

    def test_sell_amount_is_capped_by_available_volume(self):
        self.bridge.trader(self.account).positions["000001.SZ"] = \
            FakePosition("000001.SZ", 500)
        response = self.client.post("/api/position/sell_amount", json={
            "account_id": self.account, "stock_code": "000001.SZ", "amount": 900,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.bridge.orders[0]["order_volume"], 500)

    # ---------------------------------------------------------------- 撤单
    def test_sell_cancel_cancels_live_orders_instead_of_dequeuing(self):
        trader = self.bridge.trader(self.account)
        trader.open_orders = [
            FakeOrder(1, "000001.SZ", SELL),
            FakeOrder(2, "000001.SZ", BUY),      # 买单不该被撤
            FakeOrder(3, "600000.SH", SELL),     # 别的票不该被撤
        ]
        response = self.client.post("/api/position/sell/cancel", json={
            "account_id": self.account, "stock_code": "000001.SZ", "percentage": 100,
        })
        self.assertEqual(response.status_code, 200, response.text)
        cancels = [o for o in self.bridge.orders if "cancel" in o]
        self.assertEqual([c["cancel"] for c in cancels], [1])

    # ---------------------------------------------------------------- 审计
    def test_every_order_attempt_is_written_to_the_audit_trail(self):
        self.client.post("/api/position/buy_new", json={
            "account_id": self.account, "stock_code": "000001.SZ", "amount": 100,
        })
        self._set_flag("sell_stopped")
        self.client.post("/api/position/sell", json={
            "account_id": self.account, "stock_code": "000001.SZ", "percentage": 100,
        })

        rows = self._audit_rows()
        statuses = {r["side"]: r["status"] for r in rows}
        self.assertEqual(statuses.get("buy"), "submitted")
        # 卖出因为无可用持仓被挡在 sell_by_percentage，不会进审计；
        # 有持仓时被风控拦下的才会。这里补一笔有持仓的：
        self.bridge.trader(self.account).positions["000001.SZ"] = \
            FakePosition("000001.SZ", 1000)
        self.client.post("/api/position/sell", json={
            "account_id": self.account, "stock_code": "000001.SZ", "percentage": 100,
        })
        rejected = [r for r in self._audit_rows() if r["status"] == "rejected"]
        self.assertTrue(rejected, "被风控拒绝的单子也必须留痕")
        self.assertIn("停止卖出", rejected[0]["message"])

    def test_order_failure_from_qmt_surfaces_as_error_not_success(self):
        self.bridge.trader(self.account).fail_with = "资金不足"
        response = self.client.post("/api/position/buy_new", json={
            "account_id": self.account, "stock_code": "000001.SZ", "amount": 100,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("资金不足", response.json()["detail"])


class OrderGatewayGuardTests(unittest.TestCase):
    """不依赖 HTTP 的闸门测试。"""

    def test_account_without_allow_order_cannot_place_anything(self):
        with FakeBridge(["locked-account"], allow_order=False):
            result = app_module.bridge_orders.place_order(
                "locked-account", "000001.SZ", "buy", 100)
        self.assertFalse(result["ok"])
        self.assertIn("未开启下单", result["message"])

    def test_unknown_account_is_rejected_not_crashed(self):
        with FakeBridge(["known"]):
            result = app_module.bridge_orders.place_order(
                "unknown", "000001.SZ", "buy", 100)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
