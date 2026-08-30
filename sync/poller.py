"""按账号主动拉取持仓/资产/委托/成交。

取代改造前的 POST /api/data —— 那时是 QMT 客户端往服务端推，服务端只能被动等；
客户端挂了要等超时才发现，数据新鲜度取决于客户端的轮询周期。

现在每个账号一个线程主动拉：交易时段 poll_seconds（默认 4s），非交易时段
idle_poll_seconds（默认 60s）。委托和成交用 query_execution_snapshot 一次 RPC
拿全，省一半往返。

落库函数由 app.py 注入（sync.sinks.register），避免 app ↔ sync 循环 import。
"""

import threading
import time

from bridge import config as bridge_config
from bridge import pool as bridge_pool
from sync import adapters
from sync import sinks

_THREADS = {}       # account_id -> Thread
_STATE = {}         # account_id -> 最近一次同步结果
_STOP = threading.Event()
_LOCK = threading.RLock()


def _in_trading_session():
    fn = sinks.get("is_trading_session")
    if fn is None:
        return True
    try:
        return bool(fn())
    except Exception:
        return True


def sync_once(account_id):
    """拉一轮并落库，返回本轮结果摘要。异常全部吞掉写进 error，线程不能死。"""
    started = time.time()
    state = {"account_id": account_id, "at": started, "ok": False,
             "positions": 0, "trades": 0, "orders": 0, "asset": False, "error": ""}
    try:
        trader = bridge_pool.get_trader(account_id)
        acc = bridge_pool.get_account_ref(account_id)

        positions = trader.query_stock_positions(acc) or []
        rows = [adapters.position_to_row(p, account_id) for p in positions]
        # 成交对象不带股票名，用本轮持仓快照补
        name_map = {r["stock_code"]: r["instrument_name"] for r in rows if r["instrument_name"]}
        sinks.call("save_positions", account_id, rows)
        state["positions"] = len(rows)

        asset = trader.query_stock_asset(acc)
        if asset is not None:
            sinks.call("save_asset", account_id, adapters.asset_to_row(asset, account_id))
            state["asset"] = True

        # 委托 + 成交一次 RPC 拿全
        snapshot = trader.query_execution_snapshot(acc) or {}
        orders = snapshot.get("orders") or []
        trades = snapshot.get("trades") or []

        if trades:
            sinks.call("save_trades", account_id, [
                adapters.trade_to_row(t, account_id, name_map.get) for t in trades
            ])
        state["trades"] = len(trades)

        sinks.call("save_orders", account_id, [
            adapters.order_to_row(o, account_id, name_map.get) for o in orders
        ])
        state["orders"] = len(orders)

        state["ok"] = True
    except bridge_pool.BridgeUnavailable as e:
        state["error"] = str(e)
    except Exception as e:
        state["error"] = "%s: %s" % (type(e).__name__, e)
    state["elapsed_ms"] = round((time.time() - started) * 1000, 1)
    with _LOCK:
        _STATE[account_id] = state
    return state


def _loop(cfg):
    """单账号轮询循环。"""
    account_id = cfg.account_id
    print(f"[同步] 账号 {account_id} 轮询启动 "
          f"(交易时段 {cfg.poll_seconds}s / 其余 {cfg.idle_poll_seconds}s)")
    consecutive_errors = 0
    while not _STOP.is_set():
        state = sync_once(account_id)
        if state["ok"]:
            consecutive_errors = 0
        else:
            consecutive_errors += 1
            # 只在前几次和每 20 次报一下，避免大QMT 关机时刷屏
            if consecutive_errors <= 3 or consecutive_errors % 20 == 0:
                print(f"[同步] 账号 {account_id} 第 {consecutive_errors} 次失败: {state['error']}")

        interval = cfg.poll_seconds if _in_trading_session() else cfg.idle_poll_seconds
        if consecutive_errors:
            # 退避：连错就拉长间隔，最多到 idle 间隔，别把 Redis 打满
            interval = min(cfg.idle_poll_seconds, interval * min(consecutive_errors, 8))
        _STOP.wait(interval)
    print(f"[同步] 账号 {account_id} 轮询已停止")


def start_all():
    """为所有启用账号拉起轮询线程。已在跑的不重复起。"""
    _STOP.clear()
    started = []
    for cfg in bridge_config.list_accounts():
        with _LOCK:
            thread = _THREADS.get(cfg.account_id)
            if thread is not None and thread.is_alive():
                continue
            thread = threading.Thread(target=_loop, args=(cfg,),
                                      name="qmt-sync-%s" % cfg.account_id, daemon=True)
            _THREADS[cfg.account_id] = thread
        thread.start()
        started.append(cfg.account_id)
    if not started:
        print("[同步] 没有启用的账号，轮询未启动")
    return started


def stop_all():
    _STOP.set()
    with _LOCK:
        threads = list(_THREADS.values())
        _THREADS.clear()
    for thread in threads:
        thread.join(timeout=2)


def last_state(account_id):
    with _LOCK:
        return _STATE.get(str(account_id or "").strip())


def snapshot():
    with _LOCK:
        return dict(_STATE)
