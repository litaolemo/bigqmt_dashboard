"""把参考数据、实时行情、指标算式拼成前端要的转债视图。

数据来源分三层，缺哪层降哪层，绝不因为某一层不可用就整块空掉：
    转股价   cb.reference（akshare 一览表 + 比价表补缺）→ 大QMT 合约详情兜底
    实时价   大QMT 行情（转债价 + 正股价）
    强赎进度 正股日线，最近 30 个交易日（日更缓存，盘中不重复算）

转股价缺失时 conversion_value / premium_rate / double_low 一律返回 None，
前端显示「—」。宁可空着，也不要拿错的转股价算出一个看起来像模像样的溢价率。
"""

import threading
from datetime import datetime

from bridge import instruments
from bridge import market as bridge_market
from cb import metrics
from cb import reference

_REDEEM_CACHE = {}          # bond_code -> {"date": "YYYY-MM-DD", "value": {...}}
_LOCK = threading.RLock()


def conversion_price_of(bond_code):
    """转股价。参考数据里没有就问大QMT 的合约详情要。"""
    row = reference.get(bond_code) or {}
    price = row.get("conversion_price")
    if price and price > 0:
        return float(price)

    detail = bridge_market.get_instrument_detail(bond_code) or {}
    for key in ("ConversionPrice", "conversionPrice", "conversion_price",
                "ExercisePrice", "exercise_price"):
        try:
            value = float(detail.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _stock_daily_closes(stock_code, count=40):
    """正股最近的日收盘价，升序。取不到返回 []。"""
    data = bridge_market.get_minute_bars([stock_code], count=count, period="1d",
                                         field_list=["time", "close"])
    bars = data.get(instruments.normalize_code(stock_code))
    if bars is None:
        return []
    try:
        records = bars.to_dict("index") if hasattr(bars, "to_dict") else dict(bars)
    except Exception:
        return []
    closes = []
    for key in sorted(records):
        values = records[key]
        if not isinstance(values, dict):
            continue
        try:
            close = float(values.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if close > 0:
            closes.append(close)
    return closes


def redeem_status(bond_code, conversion_price, stock_code):
    """强赎进度，按天缓存。

    条款看的是「最近 30 个交易日里有多少天收盘 ≥ 转股价×130%」，一天只会变一次，
    没必要每次刷新页面都去拉 30 根日线。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with _LOCK:
        cached = _REDEEM_CACHE.get(bond_code)
        if cached and cached["date"] == today:
            return cached["value"]

    closes = _stock_daily_closes(stock_code) if stock_code else []
    value = metrics.redeem_progress(closes, conversion_price)
    with _LOCK:
        _REDEEM_CACHE[bond_code] = {"date": today, "value": value}
    return value


def bond_view(bond_code, bond_price=None):
    """单只转债的完整视图：参考数据 + 实时价 + 指标 + 强赎。"""
    code = instruments.normalize_code(bond_code)
    row = reference.get(code) or {}
    stock_code = row.get("stock_code") or ""
    conversion_price = conversion_price_of(code)

    # 一次取两只（转债 + 正股）的快照，省一次 RPC
    wanted = [code] + ([stock_code] if stock_code else [])
    ticks = bridge_market.get_ticks(wanted)
    if bond_price is None:
        bond_price = _tick_price(ticks.get(code))
    stock_price = _tick_price(ticks.get(stock_code)) if stock_code else None

    view = {
        "bond_code": code,
        "bond_name": row.get("bond_name") or "",
        "stock_code": stock_code,
        "stock_name": row.get("stock_name") or "",
        "issue_size": row.get("issue_size"),
        "list_date": row.get("list_date") or "",
        "rating": row.get("rating") or "",
        "bond_price": bond_price,
        "stock_price": stock_price,
        "conversion_price": conversion_price,
        "conversion_value": None,
        "premium_rate": None,
        "double_low": None,
        "redeem": None,
        "data_gap": "",
    }

    if conversion_price is None:
        view["data_gap"] = "缺转股价，无法计算溢价率"
        return view
    if stock_price is None:
        view["data_gap"] = "取不到正股实时价"
        return view

    view["conversion_value"] = metrics.conversion_value(stock_price, conversion_price)
    if bond_price is not None:
        view["premium_rate"] = metrics.premium_rate(bond_price, view["conversion_value"])
        if view["premium_rate"] is not None:
            view["double_low"] = metrics.double_low(bond_price, view["premium_rate"])
    view["redeem"] = redeem_status(code, conversion_price, stock_code)
    return view


def _tick_price(tick):
    if not isinstance(tick, dict):
        return None
    for key in ("lastPrice", "last_price", "close"):
        try:
            value = float(tick.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def enrich_positions(positions):
    """给持仓行补上转债字段。非转债原样返回。

    持仓表里股票和转债混着，前端按 is_bond 决定要不要展开转债列。
    """
    enriched = []
    for position in positions or []:
        code = str(position.get("stock_code") or "")
        if not instruments.is_convertible_bond(code):
            position["is_bond"] = False
            enriched.append(position)
            continue
        position["is_bond"] = True
        try:
            position["bond"] = bond_view(code, bond_price=position.get("last_price"))
        except Exception as e:
            print(f"[转债] {code} 指标计算失败: {e}")
            position["bond"] = {"bond_code": code, "data_gap": str(e)}
        enriched.append(position)
    return enriched


def redeem_blocked(bond_code):
    """该转债是否已触发强赎（触发后不宜再新开仓）。

    强赎公告后转债会被按 100 元附近赎回，此时高溢价买入是纯亏钱，所以做成下单闸门。
    数据不足时一律返回 False —— 不能凭不完整的数据挡住用户的单子。
    """
    if not instruments.is_convertible_bond(bond_code):
        return False
    row = reference.get(bond_code) or {}
    conversion_price = conversion_price_of(bond_code)
    if conversion_price is None or not row.get("stock_code"):
        return False
    status = redeem_status(instruments.normalize_code(bond_code),
                           conversion_price, row["stock_code"])
    return bool(status and status.get("triggered"))


def clear_caches():
    with _LOCK:
        _REDEEM_CACHE.clear()
