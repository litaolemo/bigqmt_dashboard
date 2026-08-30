"""东财访问用的可选代理。

东方财富的接口对机房 IP 很敏感，经常直接 RemoteDisconnected。改造前这段配置
（proxy_config.json）只给东财实时行情用；转债比价表走的是同一批接口，所以收进
plugins 里给两边共用。

不配置就直连，配置了就走隧道。缺配置一律降级，不报错。
"""

import contextlib
import os
import threading

import settings

_CACHE = {"loaded": False, "proxies": None}
_LOCK = threading.Lock()
_LEGACY_FILE = os.path.join(settings.BASE_DIR, "proxy_config.json")
_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


def get_proxies():
    """{'http': url, 'https': url}，未配置返回 None。"""
    with _LOCK:
        if _CACHE["loaded"]:
            return _CACHE["proxies"]
    config = settings.load_json("proxy")
    if not config and os.path.exists(_LEGACY_FILE):
        try:
            import json
            with open(_LEGACY_FILE, encoding="utf-8") as f:
                config = json.load(f) or {}
        except Exception as e:
            print(f"[代理] 读取旧版 proxy_config.json 失败: {e}")
            config = {}
    proxies = None
    if config.get("enabled") and config.get("url"):
        proxies = {"http": config["url"], "https": config["url"]}
    with _LOCK:
        _CACHE.update({"loaded": True, "proxies": proxies})
    return proxies


@contextlib.contextmanager
def env_proxies():
    """把代理临时写进环境变量。

    akshare 不接受 proxies 参数，只能借环境变量让它底下的 requests 走代理。
    退出时无条件还原，不管中间抛没抛异常。
    """
    proxies = get_proxies()
    if not proxies:
        yield False
        return
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = proxies["http"]
        os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = proxies["https"]
        yield True
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def reset_cache():
    with _LOCK:
        _CACHE.update({"loaded": False, "proxies": None})
