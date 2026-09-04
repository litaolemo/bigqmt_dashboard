"""关键事件推送插件（plugins/notify.py）。

两个免费渠道，缺配置整体降级成打日志；配了的话失败只打日志，不能把调用方
（成交回报回调、触发引擎）搞挂——通知是锦上添花，不是关键路径。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings
from plugins import notify


class NotifyTests(unittest.TestCase):
    def setUp(self):
        notify.reset_cache()
        settings.reset_cache("notify")
        self._saved_env = {
            k: __import__("os").environ.pop(k, None)
            for k in ("WECOM_WEBHOOK_URL", "SERVERCHAN_KEY")
        }

    def tearDown(self):
        import os
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        notify.reset_cache()
        settings.reset_cache("notify")

    def test_unavailable_when_nothing_configured(self):
        self.assertFalse(notify.is_available())

    def test_notify_without_any_channel_does_not_raise(self):
        # 没配置也不能抛错——调用方（成交回调）不该因为这个挂掉
        self.assertFalse(notify.notify("标题", "内容"))

    def test_env_var_takes_precedence_over_file(self):
        import os
        os.environ["WECOM_WEBHOOK_URL"] = "https://example.com/env-hook"
        with mock.patch.object(settings, "load_json",
                               return_value={"wecom_webhook": "https://example.com/file-hook"}):
            with mock.patch("plugins.notify.requests.post") as post:
                post.return_value.raise_for_status.return_value = None
                notify.notify("t", "c")
                self.assertEqual(post.call_args.args[0], "https://example.com/env-hook")

    def test_available_when_either_channel_is_configured(self):
        import os
        os.environ["SERVERCHAN_KEY"] = "SCT123"
        self.assertTrue(notify.is_available())

    def test_wecom_sends_a_markdown_message(self):
        import os
        os.environ["WECOM_WEBHOOK_URL"] = "https://qyapi.weixin.qq.com/hook/xyz"
        with mock.patch("plugins.notify.requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            ok = notify.notify("止损触发", "600000.SH 已卖出 500 股")
            self.assertTrue(ok)
            url, kwargs = post.call_args.args[0], post.call_args.kwargs
            self.assertEqual(url, "https://qyapi.weixin.qq.com/hook/xyz")
            self.assertEqual(kwargs["json"]["msgtype"], "markdown")
            self.assertIn("止损触发", kwargs["json"]["markdown"]["content"])

    def test_serverchan_posts_title_and_content(self):
        import os
        os.environ["SERVERCHAN_KEY"] = "SCT999"
        with mock.patch("plugins.notify.requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            notify.notify("标题", "正文")
            self.assertEqual(post.call_args.args[0], "https://sctapi.ftqq.com/SCT999.send")
            self.assertEqual(post.call_args.kwargs["data"], {"title": "标题", "desp": "正文"})

    def test_both_channels_configured_sends_to_both(self):
        import os
        os.environ["WECOM_WEBHOOK_URL"] = "https://example.com/hook"
        os.environ["SERVERCHAN_KEY"] = "SCT1"
        with mock.patch("plugins.notify.requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            notify.notify("t", "c")
            self.assertEqual(post.call_count, 2)

    def test_a_channel_failure_does_not_raise_and_does_not_block_the_other(self):
        import os
        os.environ["WECOM_WEBHOOK_URL"] = "https://example.com/hook"
        os.environ["SERVERCHAN_KEY"] = "SCT1"
        with mock.patch("plugins.notify.requests.post") as post:
            def side_effect(url, **kwargs):
                if "example.com" in url:
                    raise ConnectionError("boom")
                resp = mock.Mock()
                resp.raise_for_status.return_value = None
                return resp
            post.side_effect = side_effect
            ok = notify.notify("t", "c")
            self.assertTrue(ok, "一个渠道失败，另一个成功也算发出去了")


if __name__ == "__main__":
    unittest.main()
