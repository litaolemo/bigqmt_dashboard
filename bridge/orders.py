"""唯一的下单出口。

改造前「下单」是往内存队列塞一条记录（app.py 的 pending_sell_commands 等），等 QMT
客户端下次轮询时取走自己执行 —— 服务端既不知道单子有没有报进去，也拦不住客户端不听
话。风控开关（停买/停卖/锁仓/仓位系数）全都只是**下发给客户端的建议值**。

现在所有下单都过这里：风控在服务端当场判定，判定通过才 RPC 到大QMT，返回真实
order_sys_id，失败原因当场可见。风控从建议变成闸门。

风控与审计通过注册钩子注入（register_risk_gate / register_audit_sink），因为它们要读
dashboard 的 SQLite，而 app.py 会 import 本模块 —— 直接反向 import 会成环。
"""

import datetime as _datetime
import threading
import time

import settings
from bridge import instruments
from bridge import optypes
from bridge import market as bridge_market
from bridge import pool as bridge_pool
from bridge import pricecage
from bridge import pricetypes

SIDE_BUY = "buy"
SIDE_SELL = "sell"

# 报价方式。完整的选价类型表（含分交易所的市价指令）在 bridge.pricetypes，
# 这里只留几个常用别名，老调用方不用改。默认最新价：面板上点「卖 50%」要的是
# 尽快成交，不是挂一个可能不成的价。
PRICE_TYPE_LATEST = "latest"
PRICE_TYPE_FIX = "fix"
PRICE_TYPE_PEER = "peer"        # 对手价（对方一档）
PRICE_TYPE_MINE = "mine"        # 挂单价（本方一档）


def price_type_choices(stock_code=""):
    """该标的能用的选价类型。见 bridge.pricetypes。"""
    return pricetypes.choices_for(stock_code)

_HOOKS = {"risk_gate": None, "audit_sink": None}
_LOCK = threading.Lock()


class OrderRejected(Exception):
    """风控或参数校验拒单。不是异常路径，是预期内的业务结果。"""


def register_risk_gate(fn):
    """注册风控闸门：fn(request_dict) -> None，不放行就 raise OrderRejected。"""
    with _LOCK:
        _HOOKS["risk_gate"] = fn


class temporary_risk_gate:
    """测试用：临时换一个风控闸门，退出时换回原来那个——不是换成 None。

    `_HOOKS` 是模块级全局，进程内所有测试共用一份。真实闸门是 app.py 启动时
    注册的 order_risk_gate；测试如果图省事在 finally 里写
    `register_risk_gate(None)`，退出时就把它清空了，而不是换回原来的那个。
    同一个 pytest 进程后面所有测试都会在"没有风控闸门"的状态下跑，而且
    大概率没人注意到——直到某个断言风控生效的测试莫名其妙失败。

    用法::

        with bridge_orders.temporary_risk_gate(fake_gate):
            ...
    """

    def __init__(self, fn):
        self._fn = fn
        self._saved = None

    def __enter__(self):
        with _LOCK:
            self._saved = _HOOKS["risk_gate"]
            _HOOKS["risk_gate"] = self._fn
        return self

    def __exit__(self, *exc):
        with _LOCK:
            _HOOKS["risk_gate"] = self._saved
        return False


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


# ---- 全局熔断：一天内碰交易所的次数有上限 ----
#
# 防的是软件自己失控——某个 bug（脚本死循环、条件单反复误触发、误操作连点）
# 在短时间内反复真实报单/撤单。按自然日计数，进程重启会清零：重启通常就是
# 运维在处理导致失控的那个 bug，清零是合理的，不值得为了这个再接一张持久化表、
# 再操心跨进程/跨重启的一致性。
#
# 只在真的要碰交易所之前计数（下单提交前 / 撤单提交前），被更早的校验或风控
# 拦下的请求不算——那些根本没碰交易所，不该占熔断额度，也不该被无关的失败
# 请求提前把额度耗尽。
DAILY_ACTION_LIMIT = settings.env_int("DAILY_ACTION_LIMIT", 2000)

_ACTION_GUARD_LOCK = threading.Lock()
_ACTION_GUARD_STATE = {"date": None, "count": 0}


def check_daily_action_limit():
    today = _datetime.date.today()
    with _ACTION_GUARD_LOCK:
        if _ACTION_GUARD_STATE["date"] != today:
            _ACTION_GUARD_STATE["date"] = today
            _ACTION_GUARD_STATE["count"] = 0
        _ACTION_GUARD_STATE["count"] += 1
        count = _ACTION_GUARD_STATE["count"]
    if count > DAILY_ACTION_LIMIT:
        raise OrderRejected(
            "今日买卖撤单已达 %d 次上限，熔断保护已触发。如确认是真实需要，"
            "重启服务会重新计数；如果是误触发，先查清楚是什么在反复下单。"
            % DAILY_ACTION_LIMIT)


def daily_action_count():
    """今天已经用掉多少次，供 UI/告警展示。"""
    today = _datetime.date.today()
    with _ACTION_GUARD_LOCK:
        return _ACTION_GUARD_STATE["count"] if _ACTION_GUARD_STATE["date"] == today else 0


def reset_daily_action_count():
    """管理员手动复位，或测试用。"""
    with _ACTION_GUARD_LOCK:
        _ACTION_GUARD_STATE["date"] = None
        _ACTION_GUARD_STATE["count"] = 0


def price_cage_for(code, side, now=None):
    """该品种此刻的限价有效范围。"""
    from datetime import datetime

    now = now or datetime.now()
    session = pricecage.session_of(now)
    reference = bridge_market.price_reference(code)
    base = pricecage.base_price_of(session, reference["last_price"],
                                   reference["last_close"])
    cage = pricecage.compute(code, side, base, session,
                             up_stop=reference["up_stop"],
                             down_stop=reference["down_stop"])
    cage["last_price"] = reference["last_price"]
    cage["last_close"] = reference["last_close"]
    return cage


def _resolve_price_type(price_type, stock_code):
    """解析成 passorder 的 prType。

    prType 是直接透传给 passorder 的整数，服务端不做二次映射，所以这里给对值
    就够了 —— 不需要再从 xtconstant 取常量名。分交易所的市价指令在这一步
    就会被拦下（深市票选沪市的 42 之类）。
    """
    try:
        return pricetypes.resolve(price_type, stock_code)
    except ValueError as e:
        raise OrderRejected(str(e))


def place_order(account_id, stock_code, side, volume, price=0.0,
                price_type=PRICE_TYPE_LATEST, sell_all=False,
                strategy_name="dashboard", remark="", operator="",
                skip_risk=False, bypass=None, trade_mode=""):
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
        "trade_mode": str(trade_mode or "").strip().lower(),
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

        # 买卖指令类型。同一个「买」在普通账户和信用账户上是两条不同的指令，
        # 要按账户类型定，不能一律 23/24。见 bridge/optypes.py。
        account_type = bridge_pool.account_type_of(account_id)
        try:
            order_type, mode_spec = optypes.resolve(
                result["trade_mode"], account_type, side)
        except ValueError as e:
            raise OrderRejected(str(e))
        result["trade_mode"] = mode_spec["value"]
        result["order_type"] = order_type
        result["trade_mode_label"] = mode_spec["side_label"]
        result["account_type"] = account_type

        # 数量规整必须在风控之前：风控要按真实报单量判断
        adjusted = instruments.round_volume(code, volume, sell_all=sell_all)
        if adjusted <= 0:
            raise OrderRejected(
                "%s 最小申报 %d%s，%s%s 不足一手" % (
                    code, instruments.min_volume(code), result["unit"],
                    volume, result["unit"]))
        result["volume"] = adjusted

        pr_type, price_spec = _resolve_price_type(result["price_type"], code)
        result["pr_type"] = pr_type
        result["price_role"] = price_spec["price_role"]
        result["price_type_label"] = price_spec["label"]

        normalized_price = instruments.round_price(code, price)
        result["price"] = normalized_price
        if price_spec["price_role"] == pricetypes.PRICE_ROLE_ORDER:
            # price 就是委托价，要受价格笼子约束
            if normalized_price <= 0:
                raise OrderRejected("%s 必须给出有效价格" % price_spec["label"])
            # 超出有效申报范围交易所会直接废单，不如在这里说清楚。
            # 算不出范围时放行 —— 不能凭猜的基准价拦下用户的单。
            cage = price_cage_for(code, side)
            result["price_cage"] = cage
            ok, message = pricecage.check(code, side, normalized_price, cage)
            if not ok:
                raise OrderRejected(message)
        elif price_spec["price_role"] == pricetypes.PRICE_ROLE_NONE:
            # 档位价/最新价这类，price 无意义，填 0 占位免得误导
            normalized_price = 0.0
            result["price"] = 0.0
        # PRICE_ROLE_PROTECT：price 是保护限价，0 表示取涨跌停价，不做笼子校验

        if not skip_risk:
            _run_risk_gate(dict(result))
        check_daily_action_limit()

        trader = bridge_pool.get_trader(account_id)
        acc = bridge_pool.get_account_ref(account_id)
        price_const = pr_type

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
        check_daily_action_limit()
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
