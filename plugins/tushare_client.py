"""Tushare 可选插件。

原来 app.py 顶部就是 `pro = ts.pro_api(TS_TOKEN)`：token 硬编码在源码里，而且
`import tushare` + `pro_api()` 在**导入期**就跑掉，导致 tests/ 里一句
`import app` 也要连外网。

这里改成惰性代理：`pro.daily(...)` 这样的调用点一个字都不用改，但只有真正
访问属性时才 import tushare、才建 client；没配 token 就抛 TushareUnavailable，
而 app.py 里所有 pro.* 调用点本来就包在 try/except 里，天然降级。
"""

import threading

import settings

TOKEN_ENV = "TUSHARE_TOKEN"

_STATE = {"built": False, "api": None, "error": ""}
_LOCK = threading.Lock()


class TushareUnavailable(RuntimeError):
    """没配 token 或 Tushare 初始化失败。调用方按“该数据源不可用”处理。"""


def get_token():
    """token 来源：环境变量 TUSHARE_TOKEN，其次 config/tushare.json 的 token 字段。"""
    return settings.env_str(TOKEN_ENV) or str(settings.load_json("tushare").get("token") or "")


def get_pro():
    """返回 tushare pro api；未配置或初始化失败返回 None。只初始化一次。"""
    with _LOCK:
        if _STATE["built"]:
            return _STATE["api"]
        _STATE["built"] = True
        token = get_token()
        if not token:
            _STATE["error"] = (
                "未配置 Tushare token（设环境变量 %s 或写 config/tushare.json），"
                "市值/涨跌幅/K线等字段将留空" % TOKEN_ENV
            )
            print(f"[Tushare] {_STATE['error']}")
            return None
        try:
            import tushare as ts   # 延迟到这里，import app 不再吃这个开销
            _STATE["api"] = ts.pro_api(token)
        except Exception as e:
            _STATE["error"] = "Tushare 初始化失败: %s" % e
            print(f"[Tushare] {_STATE['error']}")
            _STATE["api"] = None
        return _STATE["api"]


def is_available():
    return get_pro() is not None


def unavailable_reason():
    get_pro()
    return _STATE["error"]


class _LazyPro:
    """把属性访问转发给真实的 pro 对象，未配置时抛 TushareUnavailable。"""

    def __getattr__(self, name):
        api = get_pro()
        if api is None:
            raise TushareUnavailable(_STATE["error"] or "Tushare 不可用")
        return getattr(api, name)

    def __bool__(self):
        return is_available()

    def __repr__(self):
        return "<LazyTusharePro available=%s>" % is_available()


pro = _LazyPro()
