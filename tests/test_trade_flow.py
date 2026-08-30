"""买卖流水：账户真实成交，买卖同表。

取代原来的「历史买入列表」—— 那个展示的是 stock_market_data 的选股信号，
表里根本没有买卖方向字段，本质是选股不是成交。
"""

import sys
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module

ACCOUNT = "trade-flow-test"
BUY = 23
SELL = 24


def admin_user():
    return {"username": "admin", "role": "admin", "account_id": ACCOUNT}


def viewer_user():
    return {"username": "watcher", "role": "viewer", "is_viewer": True}


class TradeFlowTests(unittest.TestCase):
    def setUp(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        app_module.app.dependency_overrides[app_module.get_user_or_viewer] = admin_user
        self.client = TestClient(app_module.app)
        self._clean()
        self._seed()

    def tearDown(self):
        self.client.close()
        app_module.app.dependency_overrides.clear()
        self._clean()

    def _clean(self):
        conn = app_module.get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM trades WHERE account_id = ?", (ACCOUNT,))
        cur.execute("DELETE FROM users WHERE account_id = ?", (ACCOUNT,))
        conn.commit()
        conn.close()

    def _seed(self):
        now = int(time.time())
        rows = [
            # code, name, side, volume, price, 秒前
            ("600000.SH", "浦发银行", BUY, 10000, 8.95, 3600),
            ("123281.SZ", "中仑转债", BUY, 300, 154.20, 1800),
            ("688981.SH", "中芯国际", SELL, 500, 89.10, 900),
            ("510300.SH", "沪深300ETF", BUY, 20000, 4.07, 300),
            ("600000.SH", "浦发银行", SELL, 5000, 9.10, 100),
        ]
        conn = app_module.get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO users (account_id, alias) VALUES (?, ?)",
                    (ACCOUNT, "流水测试号"))
        for i, (code, name, side, volume, price, ago) in enumerate(rows):
            cur.execute(
                "INSERT INTO trades (account_id, stock_code, instrument_name, direction, "
                "order_type, traded_price, traded_volume, traded_amount, traded_time, "
                "strategy_name, order_id, order_sysid) "
                "VALUES (?,?,?,?,?,?,?,?,?,'demo',?,?)",
                (ACCOUNT, code, name, side, side, price, volume, round(price * volume, 2),
                 now - ago, "OID%d" % i, "SYS%d" % i))
        conn.commit()
        conn.close()

    def _flow(self, query=""):
        response = self.client.get("/api/trade-flow?account_id=%s&%s" % (ACCOUNT, query))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_buys_and_sells_come_back_in_one_list(self):
        body = self._flow("days=1")
        sides = [r["side"] for r in body["records"]]
        self.assertIn("buy", sides)
        self.assertIn("sell", sides)
        self.assertEqual(len(body["records"]), 5)

    def test_records_are_newest_first(self):
        records = self._flow("days=1")["records"]
        times = [r["traded_time"] for r in records]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_summary_splits_buy_and_sell_amounts(self):
        summary = self._flow("days=1")["summary"]
        self.assertEqual(summary["buy_count"], 3)
        self.assertEqual(summary["sell_count"], 2)
        # 买: 89500 + 46260 + 81400 = 217160
        self.assertAlmostEqual(summary["buy_amount"], 217160.0, places=2)
        # 卖: 44550 + 45500 = 90050
        self.assertAlmostEqual(summary["sell_amount"], 90050.0, places=2)
        self.assertAlmostEqual(summary["net_amount"], 127110.0, places=2)

    def test_side_filter(self):
        self.assertEqual(len(self._flow("days=1&side=buy")["records"]), 3)
        self.assertEqual(len(self._flow("days=1&side=sell")["records"]), 2)

    def test_keyword_filter_matches_code_and_name(self):
        self.assertEqual(len(self._flow("days=1&q=600000")["records"]), 2)
        self.assertEqual(len(self._flow("days=1&q=转债")["records"]), 1)

    def test_units_follow_the_instrument(self):
        by_code = {r["stock_code"]: r for r in self._flow("days=1")["records"]}
        self.assertEqual(by_code["123281.SZ"]["unit"], "张")
        self.assertTrue(by_code["123281.SZ"]["is_bond"])
        self.assertEqual(by_code["600000.SH"]["unit"], "股")
        self.assertFalse(by_code["600000.SH"]["is_bond"])
        self.assertEqual(by_code["510300.SH"]["unit"], "份")

    def test_account_alias_is_resolved(self):
        self.assertEqual(self._flow("days=1")["records"][0]["account_alias"], "流水测试号")

    def test_day_window_excludes_older_trades(self):
        # 把一笔挪到 10 天前，days=1 就不该再看到它
        conn = app_module.get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE trades SET traded_time = ? WHERE account_id = ? AND order_id = 'OID0'",
                    (int(time.time()) - 10 * 86400, ACCOUNT))
        conn.commit()
        conn.close()
        self.assertEqual(len(self._flow("days=1")["records"]), 4)
        self.assertEqual(len(self._flow("days=30")["records"]), 5)

    def test_ordinary_user_cannot_read_another_account(self):
        def other_user():
            return {"username": "someone", "role": "user", "account_id": "someone-else"}

        app_module.app.dependency_overrides[app_module.get_user_or_viewer] = other_user
        response = self.client.get("/api/trade-flow?account_id=%s&days=1" % ACCOUNT)
        self.assertEqual(response.status_code, 200)
        # 请求里写了别人的账号，服务端会强制改回自己的 -> 查不到东西
        self.assertEqual(response.json()["records"], [])

    def test_viewer_sees_the_flow_with_volume_and_amount(self):
        app_module.app.dependency_overrides[app_module.get_user_or_viewer] = viewer_user
        response = self.client.get("/api/trade-flow?account_id=%s&days=1" % ACCOUNT)
        self.assertEqual(response.status_code, 200, response.text)
        records = response.json()["records"]
        self.assertTrue(records)
        self.assertTrue(all(r["volume"] > 0 and r["amount"] > 0 for r in records))


if __name__ == "__main__":
    unittest.main()
