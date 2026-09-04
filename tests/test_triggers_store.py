"""条件单存储层（triggers/store.py）。"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module   # noqa: F401  触发 dbaccess.register()，store 才能连上库
from bridge import market as bridge_market
from triggers import store


class CreateValidationTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()

    def tearDown(self):
        store.reset_for_tests()

    def test_unknown_trigger_type_is_rejected(self):
        with self.assertRaises(ValueError):
            store.create("acc1", "600000.SH", "moon_landing", 9.0, volume=100)

    def test_non_positive_trigger_price_is_rejected(self):
        for price in (0, -1):
            with self.assertRaises(ValueError):
                store.create("acc1", "600000.SH", "stop_loss", price, volume=100)

    def test_needs_either_volume_or_percentage(self):
        with self.assertRaises(ValueError):
            store.create("acc1", "600000.SH", "stop_loss", 8.5)

    def test_percentage_is_rejected_on_a_buy_trigger(self):
        # 买入没有「持仓百分比」这回事
        with self.assertRaises(ValueError):
            store.create("acc1", "600000.SH", "buy_dip", 8.5, percentage=50)

    def test_percentage_out_of_range_is_rejected(self):
        for pct in (0, 101, -10):
            with self.assertRaises(ValueError):
                store.create("acc1", "600000.SH", "stop_loss", 8.5, percentage=pct)

    def test_order_role_price_types_are_rejected(self):
        # 限价类需要单独的委托价，条件单目前只存触发价，两者不是一回事
        with self.assertRaises(ValueError):
            store.create("acc1", "600000.SH", "stop_loss", 8.5, volume=100, price_type="fix")

    def test_market_role_price_types_are_accepted(self):
        for pt in ("latest", "peer", "mine", "stop"):
            order_id = store.create("acc1", "600000.SH", "stop_loss", 8.5,
                                    volume=100, price_type=pt)
            self.assertTrue(order_id)

    def test_a_valid_stop_loss_resolves_side_and_compare(self):
        order_id = store.create("acc1", "600000.SH", "stop_loss", 8.5, volume=500)
        row = store.get(order_id)
        self.assertEqual(row["side"], "sell")
        self.assertEqual(row["compare"], "lte")
        self.assertEqual(row["status"], store.STATUS_ACTIVE)

    def test_a_valid_take_profit_resolves_side_and_compare(self):
        order_id = store.create("acc1", "600000.SH", "take_profit", 9.5, percentage=50)
        row = store.get(order_id)
        self.assertEqual(row["side"], "sell")
        self.assertEqual(row["compare"], "gte")

    def test_buy_triggers_resolve_side_and_compare(self):
        dip = store.get(store.create("acc1", "600000.SH", "buy_dip", 8.0, volume=100))
        breakout = store.get(store.create("acc1", "600000.SH", "buy_breakout", 10.0, volume=100))
        self.assertEqual((dip["side"], dip["compare"]), ("buy", "lte"))
        self.assertEqual((breakout["side"], breakout["compare"]), ("buy", "gte"))


class QueryAndLifecycleTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()
        self.id_a = store.create("acc1", "600000.SH", "stop_loss", 8.5, volume=100)
        self.id_b = store.create("acc1", "123281.SZ", "take_profit", 160.0, percentage=100)
        self.id_c = store.create("acc2", "600000.SH", "stop_loss", 8.0, volume=200)

    def tearDown(self):
        store.reset_for_tests()

    def test_list_active_scopes_by_account(self):
        rows = store.list_active("acc1")
        self.assertEqual({r["id"] for r in rows}, {self.id_a, self.id_b})

    def test_list_active_all_accounts(self):
        rows = store.list_active()
        self.assertEqual({r["id"] for r in rows}, {self.id_a, self.id_b, self.id_c})

    def test_cancel_removes_it_from_the_active_list(self):
        self.assertTrue(store.cancel(self.id_a))
        self.assertNotIn(self.id_a, {r["id"] for r in store.list_active()})
        self.assertEqual(store.get(self.id_a)["status"], store.STATUS_CANCELLED)

    def test_cancel_is_idempotent_the_second_time_reports_nothing_to_do(self):
        store.cancel(self.id_a)
        self.assertFalse(store.cancel(self.id_a))

    def test_cancel_with_the_wrong_account_id_does_nothing(self):
        self.assertFalse(store.cancel(self.id_a, account_id="not-the-owner"))
        self.assertEqual(store.get(self.id_a)["status"], store.STATUS_ACTIVE)

    def test_mark_triggered_moves_it_out_of_active_and_records_the_order(self):
        store.mark_triggered(self.id_a, "SYS-123", "已下单")
        row = store.get(self.id_a)
        self.assertEqual(row["status"], store.STATUS_TRIGGERED)
        self.assertEqual(row["order_sys_id"], "SYS-123")
        self.assertNotIn(self.id_a, {r["id"] for r in store.list_active()})

    def test_mark_failed_moves_it_out_of_active(self):
        store.mark_failed(self.id_a, "无可用持仓")
        row = store.get(self.id_a)
        self.assertEqual(row["status"], store.STATUS_FAILED)
        self.assertEqual(row["message"], "无可用持仓")

    def test_record_retry_failure_keeps_it_active(self):
        store.record_retry_failure(self.id_a, "触发风控：停止卖出")
        row = store.get(self.id_a)
        self.assertEqual(row["status"], store.STATUS_ACTIVE)
        self.assertEqual(row["message"], "触发风控：停止卖出")
        self.assertIn(self.id_a, {r["id"] for r in store.list_active()})

    def test_record_retry_failure_only_touches_the_timestamp_when_notified(self):
        store.record_retry_failure(self.id_a, "第一次", notified=True)
        first_ts = store.get(self.id_a)["last_notified_at"]
        self.assertIsNotNone(first_ts)
        store.record_retry_failure(self.id_a, "第二次", notified=False)
        self.assertEqual(store.get(self.id_a)["last_notified_at"], first_ts)
        self.assertEqual(store.get(self.id_a)["message"], "第二次")

    def test_list_all_includes_inactive_ones(self):
        store.cancel(self.id_a)
        rows = store.list_all("acc1")
        self.assertEqual({r["id"] for r in rows}, {self.id_a, self.id_b})


class ClaimForFiringTests(unittest.TestCase):
    """下单前的原子占位，防止同一条条件单被真的下出两笔单。"""

    def setUp(self):
        store.reset_for_tests()
        self.order_id = store.create("acc1", "600000.SH", "stop_loss", 8.5, volume=100)

    def tearDown(self):
        store.reset_for_tests()

    def test_claiming_an_active_order_succeeds_and_moves_it_out_of_active(self):
        self.assertTrue(store.claim_for_firing(self.order_id))
        self.assertEqual(store.get(self.order_id)["status"], store.STATUS_SUBMITTING)
        self.assertNotIn(self.order_id, {r["id"] for r in store.list_active()})

    def test_claiming_twice_only_succeeds_once(self):
        self.assertTrue(store.claim_for_firing(self.order_id))
        self.assertFalse(store.claim_for_firing(self.order_id),
                         "已经在 submitting 了，第二次抢占不该成功——这正是要防的重复下单")

    def test_claiming_a_non_active_order_fails(self):
        store.cancel(self.order_id)
        self.assertFalse(store.claim_for_firing(self.order_id))

    def test_release_claim_puts_it_back_to_active(self):
        store.claim_for_firing(self.order_id)
        store.release_claim(self.order_id)
        row = store.get(self.order_id)
        self.assertEqual(row["status"], store.STATUS_ACTIVE)
        self.assertIn(self.order_id, {r["id"] for r in store.list_active()})

    def test_release_claim_on_a_non_submitting_order_is_a_no_op(self):
        # 没在 submitting 状态时调用不该把别的状态（比如已经 triggered 的）
        # 错误地拉回 active
        store.mark_triggered(self.order_id, "SYS-1", "已下单")
        store.release_claim(self.order_id)
        self.assertEqual(store.get(self.order_id)["status"], store.STATUS_TRIGGERED)

    def test_mark_triggered_works_after_a_claim(self):
        store.claim_for_firing(self.order_id)
        store.mark_triggered(self.order_id, "SYS-1", "已下单")
        self.assertEqual(store.get(self.order_id)["status"], store.STATUS_TRIGGERED)


class DynamicTriggerTypeTests(unittest.TestCase):
    """涨停破板卖出 / 涨停买入：触发价是当天动态算的涨停价，不是用户填的数字。"""

    def setUp(self):
        store.reset_for_tests()

    def tearDown(self):
        store.reset_for_tests()

    def test_limit_up_break_does_not_need_a_trigger_price(self):
        order_id = store.create(
            "acc1", "600000.SH", "limit_up_break", volume=500)
        row = store.get(order_id)
        self.assertEqual(row["side"], "sell")
        self.assertEqual(row["compare"], "limit_break")
        self.assertEqual(row["trigger_price"], 0.0)

    def test_limit_up_buy_does_not_need_a_trigger_price(self):
        order_id = store.create("acc1", "600000.SH", "limit_up_buy", volume=500)
        row = store.get(order_id)
        self.assertEqual(row["side"], "buy")
        self.assertEqual(row["compare"], "limit_touch")

    def test_giving_a_trigger_price_for_a_dynamic_type_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            store.create("acc1", "600000.SH", "limit_up_break", trigger_price=9.9, volume=500)
        self.assertIn("动态算出", str(ctx.exception))

    def test_giving_a_trigger_pct_for_a_dynamic_type_is_rejected(self):
        with self.assertRaises(ValueError):
            store.create("acc1", "600000.SH", "limit_up_buy", trigger_pct=5, volume=500)

    def test_limit_up_break_still_honours_the_percentage_sell_rule(self):
        order_id = store.create(
            "acc1", "600000.SH", "limit_up_break", percentage=100)
        self.assertEqual(store.get(order_id)["percentage"], 100)


class PercentTriggerTests(unittest.TestCase):
    """涨跌幅 % 触发：创建时把百分比换算成绝对价格存下（相对昨收）。"""

    def setUp(self):
        store.reset_for_tests()
        self._patch = mock.patch.object(
            bridge_market, "price_reference",
            return_value={"code": "600000.SH", "last_price": 9.00,
                         "last_close": 10.00, "up_stop": 11.00, "down_stop": 9.00})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        store.reset_for_tests()

    def test_stop_loss_percent_resolves_below_last_close(self):
        # lte 类：跌 8% -> 10.00 * 0.92 = 9.20
        order_id = store.create("acc1", "600000.SH", "stop_loss", trigger_pct=8, volume=500)
        self.assertAlmostEqual(store.get(order_id)["trigger_price"], 9.20, places=3)

    def test_take_profit_percent_resolves_above_last_close(self):
        # gte 类：涨 5% -> 10.00 * 1.05 = 10.50
        order_id = store.create("acc1", "600000.SH", "take_profit", trigger_pct=5, volume=500)
        self.assertAlmostEqual(store.get(order_id)["trigger_price"], 10.50, places=3)

    def test_buy_dip_percent_resolves_below_last_close(self):
        order_id = store.create("acc1", "600000.SH", "buy_dip", trigger_pct=10, volume=500)
        self.assertAlmostEqual(store.get(order_id)["trigger_price"], 9.00, places=3)

    def test_negative_or_zero_pct_is_rejected(self):
        for pct in (0, -5):
            with self.assertRaises(ValueError):
                store.create("acc1", "600000.SH", "stop_loss", trigger_pct=pct, volume=500)

    def test_giving_both_price_and_pct_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            store.create("acc1", "600000.SH", "stop_loss",
                         trigger_price=9.0, trigger_pct=8, volume=500)
        self.assertIn("二选一", str(ctx.exception))

    def test_missing_last_close_is_rejected_rather_than_guessed(self):
        self._patch.stop()
        self._patch = mock.patch.object(
            bridge_market, "price_reference",
            return_value={"code": "600000.SH", "last_price": None,
                         "last_close": None, "up_stop": None, "down_stop": None})
        self._patch.start()
        with self.assertRaises(ValueError) as ctx:
            store.create("acc1", "600000.SH", "stop_loss", trigger_pct=8, volume=500)
        self.assertIn("昨收价", str(ctx.exception))

    def test_ordinary_trigger_price_still_works_without_pct(self):
        order_id = store.create("acc1", "600000.SH", "stop_loss", trigger_price=8.8, volume=500)
        self.assertAlmostEqual(store.get(order_id)["trigger_price"], 8.8, places=3)

    def test_neither_price_nor_pct_is_rejected(self):
        with self.assertRaises(ValueError):
            store.create("acc1", "600000.SH", "stop_loss", volume=500)


if __name__ == "__main__":
    unittest.main()
