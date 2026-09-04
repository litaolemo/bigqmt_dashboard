"""全局熔断计数在 /api/accounts/health 上的展示 + 管理员复位接口。"""

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
from bridge import orders as bridge_orders


def admin_user():
    return {"username": "admin", "role": "admin", "account_id": "admin"}


def ordinary_user():
    return {"username": "somebody", "role": "user", "account_id": "somebody"}


class DailyActionLimitApiTests(unittest.TestCase):
    def setUp(self):
        bridge_orders.reset_daily_action_count()
        self._saved_limit = bridge_orders.DAILY_ACTION_LIMIT
        app_module.app.dependency_overrides[app_module.get_current_admin] = admin_user
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self.client.close()
        app_module.app.dependency_overrides.clear()
        bridge_orders.DAILY_ACTION_LIMIT = self._saved_limit
        bridge_orders.reset_daily_action_count()

    def test_health_endpoint_reports_the_counter(self):
        body = self.client.get("/api/accounts/health").json()
        info = body["daily_action_limit"]
        self.assertEqual(info["count"], 0)
        self.assertEqual(info["limit"], bridge_orders.DAILY_ACTION_LIMIT)
        self.assertFalse(info["tripped"])

    def test_health_endpoint_reflects_a_tripped_state(self):
        bridge_orders.DAILY_ACTION_LIMIT = 1
        # 直接操纵内部计数，不需要真的打一遍下单链路
        bridge_orders._ACTION_GUARD_STATE["date"] = __import__("datetime").date.today()
        bridge_orders._ACTION_GUARD_STATE["count"] = 1
        body = self.client.get("/api/accounts/health").json()
        self.assertTrue(body["daily_action_limit"]["tripped"])

    def test_reset_endpoint_requires_admin(self):
        # 不用假的 dependency_override 掩盖真实检查：直接调真正的
        # get_current_admin，看它是不是真的会拒绝非管理员。
        app_module.app.dependency_overrides.pop(app_module.get_current_admin, None)
        app_module.app.dependency_overrides[app_module.get_current_user] = ordinary_user
        response = self.client.post("/api/admin/reset-daily-action-count")
        self.assertEqual(response.status_code, 403)

    def test_reset_endpoint_clears_the_counter(self):
        bridge_orders._ACTION_GUARD_STATE["date"] = __import__("datetime").date.today()
        bridge_orders._ACTION_GUARD_STATE["count"] = 500
        response = self.client.post("/api/admin/reset-daily-action-count")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(bridge_orders.daily_action_count(), 0)


if __name__ == "__main__":
    unittest.main()
