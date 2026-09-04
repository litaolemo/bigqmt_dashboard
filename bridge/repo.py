"""国债逆回购的自动出借。

不是买卖股票，是把收盘前账户里没花完的闲置现金按当日最优年化利率出借一晚
（或跨周末几天），不出借就是白放着吃不到这块利息。

代码固定两个：深市 131810.SZ、沪市 204001.SH，都是市场上最活跃的 1 天期品种。
两个的最新价（lastPrice）就是当前的年化利率（国债逆回购报价的行业惯例——
价格数值本身就是百分比年化利率，比如 1.850 表示 1.85%），报哪个看哪个更高。

金额换算公式和下单方式是账户脚本里已经在用、跑通过的逻辑，原样照搬——不套用
bridge/instruments.py 的股票取整规则，逆回购的最小/步进单位和普通股票完全
不是一回事，套错了会让金额算错。价格给 LATEST_PRICE 常量对应的值，逆回购
按最新成交利率成交，不需要也不该自己指定一个价格。

**不走 bridge/orders.py 的 place_order()**：那条路径的品种规整
（round_volume / round_price）、价格笼子都是按股票设计的，套到逆回购上是
错的。但仍然遵守同一条全局熔断（bridge.orders.check_daily_action_limit）和
账号的 allow_order 开关——这两条是"防误操作/防失控"的通用闸门，跟品种无关，
逆回购不能绕过。

只在收盘前后的时间窗口里跑一次；每个账号每个自然日最多跑一次。"今天跑没跑过"
是进程内存状态，不落库——跟 cb/ipo.py 的 run_once 是同一个取舍：图简单，
代价是重启当天可能会再跑一次，但反正跑第二次时上一次出借掉的现金已经不在
可用余额里了，算出来的金额会是 0，不会真的重复出借。
"""

import threading
from datetime import datetime

from bridge import config as bridge_config
from bridge import market as bridge_market
from bridge import orders as bridge_orders
from bridge import pool as bridge_pool

SH_CODE = "204001.SH"
SZ_CODE = "131810.SZ"
CODES = (SH_CODE, SZ_CODE)

MIN_CASH = 10000.0   # 换算公式在这以下会算出 0，不用尝试（1 万元档位）

_LAST_RUN = {}       # account_id -> 'YYYY-MM-DD'
_LOCK = threading.RLock()


def resolve_amount(cash):
    """账户脚本里验证过的换算公式，原样照搬，不做任何"改进"。"""
    try:
        cash = float(cash or 0)
    except (TypeError, ValueError):
        return 0
    if cash <= 0:
        return 0
    return int(int(cash * 0.001 * 10) / 100) * 100


def best_code(rates):
    """rates: {code: rate}，挑利率更高的那个代码；两个都取不到返回 None。

    利率是浮点数比较，两边都能拿到时优先深市（>=，不是 >）跟原脚本一致——
    利率相等时选哪个都一样，不需要额外规则。
    """
    sh_rate = rates.get(SH_CODE)
    sz_rate = rates.get(SZ_CODE)
    if sh_rate is None and sz_rate is None:
        return None
    if sh_rate is None:
        return SZ_CODE
    if sz_rate is None:
        return SH_CODE
    return SH_CODE if sh_rate >= sz_rate else SZ_CODE


def submit(account_id, strategy_name="reverse_repo", remark="reverse_repo"):
    """给一个账号出借一次。返回结果 dict，绝不抛异常。"""
    result = {"account_id": account_id, "ok": False, "code": "", "amount": 0,
             "rate": None, "order_sys_id": None, "message": "",
             "at": datetime.now().isoformat(timespec="seconds")}
    try:
        if not bridge_pool.allow_order(account_id):
            result["message"] = "账号 %s 未开启下单" % account_id
            return result

        trader = bridge_pool.get_trader(account_id)
        acc = bridge_pool.get_account_ref(account_id)
        asset = trader.query_stock_asset(acc)
        cash = float(getattr(asset, "cash", 0) or 0)
        amount = resolve_amount(cash)
        if amount <= 0:
            result["ok"] = True   # 不是失败，是没什么好出借的
            result["message"] = "可用资金不足 %.0f 元，不用出借（当前 %.2f）" % (MIN_CASH, cash)
            return result

        ticks = bridge_market.get_ticks(list(CODES))
        rates = {code: (ticks.get(code) or {}).get("lastPrice") for code in CODES}
        code = best_code(rates)
        if code is None:
            result["message"] = "取不到逆回购行情，跳过（不能凭猜的利率下单）"
            return result

        bridge_orders.check_daily_action_limit()

        compat = bridge_pool._compat()
        response = trader.order_stock_result(
            acc, code, compat.STOCK_SELL, amount, compat.LATEST_PRICE, 1.0,
            strategy_name, remark,
        )
        order_sys_id = (response or {}).get("order_sys_id")
        result["code"] = code
        result["amount"] = amount
        result["rate"] = rates.get(code)
        if order_sys_id in (None, -1, "-1", ""):
            result["message"] = str((response or {}).get("message") or "大QMT 未返回委托号")
        else:
            result["ok"] = True
            result["order_sys_id"] = order_sys_id
            result["message"] = "已出借 %d 元至 %s（年化 %s%%），委托号 %s" % (
                amount, code, rates.get(code), order_sys_id)
    except bridge_orders.OrderRejected as e:
        result["message"] = str(e)
    except bridge_pool.BridgeUnavailable as e:
        result["message"] = str(e)
    except Exception as e:
        result["message"] = "%s: %s" % (type(e).__name__, e)
    return result


def run_once(enabled_for, force=False):
    """给所有"启用了自动逆回购"的账号各出借一次，每个账号每个自然日最多一次。

    enabled_for(account_id) -> bool 由调用方传入（app.py 读 trading_status 表
    的 reverse_repo_enabled 字段），本模块不直接连 dashboard 的数据库，
    避免反向 import——跟 bridge/orders.py 的 register_risk_gate 是同一个道理。

    什么时候该调这个函数（收盘前后的时间窗口）由调用方决定，这里不管时间，
    方便测试和复用。
    """
    results = []
    today = datetime.now().strftime("%Y-%m-%d")
    for cfg in bridge_config.list_accounts():
        account_id = cfg.account_id
        try:
            if not enabled_for(account_id):
                continue
        except Exception as e:
            print(f"[逆回购] 查询账号 {account_id} 开关状态失败: {e}")
            continue
        with _LOCK:
            if not force and _LAST_RUN.get(account_id) == today:
                continue
            _LAST_RUN[account_id] = today
        outcome = submit(account_id)
        results.append(outcome)
        print("[逆回购] %s: %s" % (account_id, outcome["message"]))
    return results


def reset_state():
    """测试用。"""
    with _LOCK:
        _LAST_RUN.clear()
