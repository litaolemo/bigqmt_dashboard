"""/ws/updates：账号级实时推送的握手、鉴权、频道路由。"""

import os
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
from sync import ws_hub


ADMIN_USERNAME = "ws-endpoint-test-admin"


class WsEndpointTests(unittest.TestCase):
    def setUp(self):
        # websocket_connect() 会真的跑一遍 app 的 lifespan（跟普通 GET/POST 不
        # 一样，那些不触发）——不关掉后台任务的话，每个测试都会新起一整套轮询/
        # 行情线程，几个测试跑下来拖到一百多秒。这里跟 lifespan 里检查的是同一
        # 个环境变量。
        self._saved_skip = os.environ.get("QMT_DASHBOARD_SKIP_BACKGROUND_TASKS")
        os.environ["QMT_DASHBOARD_SKIP_BACKGROUND_TASKS"] = "1"
        ws_hub.reset()
        self.client = TestClient(app_module.app)
        # resolve_user_from_token 拿到 sub 之后要用它查 users 表，光签一个 JWT
        # 没用——之前这里直接拿 "admin" 当用户名，本地库里刚好手动建过一个真
        # admin 账号，测试才凑巧过；换一台干净的机器（比如导出仓库、CI）没有
        # 这行数据就全挂。自己建一行专用的，不依赖环境里恰好有什么。
        conn = app_module.get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO users (account_id, username, role) VALUES (?, ?, ?)",
            (ADMIN_USERNAME, ADMIN_USERNAME, "admin"))
        conn.commit()
        conn.close()
        self.admin_token = app_module.create_access_token(
            {"sub": ADMIN_USERNAME, "role": "admin"})
        self.viewer_token = app_module.create_access_token(
            {"sub": "watcher", "type": "viewer"})

    def tearDown(self):
        self.client.close()
        ws_hub.reset()
        conn = app_module.get_db_connection()
        conn.execute("DELETE FROM users WHERE account_id = ?", (ADMIN_USERNAME,))
        conn.commit()
        conn.close()
        if self._saved_skip is None:
            os.environ.pop("QMT_DASHBOARD_SKIP_BACKGROUND_TASKS", None)
        else:
            os.environ["QMT_DASHBOARD_SKIP_BACKGROUND_TASKS"] = self._saved_skip

    def test_no_token_is_rejected(self):
        with self.assertRaises(Exception):
            with self.client.websocket_connect("/ws/updates"):
                pass

    def test_a_viewer_token_is_rejected(self):
        # 观察者令牌不得访问任何账户接口，WebSocket 也不例外
        with self.assertRaises(Exception):
            with self.client.websocket_connect(
                    "/ws/updates?token=%s" % self.viewer_token):
                pass

    def test_admin_without_account_id_subscribes_to_everything(self):
        with self.client.websocket_connect(
                "/ws/updates?token=%s" % self.admin_token) as ws:
            ws_hub.publish("some-account", "trade", {"x": 1})
            message = ws.receive_json()
        self.assertEqual(message["type"], "trade")
        self.assertEqual(message["account_id"], "some-account")
        self.assertEqual(message["data"], {"x": 1})

    def test_admin_can_scope_to_one_account(self):
        with self.client.websocket_connect(
                "/ws/updates?token=%s&account_id=acc-1" % self.admin_token) as ws:
            ws_hub.publish("acc-2", "trade", {})   # 不该收到
            ws_hub.publish("acc-1", "order", {"y": 2})
            message = ws.receive_json()
        self.assertEqual(message["account_id"], "acc-1")

    def test_ordinary_user_is_forced_onto_their_own_account(self):
        conn = app_module.get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO users (account_id, username, role) VALUES (?, ?, ?)",
            ("ws-test-user", "ws-test-user", "user"))
        conn.commit()
        conn.close()
        token = app_module.create_access_token({"sub": "ws-test-user", "role": "user"})
        try:
            with self.client.websocket_connect(
                    "/ws/updates?token=%s&account_id=someone-elses-account" % token) as ws:
                ws_hub.publish("someone-elses-account", "trade", {})   # 不该收到
                ws_hub.publish("ws-test-user", "trade", {"z": 3})
                message = ws.receive_json()
            self.assertEqual(message["account_id"], "ws-test-user")
        finally:
            conn = app_module.get_db_connection()
            conn.execute("DELETE FROM users WHERE account_id = ?", ("ws-test-user",))
            conn.commit()
            conn.close()

    def test_disconnect_unsubscribes(self):
        with self.client.websocket_connect(
                "/ws/updates?token=%s&account_id=acc-1" % self.admin_token):
            self.assertEqual(ws_hub.connection_count(), 1)
        self.assertEqual(ws_hub.connection_count(), 0)


if __name__ == "__main__":
    unittest.main()
