"""落库回调的注册表。

poller（定时拉）和 callbacks（实时推）写的是同一批表，所以共用一份 sink 注册表。
实现由 app.py 注入 —— app 会 import sync，sync 反过来 import app 就成环了。
"""

import threading

_SINKS = {
    "save_positions": None,
    "save_asset": None,
    "save_trades": None,
    "save_orders": None,
    "is_trading_session": None,
    "on_order_event": None,     # 委托状态变化（含废单原因）
    "on_trade_event": None,     # 实时成交回报（只有回调路径会触发，批量轮询不触发）
}

_LOCK = threading.RLock()


def register(**sinks):
    """注册落库回调；未注册的项调用方直接跳过，不报错。"""
    with _LOCK:
        for key, fn in sinks.items():
            if key not in _SINKS:
                raise KeyError("未知的 sink: %s（可用: %s）" % (key, ", ".join(sorted(_SINKS))))
            _SINKS[key] = fn


def get(name):
    with _LOCK:
        return _SINKS.get(name)


def call(name, *args, **kwargs):
    """调用一个 sink；没注册就返回 None，抛错只打日志不外传。

    同步链路上的任何一个写库失败，都不该把整个轮询线程或行情回调线程搞死。
    """
    fn = get(name)
    if fn is None:
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"[同步] sink {name} 执行失败: {type(e).__name__}: {e}")
        return None


def reset():
    """测试用。"""
    with _LOCK:
        for key in _SINKS:
            _SINKS[key] = None
