"""品种识别与下单规整（最小数量、步进、价格精度、T+0）。

**不要复用桥接层的 bigqmt_signal_trader.code_utils。** 它的 min_lot() 只认
「688 开头 = 200，其余 = 100」：
  * 可转债最小 10 张，走它会被 (10 // 100) * 100 规整成 0，单子直接废掉；
  * 科创板 200 股以上是按 1 股递增的，它会把 250 股砍成 200；
  * ETF 报价 3 位小数，它根本不管价格精度。
本项目要完整交易可转债，所以规则在这里重写一份。

代码段划分（沪深两市现行规则）：
    可转债/可交换债  SH 110/111/113/118/132  SZ 12x        10 张起，步进 10，0.001
    ETF / LOF        SH 5xxxxx               SZ 15/16/18   100 份起，步进 100，0.001
    科创板           SH 688/689                            200 股起，步进 1，0.01
    北交所           BJ 4/8/920                            100 股起，步进 1，0.01
    普通 A 股        其余                                   100 股起，步进 100，0.01
"""

KIND_STOCK = "stock"
KIND_STAR = "star"          # 科创板
KIND_BJ = "bj"              # 北交所
KIND_ETF = "etf"            # ETF / LOF
KIND_BOND = "bond"          # 可转债 / 可交换债

# 单位：可转债论「张」，基金论「份」，股票论「股」。前端展示要用对。
UNIT_BY_KIND = {
    KIND_STOCK: "股", KIND_STAR: "股", KIND_BJ: "股",
    KIND_ETF: "份", KIND_BOND: "张",
}

_BOND_SH_PREFIXES = ("110", "111", "113", "118", "132", "100")
_BOND_SZ_PREFIXES = ("12",)
_ETF_SZ_PREFIXES = ("15", "16", "18")


def split_code(stock_code):
    """拆成 (数字部分, 交易所后缀)。无后缀时按代码段推断，转债不会被误判到深市。

    桥接层的 normalize_stock_code() 对裸 6 位码按「5/6 开头 → SH」判断，
    沪市转债 110xxx / 113xxx 会被错判成 SZ，所以这里自己判。
    """
    raw = str(stock_code or "").strip().upper()
    if not raw:
        return "", ""
    if "." in raw:
        num, _, suffix = raw.partition(".")
        return num.strip(), suffix.strip()
    if raw.startswith(("SH", "SZ", "BJ")) and raw[2:].isdigit():
        return raw[2:], raw[:2]
    if not raw.isdigit():
        return raw, ""
    if raw.startswith(_BOND_SH_PREFIXES) or raw.startswith(("5", "6")):
        return raw, "SH"
    if raw.startswith(("4", "8", "920")):
        return raw, "BJ"
    return raw, "SZ"


def normalize_code(stock_code):
    """规整成带后缀的 QMT 代码（600000.SH）。无法识别时原样返回。"""
    num, suffix = split_code(stock_code)
    if not num:
        return ""
    return "%s.%s" % (num, suffix) if suffix else num


def instrument_kind(stock_code):
    """判定品种。纯代码段规则，离线可用。

    有大QMT 连接时优先用 xtdata.get_instrument_type()（见 bridge.market），
    这里是兜底，也是没连接时前端展示的依据。
    """
    num, suffix = split_code(stock_code)
    if not num:
        return KIND_STOCK
    if suffix == "SH":
        if num.startswith(_BOND_SH_PREFIXES):
            return KIND_BOND
        if num.startswith(("688", "689")):
            return KIND_STAR
        if num.startswith("5"):
            return KIND_ETF
        return KIND_STOCK
    if suffix == "SZ":
        if num.startswith(_BOND_SZ_PREFIXES):
            return KIND_BOND
        if num.startswith(_ETF_SZ_PREFIXES):
            return KIND_ETF
        return KIND_STOCK
    if suffix == "BJ":
        return KIND_BJ
    return KIND_STOCK


def is_convertible_bond(stock_code):
    return instrument_kind(stock_code) == KIND_BOND


def is_t0(stock_code):
    """当日可回转交易。可转债 T+0，其余 A 股品种 T+1。"""
    return instrument_kind(stock_code) == KIND_BOND


def unit_name(stock_code):
    return UNIT_BY_KIND.get(instrument_kind(stock_code), "股")


def min_volume(stock_code):
    """最小申报数量。"""
    kind = instrument_kind(stock_code)
    if kind == KIND_BOND:
        return 10
    if kind == KIND_STAR:
        return 200
    return 100


def volume_step(stock_code):
    """最小申报数量之上的递增单位。

    科创板和北交所是「起点之上按 1 递增」，不是整手递增——按 100 取整会白白丢股数。
    """
    kind = instrument_kind(stock_code)
    if kind == KIND_BOND:
        return 10
    if kind in (KIND_STAR, KIND_BJ):
        return 1
    return 100


def price_tick(stock_code):
    """最小报价单位。可转债和基金都是 3 位小数，股票 2 位。"""
    kind = instrument_kind(stock_code)
    return 0.001 if kind in (KIND_BOND, KIND_ETF) else 0.01


def price_decimals(stock_code):
    return 3 if price_tick(stock_code) < 0.01 else 2


def round_volume(stock_code, volume, sell_all=False):
    """把意向数量规整为可申报数量；不足最小申报量返回 0。

    sell_all=True 用于清仓：零股必须一次性卖出，此时不做任何规整。
    """
    try:
        value = int(volume or 0)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    if sell_all:
        return value
    floor = min_volume(stock_code)
    if value < floor:
        return 0
    step = volume_step(stock_code)
    if step <= 1:
        return value
    return floor + ((value - floor) // step) * step


def round_price(stock_code, price):
    """把价格对齐到最小报价单位，避免因精度被交易所拒单。"""
    try:
        value = float(price or 0)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return round(value, price_decimals(stock_code))


def describe(stock_code):
    """给前端下单弹窗用：单位、步进、精度一次给全。"""
    kind = instrument_kind(stock_code)
    return {
        "code": normalize_code(stock_code),
        "kind": kind,
        "unit": UNIT_BY_KIND.get(kind, "股"),
        "min_volume": min_volume(stock_code),
        "volume_step": volume_step(stock_code),
        "price_tick": price_tick(stock_code),
        "price_decimals": price_decimals(stock_code),
        "is_t0": is_t0(stock_code),
    }
