"""大QMT 推过来的实时委托/成交回报。

改造前成交要等 QMT 客户端下一轮 push 才看得到；现在大QMT 一有回报就经 Redis
频道推过来（xtquant_big_convert 的 exec_events），面板秒级更新。

注意：桥接层文档 XTQUANT_COMPAT_REPLACEMENT.md 里那句「RPC 暂不推送回调」是旧文，
代码里 BigQmtXtTrader.start() 会拉起事件监听线程，on_stock_order / on_stock_trade
是真的会触发的。
"""

import threading

from bridge import config as bridge_config
from bridge import pool as bridge_pool
from sync import adapters
from sync import sinks

_LISTENERS = {}     # account_id -> callback 实例
_LOCK = threading.RLock()
_STATS = {}         # account_id -> 计数


def _bump(account_id, key):
    with _LOCK:
        stat = _STATS.setdefault(account_id, {"orders": 0, "trades": 0, "errors": 0})
        stat[key] = stat.get(key, 0) + 1


def _make_callback_class():
    """在函数里建类：父类要从桥接层拿，而桥接层是惰性 import 的。"""
    compat = bridge_pool._compat()

    class DashboardCallback(compat.XtQuantTraderCallback):
        def __init__(self, account_id):
            self.account_id = account_id

        def on_stock_trade(self, trade):
            """成交回报：立刻落库 + 推给正在看这个账号的浏览器 / 手机通知。

            on_trade_event 只从这条实时路径触发，批量轮询（sync/poller.py）
            不触发它——否则每次轮询把同一笔历史成交重新拉回来一遍，都会当成
            「新成交」推一次，通知和浏览器推送都会重复刷屏。
            """
            _bump(self.account_id, "trades")
            try:
                row = adapters.trade_to_row(trade, self.account_id)
                sinks.call("save_trades", self.account_id, [row])
                sinks.call("on_trade_event", self.account_id, row)
            except Exception as e:
                print(f"[回报] 账号 {self.account_id} 处理成交失败: {e}")

        def on_stock_order(self, order):
            """委托状态变化：更新活动委托表，废单原因在 status_msg 里。"""
            _bump(self.account_id, "orders")
            try:
                row = adapters.order_to_row(order, self.account_id)
                sinks.call("save_orders", self.account_id, [row], partial=True)
                sinks.call("on_order_event", self.account_id, row)
            except Exception as e:
                print(f"[回报] 账号 {self.account_id} 处理委托失败: {e}")

        def on_order_error(self, order_error):
            _bump(self.account_id, "errors")
            message = getattr(order_error, "error_msg", "") or getattr(order_error, "error_id", "")
            print(f"[回报] 账号 {self.account_id} 委托被拒: {message}")
            sinks.call("on_order_event", self.account_id, {
                "account_id": self.account_id,
                "order_id": getattr(order_error, "order_id", None),
                "order_status": -1,
                "status_msg": str(message),
            })

        def on_cancel_error(self, cancel_error):
            _bump(self.account_id, "errors")
            message = getattr(cancel_error, "error_msg", "") or getattr(cancel_error, "error_id", "")
            print(f"[回报] 账号 {self.account_id} 撤单失败: {message}")

        def on_account_status(self, status):
            print(f"[回报] 账号 {self.account_id} 状态: "
                  f"{getattr(status, 'status', status)}")

    return DashboardCallback


def start(account_id):
    """给一个账号挂上实时回报监听。已挂过则跳过。"""
    account_id = str(account_id or "").strip()
    with _LOCK:
        if account_id in _LISTENERS:
            return True
    try:
        trader = bridge_pool.get_trader(account_id)
        callback = _make_callback_class()(account_id)
        trader.register_callback(callback)
        trader.start()      # 拉起 exec_events 监听线程
        trader.connect()
        trader.subscribe(bridge_pool.get_account_ref(account_id))
    except bridge_pool.BridgeUnavailable as e:
        print(f"[回报] 账号 {account_id} 无法挂载实时回报: {e}")
        return False
    except Exception as e:
        print(f"[回报] 账号 {account_id} 挂载实时回报失败: {type(e).__name__}: {e}")
        return False
    with _LOCK:
        _LISTENERS[account_id] = callback
    print(f"[回报] 账号 {account_id} 实时委托/成交回报已挂载")
    return True


def start_all():
    """给所有启用账号挂监听，返回成功的账号列表。"""
    ok = []
    for cfg in bridge_config.list_accounts():
        if start(cfg.account_id):
            ok.append(cfg.account_id)
    return ok


def stop_all():
    with _LOCK:
        account_ids = list(_LISTENERS)
        _LISTENERS.clear()
    for account_id in account_ids:
        try:
            bridge_pool.get_trader(account_id).stop()
        except Exception as e:
            print(f"[回报] 账号 {account_id} 停止监听出错: {e}")


def stats():
    with _LOCK:
        return {k: dict(v) for k, v in _STATS.items()}
