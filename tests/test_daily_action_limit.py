"""全局熔断：一天内买卖撤单合计次数上限（bridge/orders.py）。

防的是软件自己失控——某个 bug 在短时间内反复真实报单/撤单。只在真的要碰
交易所之前计数，被更早的校验/风控拦下的请求不算数，也不该被这些无关的
失败请求提前把额度耗尽。
"""

import datetime
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import orders as bridge_orders
from tests.fake_bridge import FakeBridge, FakePosition

ACCOUNT = "daily-limit-test"


class DailyActionLimitTests(unittest.TestCase):
    def setUp(self):
        bridge_orders.reset_daily_action_count()
        self._saved_limit = bridge_orders.DAILY_ACTION_LIMIT
        self.bridge = FakeBridge([ACCOUNT]).__enter__()

    def tearDown(self):
        self.bridge.__exit__(None, None, None)
        bridge_orders.DAILY_ACTION_LIMIT = self._saved_limit
        bridge_orders.reset_daily_action_count()

    def _set_limit(self, n):
        bridge_orders.DAILY_ACTION_LIMIT = n

    def test_count_starts_at_zero(self):
        self.assertEqual(bridge_orders.daily_action_count(), 0)

    def test_a_successful_order_counts_once(self):
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        self.assertEqual(bridge_orders.daily_action_count(), 1)

    def test_a_cancel_counts_too(self):
        bridge_orders.cancel_order(ACCOUNT, order_id="1")
        self.assertEqual(bridge_orders.daily_action_count(), 1)

    def test_buy_sell_and_cancel_share_the_same_counter(self):
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        self.bridge.trader(ACCOUNT).positions["600000.SH"] = FakePosition("600000.SH", 100)
        bridge_orders.place_order(ACCOUNT, "600000.SH", "sell", 100)
        bridge_orders.cancel_order(ACCOUNT, order_id="1")
        self.assertEqual(bridge_orders.daily_action_count(), 3)

    def test_a_request_rejected_before_reaching_the_exchange_does_not_count(self):
        # 账号没配置/未开下单，place_order 在真正碰交易所之前就被拒了
        response = bridge_orders.place_order("no-such-account", "600000.SH", "buy", 100)
        self.assertFalse(response["ok"])
        self.assertEqual(bridge_orders.daily_action_count(), 0)

    def test_a_risk_gate_rejection_does_not_count(self):
        def rejecting_gate(_r):
            raise bridge_orders.OrderRejected("停止买入")

        with bridge_orders.temporary_risk_gate(rejecting_gate):
            response = bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        self.assertFalse(response["ok"])
        self.assertEqual(bridge_orders.daily_action_count(), 0)

    def test_trips_once_the_limit_is_exceeded(self):
        self._set_limit(2)
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        response = bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "rejected")
        self.assertIn("熔断", response["message"])

    def test_a_tripped_order_still_does_not_reach_the_exchange(self):
        self._set_limit(1)
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        self.bridge.orders[:] = []
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        self.assertEqual(self.bridge.orders, [], "熔断之后不该再有真实报单发出去")

    def test_the_trip_also_blocks_cancellation(self):
        self._set_limit(1)
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        response = bridge_orders.cancel_order(ACCOUNT, order_id="1")
        self.assertFalse(response["ok"])
        self.assertIn("熔断", response["message"])

    def test_reset_clears_the_count(self):
        self._set_limit(1)
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        bridge_orders.reset_daily_action_count()
        self.assertEqual(bridge_orders.daily_action_count(), 0)
        response = bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        self.assertTrue(response["ok"], "复位之后应该能继续正常下单")

    def test_a_tripped_attempt_still_counts_towards_the_total(self):
        # 熔断之后的尝试本身也该被记一笔，不然 daily_action_count() 会一直
        # 停在上限那个数字，看不出「后面还有多少次被挡下来了」
        self._set_limit(1)
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        self.assertEqual(bridge_orders.daily_action_count(), 2)

    def test_the_count_rolls_over_on_a_new_day(self):
        self._set_limit(1)
        bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        self.assertEqual(bridge_orders.daily_action_count(), 1)
        with unittest.mock.patch("bridge.orders._datetime") as fake_dt:
            fake_dt.date.today.return_value = datetime.date.today() + datetime.timedelta(days=1)
            self.assertEqual(bridge_orders.daily_action_count(), 0)
            response = bridge_orders.place_order(ACCOUNT, "600000.SH", "buy", 100)
        self.assertTrue(response["ok"], "新的一天应该重新计数，不该继续算昨天的额度")


if __name__ == "__main__":
    unittest.main()
