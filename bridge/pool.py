"""account_id → 大QMT 连接实例的缓存与探活。

**不要用 xtquant_compat.configure()。** 它写的是模块级单例 xt_trader / xtdata
（xtquant_compat.py 末尾），一个进程只能有一个账号。本项目是多账号多实例——不同账号
可能连不同机器上的大QMT、不同 Redis db —— 所以这里每账号直接构造
BigQmtXtTrader，再复用它自己的 RpcClient 建 BigQmtXtData。

BigQmtRpcClient 的 redis 连接是懒建的（_redis() 里才 import redis 并连），
所以构造实例本身不联网，池子可以放心按需初始化。
"""

import threading
import time

from bridge import config as bridge_config

_TRADERS = {}       # account_id -> BigQmtXtTrader
_XTDATA = {}        # account_id -> BigQmtXtData
_HEALTH = {}        # account_id -> dict
_LOCK = threading.RLock()

_COMPAT = {"module": None, "error": ""}


class BridgeUnavailable(RuntimeError):
    """桥接层没装、或该账号没配置。调用方应返回明确错误而不是 500。"""


def _compat():
    """惰性导入 xtquant_big_convert；没装就抛 BridgeUnavailable。

    没装 QMT 的机器（CI、只想看代码的人）clone 下来也要能 import app、能跑测试，
    所以这个 import 绝不能放模块顶层。
    """
    with _LOCK:
        if _COMPAT["module"] is not None:
            return _COMPAT["module"]
        if _COMPAT["error"]:
            raise BridgeUnavailable(_COMPAT["error"])
        try:
            from bigqmt_signal_trader import xtquant_compat
        except Exception as e:
            _COMPAT["error"] = (
                "未安装 xtquant_big_convert（pip install xtquant-big-convert[redis]），"
                "大QMT 直连不可用: %s" % e
            )
            print(f"[bridge] {_COMPAT['error']}")
            raise BridgeUnavailable(_COMPAT["error"])
        _COMPAT["module"] = xtquant_compat
        return xtquant_compat


def compat_available():
    try:
        _compat()
        return True
    except BridgeUnavailable:
        return False


def _require_config(account_id):
    cfg = bridge_config.get_account(account_id)
    if cfg is None:
        raise BridgeUnavailable("账号 %s 未在 config/accounts.json 中配置" % account_id)
    if not cfg.enabled:
        raise BridgeUnavailable("账号 %s 已在配置中停用" % account_id)
    return cfg


def get_trader(account_id):
    """取该账号的 BigQmtXtTrader，按 account_id 缓存。"""
    account_id = str(account_id or "").strip()
    with _LOCK:
        trader = _TRADERS.get(account_id)
        if trader is not None:
            return trader
    cfg = _require_config(account_id)
    compat = _compat()
    trader = compat.BigQmtXtTrader(
        account_id=cfg.account_id,
        redis_config=cfg.rpc,
        timeout_seconds=cfg.timeout_seconds,
    )
    with _LOCK:
        # 双检：另一个线程可能刚建好
        existing = _TRADERS.get(account_id)
        if existing is not None:
            return existing
        _TRADERS[account_id] = trader
    print(f"[bridge] 账号 {account_id} 连接已建立 (transport={cfg.transport})")
    return trader


def get_xtdata(account_id):
    """取该账号的行情句柄，复用 trader 的 RpcClient，不额外建连接。"""
    account_id = str(account_id or "").strip()
    with _LOCK:
        handle = _XTDATA.get(account_id)
        if handle is not None:
            return handle
    trader = get_trader(account_id)
    handle = _compat().BigQmtXtData(trader.client)
    with _LOCK:
        existing = _XTDATA.get(account_id)
        if existing is not None:
            return existing
        _XTDATA[account_id] = handle
    return handle


def get_account_ref(account_id):
    """构造 StockAccount，下单/查询接口都要这个对象。"""
    cfg = _require_config(account_id)
    return _compat().StockAccount(cfg.account_id, cfg.account_type)


def allow_order(account_id):
    """本地是否允许对该账号下单。大QMT 侧还有 rpc_allow_order_methods，两边都开才行。"""
    cfg = bridge_config.get_account(account_id)
    return bool(cfg and cfg.enabled and cfg.allow_order)


def check_health(account_id, timeout_seconds=3.0):
    """一次 ping 探活，结果写进 _HEALTH 并返回。

    这个取代了改造前基于「客户端最后一次 push 时间」的在线判定
    （app.py 的 account_last_sync）：以前客户端崩了要等超时才看得出来，
    现在是主动探测，而且能量出往返延迟。
    """
    account_id = str(account_id or "").strip()
    started = time.time()
    status = {"account_id": account_id, "online": False, "latency_ms": None,
              "error": "", "checked_at": started}
    try:
        trader = get_trader(account_id)
        response = trader.client.call("ping", timeout_seconds=timeout_seconds)
        status["online"] = True
        status["latency_ms"] = round((time.time() - started) * 1000, 1)
        status["response"] = response if isinstance(response, dict) else {}
    except BridgeUnavailable as e:
        status["error"] = str(e)
    except Exception as e:
        status["error"] = "%s: %s" % (type(e).__name__, e)
    with _LOCK:
        _HEALTH[account_id] = status
    return status


def last_health(account_id):
    """上一次探活结果；没探过返回 None（调用方不要为此阻塞请求）。"""
    with _LOCK:
        return _HEALTH.get(str(account_id or "").strip())


def health_snapshot():
    """所有已配置账号的在线状态，给前端账号列表用。"""
    result = {}
    for cfg in bridge_config.list_accounts():
        result[cfg.account_id] = last_health(cfg.account_id) or {
            "account_id": cfg.account_id, "online": False,
            "latency_ms": None, "error": "尚未探测", "checked_at": None,
        }
    return result


def close_all():
    """丢掉所有缓存实例，配置变更或测试收尾用。"""
    with _LOCK:
        for account_id, trader in list(_TRADERS.items()):
            try:
                if getattr(trader, "_event_running", False):
                    trader.stop()
            except Exception as e:
                print(f"[bridge] 关闭账号 {account_id} 连接时出错: {e}")
        _TRADERS.clear()
        _XTDATA.clear()
        _HEALTH.clear()
