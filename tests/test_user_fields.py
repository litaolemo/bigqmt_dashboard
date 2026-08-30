"""用户字段映射与敏感信息过滤。

发现经过：本地建了个 admin 账号后，/api/users/me 返回里 alias 是个时间戳、
created_at 是 null。追下去发现 users 表的实际列顺序是
    account_name, created_at, alias, clear_password, if_delete
而代码里写死的是
    account_name, alias, created_at, if_delete
整体错位一位——if_delete 字段里装的其实是 clear_password（明文存的清仓密码），
而 /api/users/me 只 pop 了 password，于是清仓密码被发给了浏览器。
"""

import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module


class UserColumnMappingTests(unittest.TestCase):
    def setUp(self):
        self.account_id = "field-test-" + uuid.uuid4().hex[:8]
        self.username = "fieldtest-" + uuid.uuid4().hex[:8]
        self.clear_password = "SECRET-CLEAR-PWD"
        conn = app_module.get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (account_id, username, password, role, account_name, "
            "alias, clear_password, if_delete) VALUES (?,?,?,'user',?,?,?,0)",
            (self.account_id, self.username, "hashed-login-pwd", "字段测试",
             "我的别名", self.clear_password))
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = app_module.get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE account_id = ?", (self.account_id,))
        conn.commit()
        conn.close()
        app_module.app.dependency_overrides.clear()

    def test_columns_map_to_the_right_names(self):
        user = app_module.get_user_by_account_id(self.account_id)
        self.assertEqual(user["alias"], "我的别名")
        self.assertEqual(user["account_name"], "字段测试")
        self.assertEqual(user["clear_password"], self.clear_password)
        # 关键：if_delete 是 0/1 的标记，不该是清仓密码
        self.assertEqual(user["if_delete"], 0)
        self.assertNotEqual(user["if_delete"], self.clear_password)

    def test_lookup_by_username_maps_the_same(self):
        user = app_module.get_user(self.username)
        self.assertEqual(user["alias"], "我的别名")
        self.assertEqual(user["if_delete"], 0)

    def test_public_user_strips_both_passwords(self):
        user = app_module.get_user_by_account_id(self.account_id)
        public = app_module.public_user(user)
        self.assertNotIn("password", public)
        self.assertNotIn("clear_password", public)
        self.assertNotIn(self.clear_password, str(public.values()))
        # 正常字段要留着
        self.assertEqual(public["alias"], "我的别名")
        self.assertEqual(public["account_id"], self.account_id)

    def test_users_me_never_returns_the_clearing_password(self):
        def as_me():
            return app_module.get_user_by_account_id(self.account_id)

        app_module.app.dependency_overrides[app_module.get_current_user] = as_me
        with TestClient(app_module.app) as client:
            response = client.get("/api/users/me")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn("password", body)
        self.assertNotIn("clear_password", body)
        # 清仓密码不能以任何字段名出现在响应里
        self.assertNotIn(self.clear_password, response.text)
        self.assertEqual(body["alias"], "我的别名")

    def test_mapping_follows_the_real_schema_not_a_hardcoded_list(self):
        # 直接对着 PRAGMA 校验：以后有人加列也不会再错位
        conn = app_module.get_db_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(users)")
        schema = [row[1] for row in cur.fetchall()]
        conn.close()
        user = app_module.get_user_by_account_id(self.account_id)
        self.assertEqual(sorted(user.keys()), sorted(schema))


if __name__ == "__main__":
    unittest.main()
