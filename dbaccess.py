"""dashboard SQLite 的连接入口。

bridge / sync / cb 这些新模块都要读写 dashboard.db，但 app.py 会 import 它们，
反过来 import app 就成环。所以由 app.py 在启动时把自己的 get_db_connection 注册
进来，其它模块统一从这里取。

注册的是函数而不是路径，所以测试里换掉 app.DB_PATH 依然生效。
"""

import threading

_FACTORY = {"fn": None}
_LOCK = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    """还没注册连接工厂就用了数据库。属于装配错误，不该被吞掉。"""


def register(fn):
    with _LOCK:
        _FACTORY["fn"] = fn


def connect():
    with _LOCK:
        fn = _FACTORY["fn"]
    if fn is None:
        raise DatabaseUnavailable("尚未注册数据库连接工厂（app.py 启动时会注册）")
    return fn()


def is_ready():
    with _LOCK:
        return _FACTORY["fn"] is not None
