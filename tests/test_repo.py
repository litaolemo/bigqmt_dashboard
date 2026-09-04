"""国债逆回购自动出借（bridge/repo.py）。"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import config as bridge_config
from bridge import orders as bridge_orders
from bridge import repo
from tests.fake_bridge import FakeBridge

ACCOUNT = "repo-test"


class ResolveAmountTests(unittest.TestCase):
    """金额换算公式：账户脚本里验证过的逻辑，原样照搬，这里只验证没抄错。"""

    def test_matches_the_original_formula(self):
        # int(int(cash * 0.001 * 10) / 100) * 100——数值是直接跑这个公式验证
        # 出来的（不是心算猜的），只是把它钉成回归测试。换算出来的量级比
        # cash 小两个数量级：10 万元现金只换算出 1000，这是公式本身的行为，
        # 照搬账户脚本，不是这里的 bug。
        self.assertEqual(repo.resolve_amount(100000), 1000)
        self.assertEqual(repo.resolve_amount(123456), 1200)
        self.assertEqual(repo.resolve_amount(999), 0)
        self.assertEqual(repo.resolve_amount(1000), 0)
        self.assertEqual(repo.resolve_amount(50000), 500)
        self.assertEqual(repo.resolve_amount(50999), 500)
        self.assertEqual(repo.resolve_amount(2000000), 20000)

    def test_the_practical_minimum_is_ten_thousand(self):
        # 1 万元以下换算出来恒为 0，MIN_CASH 常量描述的就是这条边界
        self.assertEqual(repo.resolve_amount(9999), 0)
        self.assertEqual(repo.resolve_amount(10000), 100)

    def test_zero_or_negative_is_zero(self):
        for cash in (0, -1, -100000):
            self.assertEqual(repo.resolve_amount(cash), 0)

    def test_non_numeric_is_zero_not_a_crash(self):
        for cash in (None, "abc", object()):
            self.assertEqual(repo.resolve_amount(cash), 0)


class BestCodeTests(unittest.TestCase):
    def test_picks_the_higher_rate(self):
        self.assertEqual(repo.best_code({repo.SH_CODE: 1.5, repo.SZ_CODE: 1.8}), repo.SZ_CODE)
        self.assertEqual(repo.best_code({repo.SH_CODE: 2.0, repo.SZ_CODE: 1.8}), repo.SH_CODE)

    def test_equal_rates_pick_shanghai(self):
        self.assertEqual(repo.best_code({repo.SH_CODE: 1.5, repo.SZ_CODE: 1.5}), repo.SH_CODE)

    def test_falls_back_to_whichever_side_has_a_rate(self):
        self.assertEqual(repo.best_code({repo.SH_CODE: 1.5, repo.SZ_CODE: None}), repo.SH_CODE)
        self.assertEqual(repo.best_code({repo.SH_CODE: None, repo.SZ_CODE: 1.5}), repo.SZ_CODE)

    def test_neither_available_returns_none(self):
        self.assertIsNone(repo.best_code({repo.SH_CODE: None, repo.SZ_CODE: None}))
        self.assertIsNone(repo.best_code({}))


class SubmitTests(unittest.TestCase):
    def setUp(self):
        bridge_orders.reset_daily_action_count()
        self._saved_limit = bridge_orders.DAILY_ACTION_LIMIT
        self.bridge = FakeBridge([ACCOUNT]).__enter__()

    def tearDown(self):
        self.bridge.__exit__(None, None, None)
        bridge_orders.DAILY_ACTION_LIMIT = self._saved_limit
        bridge_orders.reset_daily_action_count()

    def _set_rates(self, sh=None, sz=None):
        import bridge.market as bridge_market
        ticks = {}
        if sh is not None:
            ticks[repo.SH_CODE] = {"lastPrice": sh}
        if sz is not None:
            ticks[repo.SZ_CODE] = {"lastPrice": sz}
        self._patched_get_ticks = bridge_market.get_ticks
        bridge_market.get_ticks = lambda codes, timeout_seconds=None: ticks
        self.addCleanup(setattr, bridge_market, "get_ticks", self._patched_get_ticks)

    def test_account_without_allow_order_is_rejected_without_touching_the_exchange(self):
        self.bridge.allow_order_flag = False
        result = repo.submit(ACCOUNT)
        self.assertFalse(result["ok"])
        self.assertIn("未开启下单", result["message"])
        self.assertEqual(self.bridge.orders, [])

    def test_cash_below_the_minimum_is_not_a_failure_but_does_nothing(self):
        self.bridge.trader(ACCOUNT).cash = 500
        self._set_rates(sh=1.5, sz=1.8)
        result = repo.submit(ACCOUNT)
        self.assertTrue(result["ok"], "没什么可出借的不是错误")
        self.assertIn("不用出借", result["message"])
        self.assertEqual(self.bridge.orders, [])

    def test_missing_rates_on_both_sides_is_a_failure(self):
        self.bridge.trader(ACCOUNT).cash = 100000
        self._set_rates()   # 两边都没有
        result = repo.submit(ACCOUNT)
        self.assertFalse(result["ok"])
        self.assertIn("取不到逆回购行情", result["message"])
        self.assertEqual(self.bridge.orders, [])

    def test_submits_to_the_higher_rate_side_with_the_resolved_amount(self):
        self.bridge.trader(ACCOUNT).cash = 250000
        self._set_rates(sh=1.5, sz=1.85)
        result = repo.submit(ACCOUNT)
        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(result["code"], repo.SZ_CODE)
        self.assertEqual(result["amount"], 2500)
        self.assertEqual(result["rate"], 1.85)
        self.assertTrue(result["order_sys_id"])
        self.assertEqual(len(self.bridge.orders), 1)
        order = self.bridge.orders[0]
        self.assertEqual(order["stock_code"], repo.SZ_CODE)
        self.assertEqual(order["order_volume"], 2500)
        self.assertEqual(order["order_type"], 24)   # STOCK_SELL

    def test_a_bridge_side_rejection_is_reported_not_raised(self):
        self.bridge.trader(ACCOUNT).cash = 100000
        self._set_rates(sh=1.5, sz=1.8)
        self.bridge.trader(ACCOUNT).fail_with = "资金不足"
        result = repo.submit(ACCOUNT)
        self.assertFalse(result["ok"])
        self.assertIn("资金不足", result["message"])

    def test_the_global_circuit_breaker_still_applies(self):
        bridge_orders.DAILY_ACTION_LIMIT = 0
        self.bridge.trader(ACCOUNT).cash = 100000
        self._set_rates(sh=1.5, sz=1.8)
        result = repo.submit(ACCOUNT)
        self.assertFalse(result["ok"])
        self.assertIn("熔断", result["message"])
        self.assertEqual(self.bridge.orders, [], "熔断之后不该有真实报单发出去")

    def test_never_raises_on_an_unexpected_exception(self):
        self.bridge.trader(ACCOUNT).cash = 100000
        self._set_rates(sh=1.5, sz=1.8)

        def boom(*a, **kw):
            raise RuntimeError("意外故障")

        self.bridge.trader(ACCOUNT).order_stock_result = boom
        result = repo.submit(ACCOUNT)
        self.assertFalse(result["ok"])
        self.assertIn("意外故障", result["message"])


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        repo.reset_state()
        bridge_orders.reset_daily_action_count()
        self.bridge = FakeBridge(["acc-a", "acc-b"]).__enter__()
        # FakeBridge 把 list_accounts 打成空列表（那是给别的、不关心账号
        # 遍历的测试用的）；run_once() 恰恰需要遍历账号，这里另外接一份。
        self._saved_list_accounts = bridge_config.list_accounts
        bridge_config.list_accounts = lambda enabled_only=True: [
            SimpleNamespace(account_id="acc-a"), SimpleNamespace(account_id="acc-b")]
        for account_id in ("acc-a", "acc-b"):
            self.bridge.trader(account_id).cash = 100000

        import bridge.market as bridge_market
        self._saved_get_ticks = bridge_market.get_ticks
        bridge_market.get_ticks = lambda codes, timeout_seconds=None: {
            repo.SH_CODE: {"lastPrice": 1.5}, repo.SZ_CODE: {"lastPrice": 1.8}}

    def tearDown(self):
        import bridge.market as bridge_market
        bridge_market.get_ticks = self._saved_get_ticks
        bridge_config.list_accounts = self._saved_list_accounts
        self.bridge.__exit__(None, None, None)
        repo.reset_state()
        bridge_orders.reset_daily_action_count()

    def test_only_enabled_accounts_are_processed(self):
        results = repo.run_once(enabled_for=lambda aid: aid == "acc-a")
        self.assertEqual([r["account_id"] for r in results], ["acc-a"])

    def test_each_account_runs_at_most_once_per_day(self):
        repo.run_once(enabled_for=lambda aid: True)
        second = repo.run_once(enabled_for=lambda aid: True)
        self.assertEqual(second, [], "同一天不该再跑第二次")

    def test_force_bypasses_the_once_per_day_guard(self):
        repo.run_once(enabled_for=lambda aid: True)
        second = repo.run_once(enabled_for=lambda aid: True, force=True)
        self.assertEqual(len(second), 2)

    def test_enabled_for_raising_skips_that_account_without_crashing_the_rest(self):
        def flaky(account_id):
            if account_id == "acc-a":
                raise RuntimeError("配置读取失败")
            return True

        results = repo.run_once(enabled_for=flaky)
        self.assertEqual([r["account_id"] for r in results], ["acc-b"])


if __name__ == "__main__":
    unittest.main()
