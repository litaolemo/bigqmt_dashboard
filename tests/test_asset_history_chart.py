import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module


class AssetHistoryChartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = app_module.DB_PATH
        self.db_path = os.path.join(self.tmp.name, "dashboard.db")
        app_module.DB_PATH = self.db_path
        app_module.init_db()

    def tearDown(self):
        app_module.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def insert_user(self, account_id, is_dormant=0):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO users (account_id, username, is_dormant) VALUES (?, ?, ?)",
                (account_id, account_id, is_dormant),
            )
            conn.commit()
        finally:
            conn.close()

    def insert_asset_history(self, account_id, total_asset, market_value, cash, record_time):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO asset_history (account_id, total_asset, market_value, cash, record_time)
                VALUES (?, ?, ?, ?, ?)
                """,
                (account_id, total_asset, market_value, cash, record_time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
        finally:
            conn.close()

    def test_all_account_history_carries_latest_value_for_staggered_account_updates(self):
        self.insert_user("A")
        self.insert_user("B")
        base = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=30)
        base = base.replace(minute=(base.minute // 10) * 10)

        self.insert_asset_history("A", 100, 60, 40, base + timedelta(minutes=1))
        self.insert_asset_history("B", 200, 120, 80, base + timedelta(minutes=5))
        self.insert_asset_history("A", 110, 66, 44, base + timedelta(minutes=11))

        history = app_module.get_asset_history("all", hours=2)
        by_time = {row["record_time"]: row for row in history}

        first_bucket = base.strftime("%Y-%m-%d %H:%M")
        second_bucket = (base + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")

        self.assertEqual(by_time[first_bucket]["total_asset"], 300)
        self.assertEqual(by_time[second_bucket]["total_asset"], 310)
        self.assertEqual(by_time[second_bucket]["market_value"], 186)
        self.assertEqual(by_time[second_bucket]["cash"], 124)


if __name__ == "__main__":
    unittest.main()
