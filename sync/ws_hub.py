"""浏览器端的实时推送。

服务端已经从大QMT 秒级收到委托/成交回报（sync/callbacks.py），但浏览器一直是
按秒轮询接口才看得到——推送补的就是这最后一段。

发布方是同步/回调线程，不是 FastAPI 的事件循环线程，所以不能直接往
asyncio.Queue 里塞（那个不是线程安全的）。这里用标准库 queue.Queue：写侧
（publish）任何线程都能调，读侧（WebSocket 处理协程）用 asyncio.to_thread
包一层阻塞 get，两边都不用关心对方在哪个线程/循环上。

推送的消息只是「account_id 上发生了 event_type」，不带完整状态 diff ——
前端收到后直接重新拉一次对应接口，比在浏览器里维护一份增量合并逻辑更不容易错，
现有接口也都已经测过。WebSocket 只负责把「什么时候该拉」从几秒钟缩短到几乎实时。

ALL_ACCOUNTS 频道给汇总视图 / 管理员用：真实账号的事件会同时投给它自己的频道
和 ALL_ACCOUNTS，两边都在监听的连接只会收到一份（用 set 去重）。
"""

import queue
import threading

ALL_ACCOUNTS = "*"

_LOCK = threading.RLock()
_CONNECTIONS = {}   # account_id -> set(queue.Queue)


def subscribe(account_id):
    """开一个新连接的订阅。返回的队列要在断开时传给 unsubscribe。"""
    account_id = str(account_id or "").strip() or ALL_ACCOUNTS
    q = queue.Queue()
    with _LOCK:
        _CONNECTIONS.setdefault(account_id, set()).add(q)
    return account_id, q


def unsubscribe(account_id, q):
    with _LOCK:
        conns = _CONNECTIONS.get(account_id)
        if not conns:
            return
        conns.discard(q)
        if not conns:
            _CONNECTIONS.pop(account_id, None)


def publish(account_id, event_type, payload=None):
    """哪个线程调都行。account_id 频道 + ALL_ACCOUNTS 频道的订阅者都能收到。"""
    account_id = str(account_id or "").strip()
    message = {"type": event_type, "account_id": account_id, "data": payload}
    with _LOCK:
        targets = set(_CONNECTIONS.get(account_id, ())) | set(_CONNECTIONS.get(ALL_ACCOUNTS, ()))
    for q in targets:
        try:
            q.put_nowait(message)
        except Exception:
            pass


def connection_count():
    """诊断用：当前挂着多少条订阅。"""
    with _LOCK:
        return sum(len(v) for v in _CONNECTIONS.values())


def reset():
    """测试用。"""
    with _LOCK:
        _CONNECTIONS.clear()
