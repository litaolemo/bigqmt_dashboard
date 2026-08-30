"""可转债估值指标。纯计算，没有 IO，好测。

    转股价值 = 100 / 转股价 × 正股价        （每张面值 100 元）
    转股溢价率 = (债券价 - 转股价值) / 转股价值 × 100%
    双低 = 债券价 + 溢价率数值              （越低越有性价比，市场惯用口径）

强赎进度单独算：多数条款是「连续 30 个交易日中至少 15 日收盘价 ≥ 转股价 × 130%」，
所以看的是最近 30 个交易日里满足条件的天数，而不是连续天数。
"""

DEFAULT_PAR_VALUE = 100.0           # 可转债面值
DEFAULT_REDEEM_RATIO = 1.3          # 强赎触发价 = 转股价 × 130%
DEFAULT_REDEEM_WINDOW = 30          # 观察窗口（交易日）
DEFAULT_REDEEM_HITS = 15            # 窗口内需满足的天数


def conversion_value(stock_price, conversion_price, par_value=DEFAULT_PAR_VALUE):
    """转股价值：一张债转成股票后值多少钱。"""
    try:
        stock_price = float(stock_price)
        conversion_price = float(conversion_price)
    except (TypeError, ValueError):
        return None
    if conversion_price <= 0 or stock_price <= 0:
        return None
    return round(par_value / conversion_price * stock_price, 4)


def premium_rate(bond_price, conv_value):
    """转股溢价率，百分数。转股价值为 0 时无意义，返回 None。"""
    try:
        bond_price = float(bond_price)
        conv_value = float(conv_value)
    except (TypeError, ValueError):
        return None
    if conv_value <= 0 or bond_price <= 0:
        return None
    return round((bond_price - conv_value) / conv_value * 100, 4)


def double_low(bond_price, premium):
    """双低 = 债券价 + 溢价率。低价 + 低溢价，越小越好。"""
    try:
        return round(float(bond_price) + float(premium), 4)
    except (TypeError, ValueError):
        return None


def redeem_trigger_price(conversion_price, ratio=DEFAULT_REDEEM_RATIO):
    """强赎触发价。"""
    try:
        conversion_price = float(conversion_price)
    except (TypeError, ValueError):
        return None
    return round(conversion_price * ratio, 4) if conversion_price > 0 else None


def redeem_progress(stock_closes, conversion_price, ratio=DEFAULT_REDEEM_RATIO,
                    window=DEFAULT_REDEEM_WINDOW, hits_needed=DEFAULT_REDEEM_HITS):
    """强赎进度。

    stock_closes 按时间升序传入正股收盘价；只看最后 window 个交易日。
    返回 {trigger_price, hits, hits_needed, window, days_counted, triggered,
    ratio_done, known}。

    triggered 在数据不足时一定是 False —— 宁可漏报也不能凭不完整的数据吓人。
    但「没数据」和「0 次命中」是两回事：前者是不知道，后者是确定安全。
    known=False 表示压根没算成，调用方必须显示成「—」而不是 0/15。
    """
    trigger = redeem_trigger_price(conversion_price, ratio)
    result = {
        "trigger_price": trigger,
        "hits": 0,
        "hits_needed": hits_needed,
        "window": window,
        "days_counted": 0,
        "triggered": False,
        "ratio_done": 0.0,
        "known": False,
        "reason": "",
    }
    if trigger is None:
        result["reason"] = "缺转股价"
        return result

    closes = []
    for value in (stock_closes or []):
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            closes.append(price)
    recent = closes[-window:]
    result["days_counted"] = len(recent)
    if not recent:
        result["reason"] = "无正股日线数据"
        return result

    hits = sum(1 for price in recent if price >= trigger)
    result["hits"] = hits
    result["ratio_done"] = round(hits / hits_needed, 4) if hits_needed else 0.0
    result["triggered"] = hits >= hits_needed
    # 日线不足一个完整窗口时，命中数是真的，但「未触发」这个结论还不牢靠。
    result["known"] = True
    if len(recent) < window:
        result["reason"] = "日线仅 %d 根，不足 %d 个交易日的观察窗口" % (len(recent), window)
    return result


def evaluate(bond_price, stock_price, conversion_price, stock_closes=None,
             par_value=DEFAULT_PAR_VALUE):
    """一次算齐一只转债的全部指标。任何一项算不出就是 None，不抛异常。"""
    conv = conversion_value(stock_price, conversion_price, par_value)
    premium = premium_rate(bond_price, conv)
    return {
        "conversion_price": _safe_float(conversion_price),
        "stock_price": _safe_float(stock_price),
        "bond_price": _safe_float(bond_price),
        "conversion_value": conv,
        "premium_rate": premium,
        "double_low": double_low(bond_price, premium) if premium is not None else None,
        "redeem": redeem_progress(stock_closes, conversion_price),
    }


def _safe_float(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
