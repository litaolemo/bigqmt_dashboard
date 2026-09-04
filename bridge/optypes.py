# -*- coding: utf-8 -*-
"""买卖指令类型（passorder 的 opType）。

opType 决定的不只是买还是卖，而是「这一笔到底是什么业务」：

    23 买入        24 卖出        股票 / ETF / 可转债
    27 融资买入    28 融券卖出    信用账户，产生负债
    29 买券还券    31 卖券还款    信用账户，还账

把融资买入当成普通买入发出去，交易所收到的是一笔真实但业务类型不同的单
（xtquant_big_convert issue #103），比直接报错糟得多。所以这里不猜：
认不出的 trade_mode 一律报错。

出处：https://dict.thinktrader.net/innerApi/enum_constants.html?id=NF25nX
（docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md 10.1 节是抄录）

**担保品买卖（33 / 34）暂不支持。** 它在 xtconstant 里没有独立值
（CREDIT_BUY == STOCK_BUY == 23），要支持得先改 xtquant_big_convert，
让它能直接透传 33/34。在那之前宁可不给这个选项 —— 给了只会退化成普通买入。

现在这张表里的每一条都是当前桥接层已经认得的。
"""

ACCOUNT_STOCK = "STOCK"
ACCOUNT_CREDIT = "CREDIT"

# (key, 名称, 适用账户类型, 买入 order_type, 卖出 order_type, 买入名, 卖出名, 说明)
_TABLE = (
    ("normal", "普通买卖", (ACCOUNT_STOCK, ACCOUNT_CREDIT), 23, 24, "买入", "卖出",
     "股票 / ETF / 可转债的普通买卖"),
    ("margin", "融资融券", (ACCOUNT_CREDIT,), 27, 28, "融资买入", "融券卖出",
     "借钱买 / 借券卖，产生负债"),
    ("repay", "还券还款", (ACCOUNT_CREDIT,), 29, 31, "买券还券", "卖券还款",
     "买入用于还券，卖出用于还款"),
)

BY_KEY = {row[0]: row for row in _TABLE}

# 反查表：某个已经落地成交/委托的 order_type 是买还是卖。表里的普通买卖(23/24)
# 之外，信用账户的融资买入(27)/买券还券(29) 也是买方向，融券卖出(28)/卖券还款(31)
# 是卖方向——成交回报里的 order_type 就是这几个数之一，不会是别的。
_BUY_OP_TYPES = frozenset(row[3] for row in _TABLE)
_SELL_OP_TYPES = frozenset(row[4] for row in _TABLE)

# 两种账户的默认都是普通买卖：默认不能是会产生负债的那一条。
# 账户类型没配或配了个不认识的值时按普通账户走，信用账户必须显式配 CREDIT。
DEFAULT_MODE = {ACCOUNT_STOCK: "normal", ACCOUNT_CREDIT: "normal"}

SIDE_BUY = "buy"
SIDE_SELL = "sell"


def normalize_account_type(account_type):
    text = str(account_type or "").strip().upper()
    return text if text in (ACCOUNT_STOCK, ACCOUNT_CREDIT) else ACCOUNT_STOCK


def _as_dict(row, side=None):
    """side 给了才带 side_label。

    列选项时方向还没定（同一个弹窗既可能买也可能卖），默认给一个
    “担保品买入”会直接显在卖出弹窗上 —— 宁可不给，让调用方拿
    buy_label / sell_label 自己选。
    """
    key, label, account_types, buy, sell, buy_label, sell_label, hint = row
    spec = {
        "value": key,
        "label": label,
        "account_types": list(account_types),
        "buy_order_type": buy,
        "sell_order_type": sell,
        "buy_label": buy_label,
        "sell_label": sell_label,
        "hint": hint,
    }
    if side is not None:
        spec["side_label"] = sell_label if side == SIDE_SELL else buy_label
    return spec


def default_mode_for(account_type):
    return DEFAULT_MODE[normalize_account_type(account_type)]


def choices_for(account_type, side=None):
    """该账户类型能用的交易类型。普通账户只有一条，前端可以据此不显示选择器。"""
    kind = normalize_account_type(account_type)
    return [_as_dict(row, side) for row in _TABLE if kind in row[2]]


def resolve(trade_mode, account_type, side):
    """(trade_mode, 账户类型, 方向) -> (送给桥接层的 order_type, 该类型的定义)。

    认不出来就抛 ValueError。业务类型是猜不得的：猜错发出去的是一笔真单。
    """
    if side not in (SIDE_BUY, SIDE_SELL):
        raise ValueError("方向只能是 buy 或 sell，收到 %r" % (side,))
    kind = normalize_account_type(account_type)
    key = str(trade_mode or "").strip().lower() or default_mode_for(kind)
    row = BY_KEY.get(key)
    if row is None:
        raise ValueError("不支持的交易类型: %r" % (trade_mode,))
    if kind not in row[2]:
        raise ValueError(
            "%s 只适用于 %s 账户，当前账户是 %s"
            % (row[1], "/".join(row[2]), kind))
    order_type = row[3] if side == SIDE_BUY else row[4]
    return order_type, _as_dict(row, side)


def side_of(order_type):
    """已经落地的 order_type（成交/委托回报里的那个数）是买还是卖。

    认不出来（不在这张表里）返回 ""——不猜，交给调用方决定怎么显示未知方向。
    """
    try:
        value = int(order_type)
    except (TypeError, ValueError):
        return ""
    if value in _BUY_OP_TYPES:
        return SIDE_BUY
    if value in _SELL_OP_TYPES:
        return SIDE_SELL
    return ""
