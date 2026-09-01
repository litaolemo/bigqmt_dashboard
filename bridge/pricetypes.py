"""下单选价类型（passorder 的 prType）。

数值与适用范围来自迅投官方枚举：
https://dict.thinktrader.net/innerApi/enum_constants.html?id=NF25nX
（xtquant_big_convert 的 docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md 10.3 节是抄录）

**必须按交易所过滤。** 市价类指令是分交易所报备的：

    42 / 43   最优五档即成剩撤 / 剩转限价      只有 上交所、北交所
    46 / 47 / 48  即成剩撤 / 最优五档剩撤 / 全额成交或撤   只有 深交所
    44 / 45   对手方最优 / 本方最优            三个交易所都行

给深市票选 42 会被交易所直接拒单。前端下拉里就不该出现不能用的选项 —— 所以
choices_for() 按代码所属交易所过滤。

价格字段有三种语义，不要混：
    指定价(11) / 盘后定价(49)   price 就是委托价，要受价格笼子约束
    市价类(42-48)              price 是**保护限价**，买不高于、卖不低于；0 表示取涨跌停价
    其余档位价                  price 无意义，填 0 占位
"""

from bridge import instruments

# 价格语义
PRICE_ROLE_NONE = "none"            # 不用填
PRICE_ROLE_ORDER = "order"          # 就是委托价，受价格笼子约束
PRICE_ROLE_PROTECT = "protect"      # 保护限价，0 = 涨跌停价

_ALL = ("SH", "SZ", "BJ")

# (key, prType, 名称, 分组, 价格语义, 适用交易所, 说明)
_TABLE = (
    # ---- 常用 ----
    ("latest", 5,  "最新价",   "常用", PRICE_ROLE_NONE,    _ALL,
     "按当前最新成交价报，面板默认"),
    ("fix",    11, "限价",     "常用", PRICE_ROLE_ORDER,   _ALL,
     "自己指定价格，受价格笼子约束"),
    # peer / mine 沿用历史含义（44 / 45），不要改数值 —— 改了就是对既有调用方的
    # 静默行为变更：同一个 'peer' 会从交易所市价指令变成一档限价单。
    ("peer",   44, "对手方最优", "常用", PRICE_ROLE_PROTECT, _ALL,
     "交易所市价指令，按对手方最优价成交，最快"),
    ("mine",   45, "本方最优",   "常用", PRICE_ROLE_PROTECT, _ALL,
     "交易所市价指令，按本方最优价申报，排队等成交"),
    ("stop",   12, "涨跌停价", "常用", PRICE_ROLE_NONE,    _ALL,
     "对手方最远端价格，买用涨停、卖用跌停，几乎必成"),

    # ---- 一档价（QMT 取盘口一档后按限价报，和上面的交易所市价指令是两回事）----
    ("peer_l1", 14, "对手价(一档)", "一档", PRICE_ROLE_NONE, _ALL,
     "买取卖一价、卖取买一价，按限价报出"),
    ("mine_l1", 13, "挂单价(一档)", "一档", PRICE_ROLE_NONE, _ALL,
     "买取买一价、卖取卖一价，按限价报出"),

    # ---- 市价类（分交易所）----
    ("sh_five_cancel", 42, "最优五档即成剩撤", "市价", PRICE_ROLE_PROTECT, ("SH", "BJ"),
     "沪/北：吃五档，剩余撤销"),
    ("sh_five_limit",  43, "最优五档即成转限价", "市价", PRICE_ROLE_PROTECT, ("SH", "BJ"),
     "沪/北：吃五档，剩余转为限价挂单"),
    ("sz_cancel",      46, "即成剩撤",         "市价", PRICE_ROLE_PROTECT, ("SZ",),
     "深：能成多少成多少，剩余撤销"),
    ("sz_five_cancel", 47, "最优五档即成剩撤", "市价", PRICE_ROLE_PROTECT, ("SZ",),
     "深：吃五档，剩余撤销"),
    ("sz_fok",         48, "全额成交或撤销",   "市价", PRICE_ROLE_PROTECT, ("SZ",),
     "深：要么全部成交，要么全撤"),

    # ---- 盘口档位 ----
    ("ask5", 0,  "卖五价", "档位", PRICE_ROLE_NONE, _ALL, ""),
    ("ask4", 1,  "卖四价", "档位", PRICE_ROLE_NONE, _ALL, ""),
    ("ask3", 2,  "卖三价", "档位", PRICE_ROLE_NONE, _ALL, ""),
    ("ask2", 3,  "卖二价", "档位", PRICE_ROLE_NONE, _ALL, ""),
    ("ask1", 4,  "卖一价", "档位", PRICE_ROLE_NONE, _ALL, ""),
    ("bid1", 6,  "买一价", "档位", PRICE_ROLE_NONE, _ALL, ""),
    ("bid2", 7,  "买二价", "档位", PRICE_ROLE_NONE, _ALL, ""),
    ("bid3", 8,  "买三价", "档位", PRICE_ROLE_NONE, _ALL, ""),
    ("bid4", 9,  "买四价", "档位", PRICE_ROLE_NONE, _ALL, ""),
    ("bid5", 10, "买五价", "档位", PRICE_ROLE_NONE, _ALL, ""),

    # ---- 盘后 ----
    ("after_hours", 49, "盘后定价", "盘后", PRICE_ROLE_ORDER, ("SH", "SZ"),
     "科创板/创业板 15:05-15:30 盘后固定价格交易"),
)

BY_KEY = {row[0]: row for row in _TABLE}
BY_PRTYPE = {row[1]: row for row in _TABLE}

DEFAULT_KEY = "latest"
GROUP_ORDER = ("常用", "一档", "市价", "档位", "盘后")


def _as_dict(row):
    key, prtype, label, group, price_role, exchanges, hint = row
    return {
        "value": key,
        "pr_type": prtype,
        "label": label,
        "group": group,
        "price_role": price_role,
        "needs_price": price_role == PRICE_ROLE_ORDER,
        "accepts_price": price_role != PRICE_ROLE_NONE,
        "exchanges": list(exchanges),
        "hint": hint,
    }


def exchange_of(stock_code):
    _, suffix = instruments.split_code(stock_code)
    return suffix or ""


def choices_for(stock_code=""):
    """该标的能用的选价类型，按分组排序。

    代码认不出交易所时给全集 —— 少给选项比给错更烦人，服务端和交易所还会再把关。
    """
    exchange = exchange_of(stock_code)
    rows = [row for row in _TABLE
            if not exchange or exchange in row[5]]
    rows.sort(key=lambda r: (GROUP_ORDER.index(r[3]) if r[3] in GROUP_ORDER else 99,
                             _TABLE.index(r)))
    return [_as_dict(row) for row in rows]


def resolve(price_type, stock_code=""):
    """把前端传来的 key（或直接的 prType 数字）解析成 (prType, 该类型的定义)。

    认不出来就抛 ValueError，让调用方给出明确错误 —— 猜一个报价方式去下单
    是最不该做的事。
    """
    if price_type in (None, ""):
        price_type = DEFAULT_KEY
    row = BY_KEY.get(str(price_type).strip().lower())
    if row is None:
        try:
            row = BY_PRTYPE.get(int(price_type))
        except (TypeError, ValueError):
            row = None
    if row is None:
        raise ValueError("不支持的报价方式: %r" % (price_type,))

    exchange = exchange_of(stock_code)
    if exchange and exchange not in row[5]:
        raise ValueError(
            "%s 只适用于 %s，%s 是 %s 的标的"
            % (row[2], "/".join(row[5]), stock_code, exchange))
    return row[1], _as_dict(row)


def price_role_of(price_type, stock_code=""):
    return resolve(price_type, stock_code)[1]["price_role"]
