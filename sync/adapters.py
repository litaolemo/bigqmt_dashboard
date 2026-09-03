"""把桥接层返回的对象，翻译成 dashboard 落库函数认识的 dict。

两边字段名对不上，而且桥接层不算盈亏，所以必须有这层：

    桥接层 position          dashboard save_positions
    stock_name           →   instrument_name
    price                →   last_price
    (不提供)              →   float_profit / position_profit / profit_rate  这里算

盈亏用 (现价 - 成本价) × 数量 现算。QMT 自己给的持仓盈亏口径各家券商不一，
自己算反而稳定，也和面板上「浮动盈亏」的定义一致。
"""


def _get(obj, name, default=None):
    """CompatObject 用属性访问，Redis 缓存快照是 dict，两种都要吃得下。"""
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def position_to_row(position, account_id):
    """桥接层持仓对象 → save_positions() 需要的 dict。"""
    volume = _i(_get(position, "volume"))
    avg_price = _f(_get(position, "avg_price"))
    last_price = _f(_get(position, "price", _get(position, "last_price")))
    market_value = _f(_get(position, "market_value"), last_price * volume)

    # 桥接层不提供盈亏，这里按成本价现算
    float_profit = (last_price - avg_price) * volume if avg_price > 0 else 0.0
    profit_rate = (last_price - avg_price) / avg_price if avg_price > 0 else 0.0

    return {
        "account_id": account_id,
        "account_type": _i(_get(position, "account_type"), 2),
        "stock_code": str(_get(position, "stock_code", "")),
        "instrument_name": str(_get(position, "stock_name", _get(position, "instrument_name", ""))),
        "volume": volume,
        "can_use_volume": _i(_get(position, "can_use_volume")),
        "frozen_volume": _i(_get(position, "frozen_volume")),
        "on_road_volume": _i(_get(position, "on_road_volume")),
        "yesterday_volume": _i(_get(position, "yesterday_volume"), volume),
        "avg_price": avg_price,
        "open_price": _f(_get(position, "open_price"), avg_price),
        "last_price": last_price,
        "market_value": market_value,
        "float_profit": round(float_profit, 2),
        "position_profit": round(float_profit, 2),
        "profit_rate": round(profit_rate, 6),
        "direction": _i(_get(position, "direction"), 48),
        "open_date": str(_get(position, "open_date", "")),
        "secu_account": str(_get(position, "secu_account", "")),
        "current_change": None,     # 由行情侧回填，见 bridge.market
        "topic_reason": None,
    }


def asset_to_row(asset, account_id):
    """桥接层资产对象 → save_asset() 需要的 dict。

    current_balance / fetch_balance 是老 QMT 推送里的可用余额别名，桥接层没有，
    统一用 cash 填，保证面板上「可用资金」不会变空。
    """
    cash = _f(_get(asset, "cash", _get(asset, "available_cash")))
    return {
        "account_id": account_id,
        "account_type": _i(_get(asset, "account_type"), 2),
        "cash": cash,
        "current_balance": cash,
        "fetch_balance": cash,
        "frozen_cash": _f(_get(asset, "frozen_cash")),
        "market_value": _f(_get(asset, "market_value")),
        "total_asset": _f(_get(asset, "total_asset")),
    }


def trade_to_row(trade, account_id, name_of=None):
    """桥接层成交对象 → save_trades() 需要的 dict。

    direction 和 order_type 都写 23/24：面板的 _trade_side() 和所有统计 SQL
    都是 `direction = 23 OR order_type = 23` 这样两边都认，写齐最稳。

    成交对象不带股票名，用 name_of(code) 回填（通常来自同一轮的持仓快照）。
    """
    order_type = _i(_get(trade, "order_type"))
    stock_code = str(_get(trade, "stock_code", ""))
    name = ""
    if name_of is not None:
        try:
            name = name_of(stock_code) or ""
        except Exception:
            name = ""
    return {
        "account_id": account_id,
        "account_type": 2,
        "stock_code": stock_code,
        "instrument_name": name,
        "direction": order_type,
        "order_type": order_type,
        "offset_flag": None,
        "order_id": _get(trade, "order_id"),
        "order_sysid": str(_get(trade, "order_sysid", "")),
        "order_remark": str(_get(trade, "order_remark", "")),
        "strategy_name": str(_get(trade, "strategy_name", "")),
        # 缺失时给 None 不给 ""：traded_id 是 (account_id, traded_id) 的唯一键
        # 一部分，"" 会让所有取不到编号的成交互相顶掉，NULL 在 SQLite 里互不
        # 冲突，才是「不知道」该有的样子。
        "traded_id": (str(_get(trade, "traded_id", _get(trade, "trade_id", "")))
                      or None),
        "traded_price": _f(_get(trade, "traded_price")),
        "traded_volume": _i(_get(trade, "traded_volume")),
        "traded_amount": _f(_get(trade, "traded_amount")),
        "traded_time": _i(_get(trade, "traded_time")),
        "commission": _f(_get(trade, "commission")),
        "secu_account": str(_get(trade, "secu_account", "")),
    }


def order_to_row(order, account_id, name_of=None):
    """桥接层委托对象 → orders 表的 dict。

    活动委托视图是直连才有的东西：改造前服务端只看得到成交，看不到「挂着没成的单」。
    """
    stock_code = str(_get(order, "stock_code", ""))
    name = ""
    if name_of is not None:
        try:
            name = name_of(stock_code) or ""
        except Exception:
            name = ""
    return {
        "account_id": account_id,
        "stock_code": stock_code,
        "instrument_name": name,
        "order_id": _get(order, "order_id"),
        "order_sysid": str(_get(order, "order_sysid", "")),
        "order_type": _i(_get(order, "order_type")),
        "order_status": _i(_get(order, "order_status")),
        "order_volume": _i(_get(order, "order_volume")),
        "traded_volume": _i(_get(order, "traded_volume")),
        "price": _f(_get(order, "price")),
        "traded_price": _f(_get(order, "traded_price")),
        "order_time": _i(_get(order, "order_time")),
        "strategy_name": str(_get(order, "strategy_name", "")),
        "order_remark": str(_get(order, "order_remark", "")),
        "status_msg": str(_get(order, "status_msg", "")),
    }
