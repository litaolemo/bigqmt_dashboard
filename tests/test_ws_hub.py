"""浏览器端实时推送的发布/订阅中枢（sync/ws_hub.py）。

写侧从同步回调线程调用，读侧是 WebSocket 协程；这里只测发布/订阅/路由本身，
不测 WebSocket 握手（那部分在 test_ws_endpoint.py 里过 TestClient）。
"""

import queue
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sync import ws_hub


class WsHubTests(unittest.TestCase):
    def setUp(self):
        ws_hub.reset()

    def tearDown(self):
        ws_hub.reset()

    def test_a_subscriber_receives_a_publish_on_its_own_channel(self):
        account_id, q = ws_hub.subscribe("acc-1")
        ws_hub.publish("acc-1", "trade", {"volume": 100})
        message = q.get_nowait()
        self.assertEqual(message["type"], "trade")
        self.assertEqual(message["account_id"], "acc-1")
        self.assertEqual(message["data"], {"volume": 100})

    def test_a_subscriber_on_a_different_account_gets_nothing(self):
        _, q = ws_hub.subscribe("acc-1")
        ws_hub.publish("acc-2", "trade", {})
        with self.assertRaises(queue.Empty):
            q.get_nowait()

    def test_the_wildcard_channel_receives_every_account(self):
        _, all_q = ws_hub.subscribe("")   # 空字符串 -> ALL_ACCOUNTS
        ws_hub.publish("acc-1", "order", {"x": 1})
        ws_hub.publish("acc-2", "order", {"x": 2})
        seen = {all_q.get_nowait()["account_id"] for _ in range(2)}
        self.assertEqual(seen, {"acc-1", "acc-2"})

    def test_own_channel_and_wildcard_do_not_double_deliver(self):
        # 同一个连接不会因为同时挂在自己账号频道和全局频道就收到两份
        _, q = ws_hub.subscribe(ws_hub.ALL_ACCOUNTS)
        ws_hub.publish("acc-1", "order", {})
        self.assertEqual(q.qsize(), 1)

    def test_unsubscribe_stops_further_delivery(self):
        account_id, q = ws_hub.subscribe("acc-1")
        ws_hub.unsubscribe(account_id, q)
        ws_hub.publish("acc-1", "trade", {})
        with self.assertRaises(queue.Empty):
            q.get_nowait()

    def test_publishing_to_nobody_does_not_raise(self):
        ws_hub.publish("nobody-is-listening", "trade", {})

    def test_connection_count_reflects_active_subscriptions(self):
        self.assertEqual(ws_hub.connection_count(), 0)
        _, q1 = ws_hub.subscribe("acc-1")
        _, q2 = ws_hub.subscribe("acc-1")
        self.assertEqual(ws_hub.connection_count(), 2)
        ws_hub.unsubscribe("acc-1", q1)
        self.assertEqual(ws_hub.connection_count(), 1)
        ws_hub.unsubscribe("acc-1", q2)
        self.assertEqual(ws_hub.connection_count(), 0)


if __name__ == "__main__":
    unittest.main()
