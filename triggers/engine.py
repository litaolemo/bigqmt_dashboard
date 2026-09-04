"""条件单（止损/止盈/条件买入）触发引擎。

后台线程每隔几秒检查一次所有 active 的条件单：批量取一遍它们涉及的标的的
最新价（复用 bridge/market.py 已有的行情主连接，不额外订阅），价格越过触发线
就调 bridge/orders.place_order() 真的下单——和面板上点按钮下单走的是同一条
风控/价格笼子/审计链路，条件单不能绕过这些闸门。

行情不可用时（bridge_market.available() 为 False）整轮直接跳过，宁可不检查，
也不能拿猜的价格去判断该不该下单。

跟 app.py 之间用 register_hooks() 注入依赖（可用持仓查询、通知），不直接
import app —— app.py 会 import 本模块，反过来 import 就成环了，跟
bridge/orders.py 的 register_risk_gate 是同一个道理。
"""

import threading
from datetime import datetime

from bridge import instruments
from bridge import market as bridge_market
from bridge import orders as bridge_orders
from sync import sinks as sync_sinks
from sync import ws_hub
from triggers import store

# 有 active 条件单时的检查间隔；没有时拉长，省得空转。
_INTERVAL_ACTIVE_SECONDS = 5
_INTERVAL_IDLE_SECONDS = 30

# 同一条条件单"触发了但下单失败"这种情况的通知节流：条件持续成立时引擎会
# 每一轮都重试，但不能每 5 秒炸一次手机。
_RETRY_NOTIFY_COOLDOWN_SECONDS = 300

_LABELS = {
    "stop_loss": "止损", "take_profit": "止盈",
    "buy_dip": "条件买入（下探）", "buy_breakout": "条件买入（突破）",
    "limit_up_break": "涨停破板卖出", "limit_up_buy": "涨停买入",
}

# 用到当天动态涨停价的 compare 模式——check_once() 得单独给这些代码多查一次
# get_instrument_detail（get_ticks 批量快照里没有涨跌停价这个字段）。
_DYNAMIC_COMPARE_MODES = frozenset({"limit_break", "limit_touch"})

_HOOKS = {"get_available_volume": None, "notify": None}
_thread = None
_stop_event = threading.Event()


def register_hooks(get_available_volume=None, notify=None):
    """get_available_volume(account_id, stock_code) -> {"can_use_volume": int} 或 None
    notify(title, content)
    两个都是可选的：不注入就等于该能力不可用（卖出条件单会退化成每次都跳过，
    通知退化成只打日志），不会因为没注入就报错。
    """
    if get_available_volume is not None:
        _HOOKS["get_available_volume"] = get_available_volume
    if notify is not None:
        _HOOKS["notify"] = notify


def _notify(title, content):
    fn = _HOOKS.get("notify")
    if fn is None:
        print(f"[条件单] {title}: {content}")
        return
    try:
        fn(title, content)
    except Exception as e:
        print(f"[条件单] 通知失败: {e}")


def _available_volume(account_id, stock_code):
    fn = _HOOKS.get("get_available_volume")
    if fn is None:
        return None
    try:
        info = fn(account_id, stock_code)
        return int((info or {}).get("can_use_volume") or 0)
    except Exception as e:
        print(f"[条件单] 查询可用持仓失败 ({account_id} {stock_code}): {e}")
        return None


def _is_trading_session():
    return bool(sync_sinks.call("is_trading_session"))


def _tick_price(tick):
    value = tick.get("lastPrice")
    if value is None:
        value = tick.get("last_price")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _tick_high(tick):
    """当日最高价。字段名跟 lastPrice/last_price 一样有两种可能的拼法。"""
    value = tick.get("high")
    if value is None:
        value = tick.get("dayHigh")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _limit_up_break_met(row, tick, up_stop):
    """涨停破板：当日最高价碰过涨停价，最新价又跌回涨停价以下。

    容差取一个最小报价单位——两边算涨停价的路径不一定完全一致（快照 vs
    合约详情），卡在理论涨停价的边缘可能因为舍入差一点点，不该因为这个就
    判定"没碰过"。
    """
    if not up_stop or up_stop <= 0:
        return False   # 取不到涨停价就不判断，不能凭猜的价格下单
    price = _tick_price(tick)
    day_high = _tick_high(tick)
    if price is None or day_high is None:
        return False
    tolerance = max(instruments.price_tick(row["stock_code"]), 0.001)
    touched = day_high >= up_stop - tolerance
    return touched and price < up_stop - tolerance


def _limit_up_buy_met(row, tick, up_stop):
    """涨停买入：最新价到了当天涨停价（或者极接近，容差同上）。"""
    if not up_stop or up_stop <= 0:
        return False
    price = _tick_price(tick)
    if price is None:
        return False
    tolerance = max(instruments.price_tick(row["stock_code"]), 0.001)
    return price >= up_stop - tolerance


def _condition_met(row, tick, up_stop=None):
    """返回 (是否满足, 用于下单判断/展示的参考价)。

    普通类型参考价就是最新价；动态类型的"触发价"是当天涨停价，参考价也
    用它，这样通知文案里显示的是有意义的那个数字，不是恒为 0 的
    trigger_price。
    """
    compare = row["compare"]
    if compare == "limit_break":
        return _limit_up_break_met(row, tick, up_stop), up_stop
    if compare == "limit_touch":
        return _limit_up_buy_met(row, tick, up_stop), up_stop

    price = _tick_price(tick)
    if price is None:
        return False, None
    if compare == "lte":
        return price <= row["trigger_price"], price
    return price >= row["trigger_price"], price


def _resolve_sell_volume(row):
    """返回 (volume, sell_all, error)。

    error 非空表示这条该判失败，volume 为 0 表示这轮先跳过。sell_all 要一路
    带到 place_order（见 _fire）——那边自己也会用同一个规则再规整一次数量，
    这里算出的 100% 卖出量不传 sell_all 会被那边按整手再砍掉零股，等于白算。
    """
    available = _available_volume(row["account_id"], row["stock_code"])
    if available is None:
        return 0, False, ""   # 查不到可用量：先跳过，不能凭猜的数字下单，也不能就此判失败
    if available <= 0:
        return 0, False, "无可用持仓，条件单已失效"
    if row["percentage"]:
        sell_all = row["percentage"] >= 100
        volume = instruments.round_volume(
            row["stock_code"], int(available * row["percentage"] / 100), sell_all=sell_all)
    else:
        sell_all = int(row["volume"] or 0) >= available
        volume = instruments.round_volume(
            row["stock_code"], min(int(row["volume"] or 0), available), sell_all=sell_all)
    if volume <= 0:
        return 0, False, "按当前可用持仓算出的数量不足一手"
    return volume, sell_all, ""


def _should_notify_retry(last_notified_at):
    if not last_notified_at:
        return True
    try:
        last = datetime.fromisoformat(str(last_notified_at))
        # SQLite 的 CURRENT_TIMESTAMP 是 UTC，要跟 utcnow() 比，不能跟本地时间
        # 比——本地时间比 UTC 快 8 小时的话，每次都会误判成“早就过了冷却期”，
        # 冷却完全不起作用。
        return (datetime.utcnow() - last).total_seconds() >= _RETRY_NOTIFY_COOLDOWN_SECONDS
    except Exception:
        return True


def _describe_condition(row, price):
    """通知文案里"为什么触发了"那句话。动态类型（涨停破板/涨停买入）没有
    用户填的 trigger_price，说"越过触发价 0.000"没有意义，得单独措辞。
    """
    if row["compare"] in _DYNAMIC_COMPARE_MODES:
        if price:
            return "当天涨停价 %.3f" % price
        return "当天涨停价"
    return "最新价 %.3f 越过触发价 %.3f" % (price, row["trigger_price"])


def _fire(row, price):
    order_id = row["id"]
    label = _LABELS.get(row["trigger_type"], row["trigger_type"])
    unit = instruments.unit_name(row["stock_code"])
    condition_text = _describe_condition(row, price)

    sell_all = False
    if row["side"] == "sell":
        volume, sell_all, error = _resolve_sell_volume(row)
        if error:
            store.mark_failed(order_id, error)
            _notify("条件单失效：%s" % row["stock_code"],
                    "%s %s：%s（%s）" % (label, row["stock_code"], error, condition_text))
            ws_hub.publish(row["account_id"], "conditional_order",
                          dict(row, status="failed", message=error))
            return
        if volume <= 0:
            return   # 查不到可用量，这轮先跳过
    else:
        volume = int(row["volume"] or 0)

    # 真的要下单了才抢占这条条件单——抢不到说明已经被别的检查周期处理掉了
    # （正常流程下不会发生，是防御性的，见 store.claim_for_firing 的说明）。
    if not store.claim_for_firing(order_id):
        return

    result = bridge_orders.place_order(
        account_id=row["account_id"], stock_code=row["stock_code"], side=row["side"],
        volume=volume, price_type=row["price_type"] or "peer", sell_all=sell_all,
        strategy_name="conditional_order",
        remark="triggered:%s#%s" % (row["trigger_type"], order_id),
        trade_mode=row["trade_mode"] or "",
    )

    if result.get("ok"):
        store.mark_triggered(order_id, result.get("order_sys_id"), result.get("message") or "")
        _notify("%s触发：%s" % (label, row["stock_code"]),
                "%s，已下单 %s%s，委托号 %s"
                % (condition_text, volume, unit, result.get("order_sys_id")))
        ws_hub.publish(row["account_id"], "conditional_order",
                      dict(row, status="triggered", order_sys_id=result.get("order_sys_id")))
        return

    message = result.get("message") or "下单失败"
    notified = _should_notify_retry(row.get("last_notified_at"))
    store.release_claim(order_id)
    store.record_retry_failure(order_id, message, notified=notified)
    if notified:
        _notify("%s触发但下单失败：%s" % (label, row["stock_code"]),
                "%s，但下单被拒：%s（条件仍成立，会继续重试）" % (condition_text, message))
    ws_hub.publish(row["account_id"], "conditional_order",
                  dict(row, status="retry_failed", message=message))


def check_once():
    """扫一遍所有 active 条件单。返回 (active_count, fired_count)。

    行情不可用时直接返回——不能拿猜的价格判断该不该下单，即便这意味着这一轮
    什么都没检查。
    """
    rows = store.list_active()
    if not rows or not bridge_market.available():
        return len(rows), 0

    codes = sorted({row["stock_code"] for row in rows})
    ticks = bridge_market.get_ticks(codes)

    # 涨停破板/涨停买入需要当天涨停价，get_ticks 的批量快照里没有这个字段，
    # 按代码各查一次 get_instrument_detail（跟下单前的价格笼子走的是同一条
    # FormulaServer 快路径）。只查真的用得到的那些代码，不用得到就不查。
    dynamic_codes = sorted({row["stock_code"] for row in rows
                           if row["compare"] in _DYNAMIC_COMPARE_MODES})
    up_stops = {code: bridge_market.get_instrument_detail(code).get("UpStopPrice")
               for code in dynamic_codes}

    fired = 0
    for row in rows:
        tick = ticks.get(instruments.normalize_code(row["stock_code"])) or {}
        up_stop = up_stops.get(row["stock_code"])
        met, reference_price = _condition_met(row, tick, up_stop)
        if not met:
            continue
        try:
            _fire(row, reference_price)
        except Exception as e:
            print(f"[条件单] 触发处理异常 (id={row['id']}): {e}")
        fired += 1
    return len(rows), fired


def run_loop():
    while not _stop_event.is_set():
        try:
            if _is_trading_session():
                active_count, _ = check_once()
            else:
                active_count = len(store.list_active())
        except Exception as e:
            print(f"[条件单] 检查出错: {e}")
            active_count = 0
        _stop_event.wait(_INTERVAL_ACTIVE_SECONDS if active_count else _INTERVAL_IDLE_SECONDS)


def start():
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=run_loop, daemon=True, name="conditional-order-engine")
    _thread.start()


def stop():
    _stop_event.set()
