"""国债逆回购开关（/api/account/reverse-repo, /api/account/trading-status）。"""

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module

ACCOUNT = "reverse-repo-api-test"


def admin_user():
    return {"username": "admin", "role": "admin", "account_id": "admin"}


def owner_user():
    return {"username": ACCOUNT, "role": "user", "account_id": ACCOUNT}


def other_user():
    return {"username": "someone-else", "role": "user", "account_id": "someone-else"}


class ReverseRepoApiTests(unittest.TestCase):
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
        conn.execute("DELETE FROM trading_status WHERE account_id = ?", (ACCOUNT,))
        conn.commit()
        conn.close()

    def _status(self):
        response = self.client.get("/api/account/trading-status?account_id=%s" % ACCOUNT)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_defaults_to_disabled_with_no_row(self):
        self.assertFalse(self._status()["reverse_repo_enabled"])

    def test_turning_it_on_persists(self):
        response = self.client.post("/api/account/reverse-repo",
                                    json={"account_id": ACCOUNT, "command_type": "reverse_repo", "command_data": "on"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["reverse_repo_enabled"])
        self.assertTrue(self._status()["reverse_repo_enabled"])

    def test_turning_it_off_again_persists(self):
        self.client.post("/api/account/reverse-repo",
                         json={"account_id": ACCOUNT, "command_type": "reverse_repo", "command_data": "on"})
        response = self.client.post("/api/account/reverse-repo",
                                    json={"account_id": ACCOUNT, "command_type": "reverse_repo", "command_data": "off"})
        self.assertFalse(response.json()["reverse_repo_enabled"])
        self.assertFalse(self._status()["reverse_repo_enabled"])

    def test_invalid_command_data_is_a_400(self):
        response = self.client.post("/api/account/reverse-repo",
                                    json={"account_id": ACCOUNT, "command_type": "reverse_repo", "command_data": "maybe"})
        self.assertEqual(response.status_code, 400)

    def test_toggling_does_not_disturb_other_trading_status_flags(self):
        self.client.post("/api/account/stop-trading",
                         json={"account_id": ACCOUNT, "command_type": "reverse_repo", "command_data": "stop_buy"})
        self.client.post("/api/account/reverse-repo",
                         json={"account_id": ACCOUNT, "command_type": "reverse_repo", "command_data": "on"})
        status = self._status()
        self.assertTrue(status["reverse_repo_enabled"])
        self.assertTrue(status["buy_stopped"], "开逆回购不该把已有的停止买入状态带没了")

    def test_owner_can_toggle_their_own_account(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = owner_user
        response = self.client.post("/api/account/reverse-repo",
                                    json={"account_id": ACCOUNT, "command_type": "reverse_repo", "command_data": "on"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_different_user_cannot_toggle_someone_elses_account(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = other_user
        response = self.client.post("/api/account/reverse-repo",
                                    json={"account_id": ACCOUNT, "command_type": "reverse_repo", "command_data": "on"})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
