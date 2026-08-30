import sys
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module


def admin_user():
    return {"username": "admin", "role": "admin", "account_id": "admin"}


ACCOUNT_TABLES = (
    "positions",
    "trades",
    "assets",
    "asset_history",
    "daily_profits",
    "capital_adjustments",
    "position_locks",
    "t0_status",
    "trading_status",
    "orders",
    "order_audit",
    "strategy_configs",
    "users",
)


class DeleteAccountTests(unittest.TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex
        self.account_id = f"acct-delete-test-{suffix}"
        self.username = f"delete-user-test-{suffix}"
        self.cleanup_seed_data()
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self.client.close()
        app_module.app.dependency_overrides.clear()
        self.cleanup_seed_data()
        for cache in (
            app_module.account_last_sync,
            app_module.account_data_time,
            app_module._BACKFILL_KLINE_ISSUED,
            app_module._LAST_STRATEGY_INI_CONTENT,
        ):
            cache.pop(self.account_id, None)

    def execute(self, sql, params=()):
        conn = app_module.get_db_connection()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def cleanup_seed_data(self):
        conn = app_module.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_sessions WHERE username = ?", (self.username,))
            for table in ACCOUNT_TABLES:
                cursor.execute(f"DELETE FROM {table} WHERE account_id = ?", (self.account_id,))
            conn.commit()
        finally:
            conn.close()

    def count_rows(self, table, column, value):
        conn = app_module.get_db_connection()
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
                (value,),
            ).fetchone()[0]
        finally:
            conn.close()

    def seed_account_data(self):
        account_id = self.account_id
        username = self.username
        self.execute(
            """
            INSERT INTO users (account_id, username, password, role, account_name)
            VALUES (?, ?, 'hash', 'user', 'Delete Test')
            """,
            (account_id, username),
        )
        self.execute(
            "INSERT INTO user_sessions (username, token, expires_at) VALUES (?, ?, '2099-01-01')",
            (username, f"token-{username}"),
        )
        self.execute(
            "INSERT INTO positions (account_id, stock_code, instrument_name, update_time) VALUES (?, '000001.SZ', '平安银行', CURRENT_TIMESTAMP)",
            (account_id,),
        )
        self.execute(
            "INSERT INTO trades (account_id, order_id, order_sysid, stock_code, traded_time) VALUES (?, ?, ?, '000001.SZ', 1)",
            (account_id, 900000001, f"sys-{username}"),
        )
        self.execute("INSERT INTO assets (account_id, total_asset) VALUES (?, 1000)", (account_id,))
        self.execute("INSERT INTO asset_history (account_id, total_asset) VALUES (?, 1000)", (account_id,))
        self.execute("INSERT INTO daily_profits (account_id, date, daily_profit, profit_rate) VALUES (?, '2026-06-19', 1, 0.1)", (account_id,))
        self.execute("INSERT INTO capital_adjustments (account_id, amount) VALUES (?, 10)", (account_id,))
        self.execute("INSERT INTO position_locks (account_id, stock_code, is_locked) VALUES (?, '000001.SZ', 1)", (account_id,))
        self.execute("INSERT INTO t0_status (account_id, stock_code, enabled) VALUES (?, '000001.SZ', 1)", (account_id,))
        self.execute("INSERT INTO trading_status (account_id, is_stopped) VALUES (?, 1)", (account_id,))
        self.execute("INSERT INTO order_audit (account_id, stock_code, side, status) VALUES (?, '000001.SZ', 'buy', 'submitted')", (account_id,))
        self.execute("INSERT INTO orders (account_id, order_id, stock_code) VALUES (?, 'O1', '000001.SZ')", (account_id,))
        self.execute("INSERT INTO strategy_configs (account_id, config_content) VALUES (?, '[x]')", (account_id,))

        app_module.account_last_sync[account_id] = 1.0
        app_module.account_data_time[account_id] = "20260619 09:30:00"
        app_module._BACKFILL_KLINE_ISSUED[account_id] = "2026-06-19"
        app_module._LAST_STRATEGY_INI_CONTENT[account_id] = "[x]"

        return account_id, username

    def test_delete_account_removes_user_business_rows_and_runtime_state(self):
        account_id, username = self.seed_account_data()

        response = self.client.post("/api/admin/delete-account", json={"account_id": account_id})

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        for table in ACCOUNT_TABLES:
            self.assertEqual(0, self.count_rows(table, "account_id", account_id), table)
        self.assertEqual(0, self.count_rows("user_sessions", "username", username))
        for cache in (
            app_module.account_last_sync,
            app_module.account_data_time,
            app_module._BACKFILL_KLINE_ISSUED,
            app_module._LAST_STRATEGY_INI_CONTENT,
        ):
            self.assertNotIn(account_id, cache)

    def test_delete_account_rejects_current_user_and_all_account(self):
        for account_id in ("admin", "all"):
            response = self.client.post("/api/admin/delete-account", json={"account_id": account_id})
            self.assertEqual(400, response.status_code)


if __name__ == "__main__":
    unittest.main()