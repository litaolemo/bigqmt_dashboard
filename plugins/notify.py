"""关键事件推送到手机（可选插件）。

支持两个免费、不需要自建服务的渠道，缺配置就整体降级成打日志：

    企业微信群机器人  WECOM_WEBHOOK_URL 或 config/notify.json 的 wecom_webhook
    Server 酱          SERVERCHAN_KEY    或 config/notify.json 的 serverchan_key

两个都配了就都发一遍；发送失败只打日志，绝不能因为一条通知发不出去就把
成交回报或触发引擎的调用方搞挂——通知本身是锦上添花，不是关键路径。
"""

import threading

import requests

import settings

_TIMEOUT_SECONDS = 5
_LOCK = threading.Lock()
_CACHE = {"loaded": False, "wecom_webhook": "", "serverchan_key": ""}


def _config():
    with _LOCK:
        if _CACHE["loaded"]:
            return _CACHE
    file_cfg = settings.load_json("notify")
    wecom = settings.env_str("WECOM_WEBHOOK_URL") or str(file_cfg.get("wecom_webhook") or "")
    serverchan = settings.env_str("SERVERCHAN_KEY") or str(file_cfg.get("serverchan_key") or "")
    with _LOCK:
        _CACHE.update({"loaded": True, "wecom_webhook": wecom, "serverchan_key": serverchan})
    return _CACHE


def is_available():
    cfg = _config()
    return bool(cfg["wecom_webhook"] or cfg["serverchan_key"])


def _send_wecom(webhook, title, content):
    # 企业微信群机器人：markdown 消息类型支持简单的加粗/换行
    text = "**%s**\n%s" % (title, content)
    resp = requests.post(webhook, json={"msgtype": "markdown", "markdown": {"content": text}},
                         timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()


def _send_serverchan(sendkey, title, content):
    url = "https://sctapi.ftqq.com/%s.send" % sendkey
    resp = requests.post(url, data={"title": title, "desp": content}, timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()


def notify(title, content=""):
    """发一条通知。没配任何渠道时只打日志，不算错误。"""
    cfg = _config()
    if not (cfg["wecom_webhook"] or cfg["serverchan_key"]):
        print(f"[通知] （未配置推送渠道，仅记录）{title}: {content}")
        return False
    ok = False
    if cfg["wecom_webhook"]:
        try:
            _send_wecom(cfg["wecom_webhook"], title, content)
            ok = True
        except Exception as e:
            print(f"[通知] 企业微信发送失败: {e}")
    if cfg["serverchan_key"]:
        try:
            _send_serverchan(cfg["serverchan_key"], title, content)
            ok = True
        except Exception as e:
            print(f"[通知] Server酱发送失败: {e}")
    return ok


def reset_cache():
    """测试用。"""
    with _LOCK:
        _CACHE.update({"loaded": False, "wecom_webhook": "", "serverchan_key": ""})
