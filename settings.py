"""统一配置读取：环境变量优先，其次 config/ 下被 gitignore 的 JSON 文件。

改造前每个模块各自拼路径、各写一遍缓存（get_mysql_sync_config、get_eastmoney_proxies
都是同一套代码抄两遍）。这里收成一处，新增的 bridge / cb / plugins 全从这里取配置。

约定：缺配置一律返回空值交给调用方降级，导入期绝不抛错、绝不联网。
"""

import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")

_JSON_CACHE = {}
_JSON_LOCK = threading.Lock()


def config_path(name):
    """config/<name>.json 的绝对路径。"""
    return os.path.join(CONFIG_DIR, "%s.json" % name)


def load_json(name, default=None):
    """读 config/<name>.json，文件缺失或损坏都返回 default（默认空 dict）。

    结果按 name 缓存；改了配置文件需要重启服务，与原来 mysql_sync_config.json
    的行为一致。
    """
    if default is None:
        default = {}
    with _JSON_LOCK:
        if name in _JSON_CACHE:
            return _JSON_CACHE[name]
    value = default
    path = config_path(name)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, (dict, list)):
                value = loaded
    except Exception as e:
        print(f"[配置] 读取 {path} 失败，按缺省降级: {e}")
    with _JSON_LOCK:
        _JSON_CACHE[name] = value
    return value


def reset_cache(name=None):
    """清掉配置缓存，测试用。"""
    with _JSON_LOCK:
        if name is None:
            _JSON_CACHE.clear()
        else:
            _JSON_CACHE.pop(name, None)


def env_str(name, default=""):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_int(name, default=0):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError, TypeError):
        return default


def env_float(name, default=0.0):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError, TypeError):
        return default


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")
