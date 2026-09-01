"""价格笼子：限价申报的有效价格范围。

交易所对限价单有「有效竞价范围」，超出范围的申报会被直接废单。两个时段口径不同：

    连续竞价   基准价 ± 2%     基准价取最新成交价
    集合竞价   基准价 ± 10%    基准价取昨收价（开盘前还没有成交价）

再往外还有一层硬边界：涨跌停价。笼子算出来的范围要和涨跌停取交集 —— 笼子可以
比涨跌停宽（低价股 2% 可能不到一个价位），但报到涨跌停之外一定是废单。

口径按主板写。科创板/创业板的涨跌幅是 20%、集合竞价有效范围也更宽，真要精确到
每个板还得按板分档 —— 这里把两个比例做成参数，`config/pricecage.json` 可以覆盖，
默认值就是用户要的 2% / 10%。

纯计算，不联网、不碰数据库，好测。
"""

from datetime import time as _time

import settings
from bridge import instruments

# 默认笼子宽度
CONTINUOUS_BAND = 0.02      # 连续竞价 ±2%
AUCTION_BAND = 0.10         # 集合竞价 ±10%

SESSION_AUCTION = "auction"          # 集合竞价
SESSION_CONTINUOUS = "continuous"    # 连续竞价
SESSION_CLOSED = "closed"            # 非交易时段

# 集合竞价：09:15-09:25 开盘、14:57-15:00 收盘。
# 09:25-09:30 是撮合与休整，不能报单，归到 closed。
_OPEN_AUCTION = (_time(9, 15), _time(9, 25))
_CLOSE_AUCTION = (_time(14, 57), _time(15, 0))
_MORNING = (_time(9, 30), _time(11, 30))
_AFTERNOON = (_time(13, 0), _time(14, 57))


def _bands():
    """笼子宽度，可被 config/pricecage.json 覆盖。"""
    cfg = settings.load_json("pricecage")
    try:
        continuous = float(cfg.get("continuous_band") or CONTINUOUS_BAND)
    except (TypeError, ValueError):
        continuous = CONTINUOUS_BAND
    try:
        auction = float(cfg.get("auction_band") or AUCTION_BAND)
    except (TypeError, ValueError):
        auction = AUCTION_BAND
    return continuous, auction


def session_of(now):
    """当前处于哪个竞价时段。传入 datetime。"""
    clock = now.time()
    if _in(clock, _OPEN_AUCTION) or _in(clock, _CLOSE_AUCTION):
        return SESSION_AUCTION
    if _in(clock, _MORNING) or _in(clock, _AFTERNOON):
        return SESSION_CONTINUOUS
    return SESSION_CLOSED


def _in(clock, window):
    start, end = window
    return start <= clock < end


def band_of(session):
    """该时段的笼子宽度。非交易时段按连续竞价口径给，便于盘前预估。"""
    continuous, auction = _bands()
    return auction if session == SESSION_AUCTION else continuous


def base_price_of(session, last_price, last_close):
    """基准价。

    连续竞价用最新成交价；集合竞价开盘前还没有成交价，用昨收。取不到就退回另一个，
    两个都没有则返回 None —— 这时不做笼子判断，不能拿猜的基准价拦用户的单。
    """
    last_price = _positive(last_price)
    last_close = _positive(last_close)
    if session == SESSION_AUCTION:
        return last_close or last_price
    return last_price or last_close


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def compute(stock_code, side, base_price, session, up_stop=None, down_stop=None,
            band=None):
    """算出该品种在该时段的限价有效范围。

    返回 {session, band, base_price, low, high, source}；basic 信息不足时
    low/high 为 None，调用方据此跳过校验而不是瞎拦。

    上下界会和涨跌停取交集：笼子在低价股上可能比一个最小价位还窄，而涨跌停是
    交易所的硬边界，两者都要满足。
    """
    band = band_of(session) if band is None else float(band)
    base = _positive(base_price)
    result = {
        "session": session,
        "band": band,
        "base_price": base,
        "low": None,
        "high": None,
        "up_stop": _positive(up_stop),
        "down_stop": _positive(down_stop),
        "side": side,
    }
    if base is None:
        result["reason"] = "取不到基准价，跳过价格笼子校验"
        return result

    decimals = instruments.price_decimals(stock_code)
    low = round(base * (1 - band), decimals)
    high = round(base * (1 + band), decimals)

    # 和涨跌停取交集
    if result["down_stop"] is not None:
        low = max(low, result["down_stop"])
    if result["up_stop"] is not None:
        high = min(high, result["up_stop"])

    result["low"] = round(low, decimals)
    result["high"] = round(high, decimals)
    return result


def check(stock_code, side, price, cage):
    """价格是否落在笼子里。返回 (ok, message)。

    算不出范围时一律放行 —— 宁可让交易所去拒，也不能凭猜的基准价拦下用户的单。
    """
    price = _positive(price)
    if price is None:
        return False, "限价申报必须给出有效价格"
    low, high = cage.get("low"), cage.get("high")
    if low is None or high is None:
        return True, cage.get("reason", "")

    unit = "元"
    if price < low:
        return False, ("%s 低于有效申报下限 %.3f（%s，基准价 %.3f ±%.0f%%）"
                       % (_fmt(price, stock_code), low, _session_label(cage["session"]),
                          cage["base_price"], cage["band"] * 100))
    if price > high:
        return False, ("%s 高于有效申报上限 %.3f（%s，基准价 %.3f ±%.0f%%）"
                       % (_fmt(price, stock_code), high, _session_label(cage["session"]),
                          cage["base_price"], cage["band"] * 100))
    return True, ""


def clamp(stock_code, price, cage):
    """把价格夹进笼子里，返回 (价格, 是否被调整)。"""
    price = _positive(price)
    low, high = cage.get("low"), cage.get("high")
    if price is None or low is None or high is None:
        return price, False
    decimals = instruments.price_decimals(stock_code)
    if price < low:
        return round(low, decimals), True
    if price > high:
        return round(high, decimals), True
    return price, False


def _fmt(price, stock_code):
    return "%.*f" % (instruments.price_decimals(stock_code), price)


def _session_label(session):
    return {
        SESSION_AUCTION: "集合竞价",
        SESSION_CONTINUOUS: "连续竞价",
        SESSION_CLOSED: "非交易时段",
    }.get(session, session)
