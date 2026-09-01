# -*- coding: utf-8 -*-
"""买卖指令类型（passorder 的 opType）。

opType 决定的不只是买还是卖，而是「这一笔到底是什么业务」：

    普通账户   23 买入            24 卖出
    信用账户   33 担保品买入      34 担保品卖出
               27 融资买入        28 融券卖出
               29 买券还券        31 卖券还款

同一个「买」在普通账户和信用账户上是两条不同的指令。把信用账户的担保品买入当成
23 发出去，交易所收到的是一笔真实但业务类型不同的单 —— 和 xtquant_big_convert
issue #103（融资买入被映射成普通买入）是同一类错误，比直接报错更糟。

出处：https://dict.thinktrader.net/innerApi/enum_constants.html?id=NF25nX
（docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md 10.1 节是抄录）

**关于送给桥接层的数值。** xtquant_big_convert 的入参约定是混的：融资融券类走
xtconstant 的 order_type（27/28/29/31，桥接层再翻成同名 opType），期货和 ETF
期权类直接透传 passorder 的 opType。担保品买卖在 xtconstant 里没有独立值
（CREDIT_BUY == STOCK_BUY == 23），所以只能按期货那条路走 —— 直接送 33/34。

好处是失败方式安全：QMT 里部署的桥接层若还不认 33/34，会明确报
「order_type 33 is not recognised by the package deployed in QMT」而拒单，
不会悄悄发出一笔普通买入。
"""

ACCOUNT_STOCK = "STOCK"
ACCOUNT_CREDIT = "CREDIT"

# (key, 名称, 适用账户类型, 买入 order_type, 卖出 order_type, 买入名, 卖出名, 说明)
_TABLE = (
    # 普通买卖两种账户都给 —— 23/24 本身是合法的 opType，信用账户能不能用
    # 看券商。信用账户的**默认**仍是担保品，但不把普通买卖从列表里拿掉。
    ("normal", "普通买卖", (ACCOUNT_STOCK, ACCOUNT_CREDIT), 23, 24, "买入", "卖出",
     "股票 / ETF / 可转债的普通买卖"),
    ("collateral", "担保品买卖", (ACCOUNT_CREDIT,), 33, 34, "担保品买入", "担保品卖出",
     "信用账户用自有资金买卖，不产生负债"),
    ("margin", "融资融券", (ACCOUNT_CREDIT,), 27, 28, "融资买入", "融券卖出",
     "借钱买 / 借券卖，产生负债"),
    ("repay", "还券还款", (ACCOUNT_CREDIT,), 29, 31, "买券还券", "卖券还款",
     "买入用于还券，卖出用于还款"),
)

BY_KEY = {row[0]: row for row in _TABLE}

# 账户类型没配或配了个不认识的值时按普通账户走 —— 绝大多数是普通账户，
# 而且普通账户发 23/24 是对的。信用账户必须显式配 CREDIT。
DEFAULT_MODE = {ACCOUNT_STOCK: "normal", ACCOUNT_CREDIT: "collateral"}

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
