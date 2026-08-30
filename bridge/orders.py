"""唯一的下单出口。

改造前「下单」是往内存队列塞一条记录（app.py 的 pending_sell_commands 等），等 QMT
客户端下次轮询时取走自己执行 —— 服务端既不知道单子有没有报进去，也拦不住客户端不听
话。风控开关（停买/停卖/锁仓/仓位系数）全都只是**下发给客户端的建议值**。

现在所有下单都过这里：风控在服务端当场判定，判定通过才 RPC 到大QMT，返回真实
order_sys_id，失败原因当场可见。风控从建议变成闸门。

风控与审计通过注册钩子注入（register_risk_gate / register_audit_sink），因为它们要读
dashboard 的 SQLite，而 app.py 会 import 本模块 —— 直接反向 import 会成环。
"""

import threading
import time

from bridge import instruments
from bridge import pool as bridge_pool

SIDE_BUY = "buy"
SIDE_SELL = "sell"

# 报价方式。默认最新价：面板上点「卖 50%」要的是尽快成交，不是挂一个可能不成的价。
PRICE_TYPE_LATEST = "latest"
PRICE_TYPE_FIX = "fix"
PRICE_TYPE_PEER = "peer"        # 对手方最优价

_PRICE_TYPE_CONSTS = {
    PRICE_TYPE_LATEST: "LATEST_PRICE",
    PRICE_TYPE_FIX: "FIX_PRICE",
    PRICE_TYPE_PEER: "MARKET_PEER_PRICE_FIRST",
}

_HOOKS = {"risk_gate": None, "audit_sink": None}
_LOCK = threading.Lock()


class OrderRejected(Exception):
    """风控或参数校验拒单。不是异常路径，是预期内的业务结果。"""


def register_risk_gate(fn):
    """注册风控闸门：fn(request_dict) -> None，不放行就 raise OrderRejected。"""
    with _LOCK:
        _HOOKS["risk_gate"] = fn


def register_audit_sink(fn):
    """注册审计落库：fn(record_dict) -> None。抛错不影响下单结果。"""
    with _LOCK:
        _HOOKS["audit_sink"] = fn


def _run_risk_gate(request):
    with _LOCK:
        gate = _HOOKS["risk_gate"]
    if gate is not None:
        gate(request)


def _write_audit(record):
    with _LOCK:
        sink = _HOOKS["audit_sink"]
    if sink is None:
        return
    try:
        sink(record)
    except Exception as e:
        # 审计写失败不能反过来把已经报出去的单子搞成"失败"
        print(f"[下单审计] 写入失败: {e}")


def _resolve_price_type(compat, price_type):
    name = _PRICE_TYPE_CONSTS.get(str(price_type or PRICE_TYPE_LATEST).lower())
    if name is None:
        raise OrderRejected("不支持的报价方式: %s" % price_type)
    value = getattr(compat, name, None)
    if value is None:
        raise OrderRejected("桥接层缺少报价常量 %s" % name)
    return value


def place_order(account_id, stock_code, side, volume, price=0.0,
                price_type=PRICE_TYPE_LATEST, sell_all=False,
                strategy_name="dashboard", remark="", operator="",
                skip_risk=False, bypass=None):
    """下一笔单。

    永远返回结果 dict，不为业务拒单抛异常 —— 清仓这类批量场景要逐笔收集结果，
    不能被中间一笔打断。调用方看 result["ok"]。
    """
    account_id = str(account_id or "").strip()
    side = str(side or "").lower()
    code = instruments.normalize_code(stock_code)
    result = {
        "ok": False, "account_id": account_id, "stock_code": code,
        "side": side, "volume": 0, "price": 0.0,
        "price_type": str(price_type or PRICE_TYPE_LATEST).lower(),
        "order_sys_id": None, "status": "rejected", "message": "",
        "unit": instruments.unit_name(code),
        # 手动操作可以显式绕过某些闸门（历史行为：手动买入忽略「停止买入」开关）。
        # 绕过什么都会写进审计流水，不是悄悄放行。
        "bypass": set(bypass or ()),
        "created_at": time.time(),
    }

    try:
        if side not in (SIDE_BUY, SIDE_SELL):
            raise OrderRejected("方向只能是 buy 或 sell，收到 %r" % side)
        if not code:
            raise OrderRejected("缺少股票代码")
        if not bridge_pool.allow_order(account_id):
            raise OrderRejected(
                "账号 %s 未开启下单（config/accounts.json 的 allow_order）" % account_id)

        # 数量规整必须在风控之前：风控要按真实报单量判断
        adjusted = instruments.round_volume(code, volume, sell_all=sell_all)
        if adjusted <= 0:
            raise OrderRejected(
                "%s 最小申报 %d%s，%s%s 不足一手" % (
                    code, instruments.min_volume(code), result["unit"],
                    volume, result["unit"]))
        result["volume"] = adjusted

        normalized_price = instruments.round_price(code, price)
        result["price"] = normalized_price
        if result["price_type"] == PRICE_TYPE_FIX and normalized_price <= 0:
            raise OrderRejected("限价委托必须给出有效价格")

        if not skip_risk:
            _run_risk_gate(dict(result))

        compat = bridge_pool._compat()
        trader = bridge_pool.get_trader(account_id)
        acc = bridge_pool.get_account_ref(account_id)
        order_type = compat.STOCK_BUY if side == SIDE_BUY else compat.STOCK_SELL
        price_const = _resolve_price_type(compat, result["price_type"])

        response = trader.order_stock_result(
            acc, code, order_type, adjusted, price_const, normalized_price,
            strategy_name, remark or "dashboard",
        )
        order_sys_id = (response or {}).get("order_sys_id")
        result["order_sys_id"] = order_sys_id
        if order_sys_id in (None, -1, "-1", ""):
            result["status"] = "failed"
            result["message"] = str((response or {}).get("message") or "大QMT 未返回委托号")
        else:
            result["ok"] = True
            result["status"] = "submitted"
            result["message"] = "已报单"
        result["raw"] = response if isinstance(response, dict) else {}

    except OrderRejected as e:
        result["status"] = "rejected"
        result["message"] = str(e)
    except bridge_pool.BridgeUnavailable as e:
        result["status"] = "unavailable"
        result["message"] = str(e)
    except Exception as e:
        result["status"] = "failed"
        result["message"] = "%s: %s" % (type(e).__name__, e)

    result["operator"] = operator
    result["remark"] = remark
    result["bypass"] = sorted(result.get("bypass") or ())
    _write_audit(result)
    return result


def cancel_order(account_id, order_id=None, order_sysid=None, market=None,
                 operator=""):
    """撤单。有 order_id 走 cancel_order_stock，只有交易所委托号则走 sysid 版。"""
    account_id = str(account_id or "").strip()
    result = {"ok": False, "account_id": account_id, "order_id": order_id,
              "order_sysid": order_sysid, "status": "rejected", "message": "",
              "operator": operator, "created_at": time.time(), "side": "cancel"}
    try:
        if not bridge_pool.allow_order(account_id):
            raise OrderRejected("账号 %s 未开启下单" % account_id)
        trader = bridge_pool.get_trader(account_id)
        acc = bridge_pool.get_account_ref(account_id)
        if order_id not in (None, "", -1):
            code = trader.cancel_order_stock(acc, order_id)
        elif order_sysid:
            if not market:
                raise OrderRejected("按交易所委托号撤单必须给出 market")
            code = trader.cancel_order_stock_sysid(acc, market, order_sysid)
        else:
            raise OrderRejected("撤单需要 order_id 或 order_sysid")
        # 大QMT 撤单返回 0 表示请求已受理
        result["ok"] = (code == 0)
        result["status"] = "submitted" if result["ok"] else "failed"
        result["message"] = "撤单已提交" if result["ok"] else "撤单被拒绝 (code=%s)" % code
        result["return_code"] = code
    except OrderRejected as e:
        result["message"] = str(e)
    except bridge_pool.BridgeUnavailable as e:
        result["status"] = "unavailable"
        result["message"] = str(e)
    except Exception as e:
        result["status"] = "failed"
        result["message"] = "%s: %s" % (type(e).__name__, e)
    _write_audit(result)
    return result
