"""条件单触发引擎（triggers/engine.py）。

行情和下单都是假的（FakeBridge + 打桩的 bridge_market），只验证引擎自己的判断：
该不该触发、触发时算出多少量、失败了要不要重试、要不要通知。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module   # noqa: F401  触发 dbaccess.register()
from bridge import market as bridge_market
from bridge import orders as bridge_orders
from sync import ws_hub
from tests.fake_bridge import FakeBridge, FakePosition
from triggers import engine, store


ACCOUNT = "trigger-engine-test"


def _tick(price):
    return {"lastPrice": price}


class TriggerEngineTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()
        ws_hub.reset()
        self.notify_calls = []
        self.available_volume = {}   # stock_code -> can_use_volume
        engine.register_hooks(
            get_available_volume=self._get_available_volume,
            notify=lambda title, content: self.notify_calls.append((title, content)))
        self.bridge = FakeBridge([ACCOUNT]).__enter__()

    def tearDown(self):
        self.bridge.__exit__(None, None, None)
        store.reset_for_tests()
        ws_hub.reset()
        engine.register_hooks(get_available_volume=None, notify=None)

    def _get_available_volume(self, account_id, stock_code):
        return {"can_use_volume": self.available_volume.get(stock_code, 0)}

    def _hold(self, stock_code, volume):
        self.available_volume[stock_code] = volume
        self.bridge.trader(ACCOUNT).positions[stock_code] = FakePosition(stock_code, volume)

    def _check(self, ticks, up_stops=None):
        details = {code: {"UpStopPrice": price} for code, price in (up_stops or {}).items()}
        with mock.patch.object(bridge_market, "available", return_value=True), \
             mock.patch.object(bridge_market, "get_ticks", return_value=ticks), \
             mock.patch.object(bridge_market, "get_instrument_detail",
                               side_effect=lambda code: details.get(code, {})):
            return engine.check_once()

    # ---- 不该查行情 ----

    def test_no_active_orders_skips_the_market_entirely(self):
        with mock.patch.object(bridge_market, "get_ticks") as get_ticks:
            active, fired = engine.check_once()
        self.assertEqual((active, fired), (0, 0))
        get_ticks.assert_not_called()

    def test_market_unavailable_fires_nothing(self):
        store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=100)
        with mock.patch.object(bridge_market, "available", return_value=False), \
             mock.patch.object(bridge_market, "get_ticks") as get_ticks:
            active, fired = engine.check_once()
        self.assertEqual(fired, 0)
        get_ticks.assert_not_called()

    # ---- 止损 / 止盈：卖出方向 ----

    def test_stop_loss_fires_when_price_drops_to_or_below_the_trigger(self):
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=500)
        active, fired = self._check({"600000.SH": _tick(8.5)})
        self.assertEqual((active, fired), (1, 1))
        self.assertEqual(self.bridge.orders[0]["order_type"], 24)   # 卖出
        self.assertEqual(self.bridge.orders[0]["order_volume"], 500)

    def test_stop_loss_does_not_fire_while_price_stays_above_the_trigger(self):
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=500)
        active, fired = self._check({"600000.SH": _tick(8.6)})
        self.assertEqual(fired, 0)
        self.assertEqual(self.bridge.orders, [])

    def test_take_profit_fires_when_price_rises_to_or_above_the_trigger(self):
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "take_profit", 9.5, volume=300)
        active, fired = self._check({"600000.SH": _tick(9.5)})
        self.assertEqual(fired, 1)
        self.assertEqual(self.bridge.orders[0]["order_type"], 24)

    def test_take_profit_does_not_fire_while_price_stays_below_the_trigger(self):
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "take_profit", 9.5, volume=300)
        active, fired = self._check({"600000.SH": _tick(9.49)})
        self.assertEqual(fired, 0)
        self.assertEqual(self.bridge.orders, [])

    def test_percentage_sell_resolves_against_current_available_volume(self):
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, percentage=50)
        self._check({"600000.SH": _tick(8.0)})
        self.assertEqual(self.bridge.orders[0]["order_volume"], 500)

    def test_hundred_percent_sells_the_full_available_including_odd_lots(self):
        self._hold("600000.SH", 1050)   # 不是整百
        store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, percentage=100)
        self._check({"600000.SH": _tick(8.0)})
        self.assertEqual(self.bridge.orders[0]["order_volume"], 1050)

    def test_zero_available_position_fails_the_order_without_ever_placing_one(self):
        self._hold("600000.SH", 0)
        order_id = store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=500)
        self._check({"600000.SH": _tick(8.0)})
        self.assertEqual(self.bridge.orders, [])
        row = store.get(order_id)
        self.assertEqual(row["status"], store.STATUS_FAILED)
        self.assertTrue(self.notify_calls, "仓位没了应该通知用户条件单失效了")

    # ---- 条件买入 ----

    def test_buy_dip_fires_exactly_at_the_trigger_price(self):
        # lte 是含等号的边界，8.0 本身就该触发，不是只有低于才算
        store.create(ACCOUNT, "600000.SH", "buy_dip", 8.0, volume=200)
        active, fired = self._check({"600000.SH": _tick(8.0)})
        self.assertEqual(fired, 1)
        self.assertEqual(self.bridge.orders[0]["order_type"], 23)   # 买入
        self.assertEqual(self.bridge.orders[0]["order_volume"], 200)

    def test_buy_dip_fires_when_price_drops_further_below_the_trigger(self):
        store.create(ACCOUNT, "600000.SH", "buy_dip", 8.0, volume=200)
        active, fired = self._check({"600000.SH": _tick(7.5)})
        self.assertEqual(fired, 1)

    def test_buy_dip_does_not_fire_while_price_stays_above_the_trigger(self):
        store.create(ACCOUNT, "600000.SH", "buy_dip", 8.0, volume=200)
        active, fired = self._check({"600000.SH": _tick(8.01)})
        self.assertEqual(fired, 0)
        self.assertEqual(self.bridge.orders, [])

    def test_buy_breakout_fires_exactly_at_the_trigger_price(self):
        store.create(ACCOUNT, "600000.SH", "buy_breakout", 10.0, volume=200)
        active, fired = self._check({"600000.SH": _tick(10.0)})
        self.assertEqual(fired, 1)
        self.assertEqual(self.bridge.orders[0]["order_type"], 23)

    def test_buy_breakout_fires_when_price_rises_further_above_the_trigger(self):
        store.create(ACCOUNT, "600000.SH", "buy_breakout", 10.0, volume=200)
        active, fired = self._check({"600000.SH": _tick(10.5)})
        self.assertEqual(fired, 1)

    def test_buy_breakout_does_not_fire_while_price_stays_below_the_trigger(self):
        store.create(ACCOUNT, "600000.SH", "buy_breakout", 10.0, volume=200)
        active, fired = self._check({"600000.SH": _tick(9.99)})
        self.assertEqual(fired, 0)
        self.assertEqual(self.bridge.orders, [])

    # ---- 触发后的状态流转 ----

    def test_a_successful_fire_marks_the_order_triggered_and_records_the_order_sys_id(self):
        self._hold("600000.SH", 1000)
        order_id = store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=500)
        self._check({"600000.SH": _tick(8.0)})
        row = store.get(order_id)
        self.assertEqual(row["status"], store.STATUS_TRIGGERED)
        self.assertTrue(row["order_sys_id"])
        self.assertNotIn(order_id, {r["id"] for r in store.list_active()})

    def test_a_successful_fire_notifies_and_publishes_to_the_ws_hub(self):
        self._hold("600000.SH", 1000)
        _, q = ws_hub.subscribe(ACCOUNT)
        store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=500)
        self._check({"600000.SH": _tick(8.0)})
        self.assertTrue(self.notify_calls)
        self.assertIn("止损触发", self.notify_calls[0][0])
        message = q.get_nowait()
        self.assertEqual(message["type"], "conditional_order")
        self.assertEqual(message["data"]["status"], "triggered")

    def test_a_rejected_order_stays_active_and_records_the_reason(self):
        # 停止卖出：风控闸门会拒单
        self._hold("600000.SH", 1000)
        order_id = store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=500)

        def rejecting_gate(_result):
            raise bridge_orders.OrderRejected("已停止卖出")

        with bridge_orders.temporary_risk_gate(rejecting_gate):
            self._check({"600000.SH": _tick(8.0)})

        row = store.get(order_id)
        self.assertEqual(row["status"], store.STATUS_ACTIVE, "条件仍成立，不该被消费掉")
        self.assertIn(order_id, {r["id"] for r in store.list_active()})
        self.assertTrue(self.notify_calls, "第一次失败应该通知")

    def test_repeated_failures_are_not_re_notified_within_the_cooldown(self):
        self._hold("600000.SH", 1000)
        order_id = store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=500)

        def rejecting_gate(_result):
            raise bridge_orders.OrderRejected("已停止卖出")

        with bridge_orders.temporary_risk_gate(rejecting_gate):
            self._check({"600000.SH": _tick(8.0)})
            first_count = len(self.notify_calls)
            self._check({"600000.SH": _tick(8.0)})
            second_count = len(self.notify_calls)

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 1, "冷却窗口内不该重复通知")
        self.assertEqual(store.get(order_id)["status"], store.STATUS_ACTIVE)

    # ---- 不能重复下单，哪怕落库那一步出了岔子 ----

    def test_a_crash_right_after_a_successful_submit_does_not_cause_a_second_order(self):
        # 模拟 place_order 成功之后，mark_triggered 落库失败（比如数据库瞬时
        # 抖了一下）。哪怕这一步炸了，条件单也不该还停在 active——不然下一轮
        # 检查条件仍然成立，会把同一个止损又真的下一遍单，等于替用户多卖了
        # 一次。claim_for_firing 的原子占位就是防这个的。
        self._hold("600000.SH", 1000)
        order_id = store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=500)

        # check_once() 本身会兜住每一行的异常只打日志（不能因为一条条件单
        # 处理出错就打断整轮检查），所以这里不需要也不该指望异常往外冒。
        with mock.patch.object(store, "mark_triggered", side_effect=RuntimeError("db 抖了一下")):
            self._check({"600000.SH": _tick(8.0)})

        self.assertEqual(len(self.bridge.orders), 1, "这一单本身应该正常报出去")
        row = store.get(order_id)
        self.assertNotEqual(row["status"], store.STATUS_ACTIVE,
                           "落库失败也不能让它退回 active，否则下一轮会重复下单")

        # 就算行情还在触发范围内，下一轮也不该再报一次
        self._check({"600000.SH": _tick(8.0)})
        self.assertEqual(len(self.bridge.orders), 1)

    def test_claim_failing_skips_this_round_without_placing_an_order(self):
        # claim_for_firing 抢占失败（防御性分支——正常时序下不会出现，
        # 但如果出现了，绝不能因此还去下单）
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=500)

        with mock.patch.object(store, "claim_for_firing", return_value=False):
            self._check({"600000.SH": _tick(8.0)})
        self.assertEqual(self.bridge.orders, [])


def _tick(price, high=None):
    row = {"lastPrice": price}
    if high is not None:
        row["high"] = high
    return row


class DynamicTriggerEngineTests(unittest.TestCase):
    """涨停破板卖出 / 涨停买入：触发价是当天动态算出来的涨停价，不是固定数字。"""

    def setUp(self):
        store.reset_for_tests()
        ws_hub.reset()
        self.notify_calls = []
        self.available_volume = {}
        engine.register_hooks(
            get_available_volume=self._get_available_volume,
            notify=lambda title, content: self.notify_calls.append((title, content)))
        self.bridge = FakeBridge([ACCOUNT]).__enter__()

    def tearDown(self):
        self.bridge.__exit__(None, None, None)
        store.reset_for_tests()
        ws_hub.reset()
        engine.register_hooks(get_available_volume=None, notify=None)

    def _get_available_volume(self, account_id, stock_code):
        return {"can_use_volume": self.available_volume.get(stock_code, 0)}

    def _hold(self, stock_code, volume):
        self.available_volume[stock_code] = volume
        self.bridge.trader(ACCOUNT).positions[stock_code] = FakePosition(stock_code, volume)

    def _check(self, ticks, up_stops):
        details = {code: {"UpStopPrice": price} for code, price in up_stops.items()}
        with mock.patch.object(bridge_market, "available", return_value=True), \
             mock.patch.object(bridge_market, "get_ticks", return_value=ticks), \
             mock.patch.object(bridge_market, "get_instrument_detail",
                               side_effect=lambda code: details.get(code, {})):
            return engine.check_once()

    # ---- 涨停破板卖出 ----

    def test_fires_when_the_day_high_touched_the_limit_and_price_has_dropped_back(self):
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "limit_up_break", volume=500)
        active, fired = self._check(
            {"600000.SH": _tick(price=10.80, high=11.00)}, {"600000.SH": 11.00})
        self.assertEqual((active, fired), (1, 1))
        self.assertEqual(self.bridge.orders[0]["order_type"], 24)   # 卖出

    def test_does_not_fire_if_the_day_high_never_reached_the_limit(self):
        # 从没封过板，现价比涨停价低是再正常不过的事，不是"破板"
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "limit_up_break", volume=500)
        active, fired = self._check(
            {"600000.SH": _tick(price=10.50, high=10.60)}, {"600000.SH": 11.00})
        self.assertEqual(fired, 0)
        self.assertEqual(self.bridge.orders, [])

    def test_does_not_fire_while_still_sitting_at_the_limit(self):
        # 封着板没开，还不该卖
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "limit_up_break", volume=500)
        active, fired = self._check(
            {"600000.SH": _tick(price=11.00, high=11.00)}, {"600000.SH": 11.00})
        self.assertEqual(fired, 0)

    def test_does_not_fire_when_the_limit_price_is_unavailable(self):
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "limit_up_break", volume=500)
        active, fired = self._check({"600000.SH": _tick(price=10.50, high=11.00)}, {})
        self.assertEqual(fired, 0, "取不到涨停价不能瞎判断")

    def test_a_successful_fire_mentions_the_limit_price_not_a_zero_trigger_price(self):
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "limit_up_break", volume=500)
        self._check({"600000.SH": _tick(price=10.80, high=11.00)}, {"600000.SH": 11.00})
        self.assertTrue(self.notify_calls)
        title, content = self.notify_calls[0]
        self.assertIn("涨停破板卖出", title)
        self.assertNotIn("0.000", content, "不该显示恒为 0 的 trigger_price")

    # ---- 涨停买入 ----

    def test_limit_up_buy_fires_when_price_reaches_the_limit(self):
        store.create(ACCOUNT, "600000.SH", "limit_up_buy", volume=200)
        active, fired = self._check({"600000.SH": _tick(price=11.00)}, {"600000.SH": 11.00})
        self.assertEqual(fired, 1)
        self.assertEqual(self.bridge.orders[0]["order_type"], 23)   # 买入
        self.assertEqual(self.bridge.orders[0]["order_volume"], 200)

    def test_limit_up_buy_does_not_fire_below_the_limit(self):
        store.create(ACCOUNT, "600000.SH", "limit_up_buy", volume=200)
        active, fired = self._check({"600000.SH": _tick(price=10.98)}, {"600000.SH": 11.00})
        self.assertEqual(fired, 0)
        self.assertEqual(self.bridge.orders, [])

    def test_limit_up_buy_does_not_fire_when_the_limit_price_is_unavailable(self):
        store.create(ACCOUNT, "600000.SH", "limit_up_buy", volume=200)
        active, fired = self._check({"600000.SH": _tick(price=11.00)}, {})
        self.assertEqual(fired, 0)

    # ---- 批量取涨停价：只查用得到的代码 ----

    def test_only_codes_with_a_dynamic_trigger_get_the_extra_lookup(self):
        self._hold("600000.SH", 1000)
        store.create(ACCOUNT, "600000.SH", "stop_loss", 8.5, volume=500)   # 普通类型
        store.create(ACCOUNT, "000001.SZ", "limit_up_buy", volume=200)     # 动态类型
        with mock.patch.object(bridge_market, "available", return_value=True), \
             mock.patch.object(bridge_market, "get_ticks",
                               return_value={"600000.SH": _tick(9.0),
                                            "000001.SZ": _tick(10.0)}), \
             mock.patch.object(bridge_market, "get_instrument_detail",
                               return_value={"UpStopPrice": 10.0}) as get_detail:
            engine.check_once()
        get_detail.assert_called_once_with("000001.SZ")


if __name__ == "__main__":
    unittest.main()
