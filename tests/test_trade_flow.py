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


class PartialFillTests(unittest.TestCase):
    """一笔委托分多笔成交时，每一笔都要留得住。

    GitHub issue #1：旧的 UNIQUE(order_id, order_sysid) 把同一委托的所有分笔
    成交当成同一行反复覆盖，只留下最后一笔。save_trades() 现在应该按
    (account_id, traded_id) 去重——那才是真正逐笔不同的字段。
    """

    account = "partial-fill-test"

    def setUp(self):
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        conn = app_module.get_db_connection()
        conn.execute("DELETE FROM trades WHERE account_id = ?", (self.account,))
        conn.commit()
        conn.close()

    def _fills(self):
        # 同一笔委托（同 order_id / order_sysid），分三笔成交，只有 traded_id 不同
        now = int(time.time())
        return [
            {"account_type": 2, "commission": 0.5, "direction": BUY,
             "instrument_name": "浦发银行", "offset_flag": None,
             "order_id": 101, "order_remark": "", "order_sysid": "SYS-101",
             "order_type": BUY, "secu_account": "", "stock_code": "600000.SH",
             "strategy_name": "demo", "traded_amount": 8950.0, "traded_id": tid,
             "traded_price": 8.95, "traded_time": now, "traded_volume": vol}
            for tid, vol in (("T-1", 300), ("T-2", 400), ("T-3", 300))
        ]

    def test_every_partial_fill_is_kept_not_just_the_last_one(self):
        app_module.save_trades(self.account, self._fills())
        conn = app_module.get_db_connection()
        rows = conn.execute(
            "SELECT traded_id, traded_volume FROM trades WHERE account_id = ? "
            "ORDER BY traded_id", (self.account,)).fetchall()
        conn.close()
        self.assertEqual([(r[0], r[1]) for r in rows],
                         [("T-1", 300), ("T-2", 400), ("T-3", 300)])

    def test_replaying_the_same_fill_updates_in_place_instead_of_duplicating(self):
        fills = self._fills()
        app_module.save_trades(self.account, fills)
        # 同一笔成交又被拉回来一次（轮询和回调都可能重复看到同一笔）
        app_module.save_trades(self.account, [fills[0]])
        conn = app_module.get_db_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE account_id = ?", (self.account,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 3, "重复拉到同一笔成交不该变成新行")

    def test_partial_fills_all_show_up_in_the_trade_flow(self):
        app_module.app.dependency_overrides[app_module.get_current_user] = admin_user
        app_module.app.dependency_overrides[app_module.get_user_or_viewer] = admin_user
        conn = app_module.get_db_connection()
        conn.execute("INSERT OR REPLACE INTO users (account_id, alias) VALUES (?, ?)",
                    (self.account, "分笔成交测试号"))
        conn.commit()
        conn.close()
        try:
            app_module.save_trades(self.account, self._fills())
            client = TestClient(app_module.app)
            response = client.get("/api/trade-flow?account_id=%s&days=1" % self.account)
            self.assertEqual(response.status_code, 200, response.text)
            volumes = sorted(r["volume"] for r in response.json()["records"])
            self.assertEqual(volumes, [300, 300, 400])
        finally:
            app_module.app.dependency_overrides.clear()
            conn = app_module.get_db_connection()
            conn.execute("DELETE FROM users WHERE account_id = ?", (self.account,))
            conn.commit()
            conn.close()

    def test_fills_with_no_traded_id_never_collide_with_each_other(self):
        # traded_id 取不到时是 None，不是 ""：SQLite 里 NULL 互不冲突，
        # 多笔“不知道编号”的成交不会互相顷掉。
        fills = self._fills()
        for f in fills:
            f["traded_id"] = None
        app_module.save_trades(self.account, fills)
        conn = app_module.get_db_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE account_id = ?", (self.account,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
