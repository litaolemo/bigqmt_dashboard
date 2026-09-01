"""行情读取，全局共用一条大QMT 连接。

行情跟账号无关，让 N 个账号各自订阅一遍全市场纯属浪费带宽和 QMT 主线程时间，
所以固定挑一个账号的连接当「行情主连接」（bridge.config.quote_account_id）。

直连之后这里取代了改造前的四条外部行情路径（akshare / 新浪 / 东财+KDL 隧道代理 /
tushare pro.daily）—— 那些现在退居为拿不到大QMT 时的兜底。
"""

import threading

from bridge import config as bridge_config
from bridge import instruments
from bridge import pool as bridge_pool

_SUBSCRIPTIONS = {}     # seq -> code_list
_LOCK = threading.Lock()
_INSTRUMENT_KIND_CACHE = {}


def quote_handle():
    """行情主连接的 xtdata 句柄。没有可用账号时抛 BridgeUnavailable。"""
    account_id = bridge_config.quote_account_id()
    if not account_id:
        raise bridge_pool.BridgeUnavailable("没有可用的大QMT 账号，行情不可用")
    return bridge_pool.get_xtdata(account_id)


def available():
    try:
        quote_handle()
        return True
    except bridge_pool.BridgeUnavailable:
        return False


def get_ticks(codes, timeout_seconds=None):
    """批量取五档快照。返回 {code: tick_dict}；不可用时返回 {}。

    行情缺失绝不能把页面打成 500 —— 面板即使没有实时价也要能显示持仓。
    """
    wanted = [instruments.normalize_code(c) for c in (codes or []) if c]
    if not wanted:
        return {}
    try:
        return quote_handle().get_full_tick(wanted, timeout_seconds=timeout_seconds) or {}
    except bridge_pool.BridgeUnavailable:
        return {}
    except Exception as e:
        print(f"[行情] get_full_tick 失败: {e}")
        return {}


# 给 UI 用的取价超时。下单弹窗要的是「大概多少钱」，等十几秒还不如直接显示「—」；
# 真正下单走的是另一条路径，用账号自己的 timeout_seconds。
UI_QUOTE_TIMEOUT_SECONDS = 3.0


def last_price(code, timeout_seconds=UI_QUOTE_TIMEOUT_SECONDS):
    """单只最新价；取不到返回 None。默认短超时，不让界面卡住。"""
    tick = get_ticks([code], timeout_seconds=timeout_seconds).get(
        instruments.normalize_code(code)) or {}
    value = tick.get("lastPrice") or tick.get("last_price")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _bar_count(frame):
    """一只标的返回了多少根 K 线。解析不了按 0 算。"""
    if frame is None:
        return 0
    try:
        return len(frame.to_dict("index") if hasattr(frame, "to_dict") else frame)
    except Exception:
        return 0


def download_history(codes, period="1m", start_time="", end_time=""):
    """让大QMT 把历史数据下载到它本地。返回成功与否。

    xtdata 的读取语义是「读本地库」：没 download 过的标的，get_market_data_ex
    会返回 0 根而不是报错。实测账号持仓的 10 只票全都没有 1m 数据（日线有），
    所以缺数据时必须主动补下载。
    """
    wanted = [instruments.normalize_code(c) for c in (codes or []) if c]
    if not wanted:
        return False
    try:
        result = quote_handle().download_history_data2(
            wanted, period, start_time, end_time)
        print(f"[行情] 已向大QMT 下载 {period} 数据: {result}")
        return True
    except bridge_pool.BridgeUnavailable:
        return False
    except Exception as e:
        print(f"[行情] download_history_data2 失败: {e}")
        return False


def price_reference(code):
    """算价格笼子要的四个数：最新价、昨收、涨停、跌停。

    最新价和昨收来自快照，涨跌停来自合约详情（走 FormulaServer 直连快速路径，
    比 RPC 快一个量级）。取不到的项是 None，调用方据此跳过校验。
    """
    normalized = instruments.normalize_code(code)
    reference = {"code": normalized, "last_price": None, "last_close": None,
                 "up_stop": None, "down_stop": None}

    tick = (get_ticks([normalized], timeout_seconds=UI_QUOTE_TIMEOUT_SECONDS)
            or {}).get(normalized) or {}
    reference["last_price"] = _positive(tick.get("lastPrice") or tick.get("last_price"))
    reference["last_close"] = _positive(tick.get("lastClose") or tick.get("last_close"))

    detail = get_instrument_detail(normalized) or {}
    reference["up_stop"] = _positive(detail.get("UpStopPrice"))
    reference["down_stop"] = _positive(detail.get("DownStopPrice"))
    if reference["last_close"] is None:
        # 合约详情里的 SettlementPrice 就是昨收，快照拿不到时用它兜底
        reference["last_close"] = _positive(detail.get("SettlementPrice"))
    return reference


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def get_minute_bars(codes, count=241, period="1m", field_list=None,
                    download_if_missing=True):
    """拉 K 线。缺数据的标的先让大QMT 下载再重试一次。

    这一条取代了改造前那套「服务端下发 backfill_kline 指令 → QMT 客户端拉分钟线 →
    再 push 回服务端」的往返。现在直接问大QMT 要。

    download_if_missing=False 用于重试路径本身，避免无限套娃。
    """
    wanted = [instruments.normalize_code(c) for c in (codes or []) if c]
    if not wanted:
        return {}
    fields = field_list or ["time", "open", "high", "low", "close", "volume"]
    try:
        data = quote_handle().get_market_data_ex(
            fields, wanted, period=period, count=count) or {}
    except bridge_pool.BridgeUnavailable:
        return {}
    except Exception as e:
        print(f"[行情] get_market_data_ex 失败: {e}")
        return {}

    if not download_if_missing:
        return data

    missing = [c for c in wanted if _bar_count(data.get(c)) == 0]
    if not missing:
        return data

    print(f"[行情] {len(missing)}/{len(wanted)} 只标的本地无 {period} 数据，先下载")
    if not download_history(missing, period=period):
        return data
    retried = get_minute_bars(missing, count=count, period=period,
                              field_list=fields, download_if_missing=False)
    data.update({c: f for c, f in (retried or {}).items() if _bar_count(f) > 0})
    return data


def get_instrument_detail(code):
    """合约详情（含转债的转股价等字段，具体字段随 QMT 版本而异）。"""
    try:
        return quote_handle().get_instrument_detail(instruments.normalize_code(code)) or {}
    except bridge_pool.BridgeUnavailable:
        return {}
    except Exception as e:
        print(f"[行情] get_instrument_detail({code}) 失败: {e}")
        return {}


def instrument_kind(code):
    """品种判定：优先问大QMT，失败退回代码段规则。结果按代码缓存。

    代码段规则（bridge.instruments）离线可用且覆盖绝大多数情况，大QMT 的
    get_instrument_type 只是用来兜住新代码段和特殊品种。
    """
    normalized = instruments.normalize_code(code)
    if not normalized:
        return instruments.KIND_STOCK
    with _LOCK:
        cached = _INSTRUMENT_KIND_CACHE.get(normalized)
    if cached:
        return cached
    kind = instruments.instrument_kind(normalized)
    try:
        types = quote_handle().get_instrument_type(normalized) or {}
        if isinstance(types, dict):
            if types.get("bond") or types.get("convertiblebond"):
                kind = instruments.KIND_BOND
            elif types.get("fund") or types.get("etf"):
                kind = instruments.KIND_ETF
    except bridge_pool.BridgeUnavailable:
        pass
    except Exception as e:
        print(f"[行情] get_instrument_type({normalized}) 失败，用代码段规则: {e}")
    with _LOCK:
        _INSTRUMENT_KIND_CACHE[normalized] = kind
    return kind


def subscribe_whole_quote(codes, callback):
    """订阅推送行情。返回订阅号；不可用时返回 None。"""
    wanted = [instruments.normalize_code(c) for c in (codes or []) if c]
    if not wanted:
        return None
    try:
        seq = quote_handle().subscribe_whole_quote(wanted, callback=callback)
    except bridge_pool.BridgeUnavailable as e:
        print(f"[行情] 订阅失败: {e}")
        return None
    except Exception as e:
        print(f"[行情] subscribe_whole_quote 失败: {e}")
        return None
    with _LOCK:
        _SUBSCRIPTIONS[seq] = wanted
    print(f"[行情] 已订阅 {len(wanted)} 个代码 (seq={seq})")
    return seq


def unsubscribe(seq):
    if seq is None:
        return
    try:
        quote_handle().unsubscribe_quote(seq)
    except Exception as e:
        print(f"[行情] 退订 {seq} 失败: {e}")
    with _LOCK:
        _SUBSCRIPTIONS.pop(seq, None)


def unsubscribe_all():
    with _LOCK:
        seqs = list(_SUBSCRIPTIONS)
    for seq in seqs:
        unsubscribe(seq)
