"""下单链路的测试替身。

直连之后下单要真的打到大QMT，测试机上没有 QMT 也不该有。这里把 bridge.pool 的
几个出口换成假的：账号配置、trader、xtconstant 常量。风控闸门、数量规整、审计
落库这些真正的业务逻辑照常跑，只有最后那一步 RPC 是假的。

用法：
    with FakeBridge(["A1"], allow_order=True) as fake:
        ...
        fake.orders          # 记录下来的报单
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge import config as bridge_config
from bridge import pool as bridge_pool


class FakeConstants:
    """xtconstant 里下单用到的那几个值，取自真实 xtquant。"""
    STOCK_BUY = 23
    STOCK_SELL = 24
    FIX_PRICE = 11
    LATEST_PRICE = 5
    MARKET_PEER_PRICE_FIRST = 44

    class XtQuantTraderCallback:
        pass

    @staticmethod
    def StockAccount(account_id, account_type="STOCK"):
        return {"account_id": account_id, "account_type": account_type}


class FakeTrader:
    """记录报单、按剧本返回结果。"""

    def __init__(self, account_id, sink):
        self.account_id = account_id
        self._sink = sink
        self.positions = {}
        self.open_orders = []
        self.next_order_id = 1000
        self.fail_with = None

    # ---- 下单 ----
    def order_stock_result(self, account, stock_code, order_type, order_volume,
                           price_type, price, strategy_name, order_remark):
        if self.fail_with is not None:
            return {"order_sys_id": -1, "message": self.fail_with}
        self.next_order_id += 1
        record = {
            "account_id": self.account_id, "stock_code": stock_code,
            "order_type": order_type, "order_volume": order_volume,
            "price_type": price_type, "price": price,
            "strategy_name": strategy_name, "order_remark": order_remark,
            "order_sys_id": str(self.next_order_id),
        }
        self._sink.append(record)
        return {"order_sys_id": record["order_sys_id"]}

    def cancel_order_stock(self, account, order_id):
        self._sink.append({"account_id": self.account_id, "cancel": order_id})
        return 0

    def cancel_order_stock_sysid(self, account, market, order_sysid):
        self._sink.append({"account_id": self.account_id, "cancel_sysid": order_sysid})
        return 0

    # ---- 查询 ----
    def query_stock_position(self, account, stock_code):
        return self.positions.get(stock_code)

    def query_stock_positions(self, account):
        return list(self.positions.values())

    def query_stock_orders(self, account, cancelable_only=False, strategy_name=""):
        return list(self.open_orders)


class FakePosition:
    def __init__(self, stock_code, volume, can_use_volume=None, avg_price=10.0):
        self.stock_code = stock_code
        self.volume = volume
        self.can_use_volume = volume if can_use_volume is None else can_use_volume
        self.avg_price = avg_price
        self.price = avg_price


class FakeOrder:
    def __init__(self, order_id, stock_code, order_type):
        self.order_id = order_id
        self.stock_code = stock_code
        self.order_type = order_type
        self.order_sysid = str(order_id)


class FakeBridge:
    """上下文管理器：进入时替换 bridge.pool 的出口，退出时还原。"""

    def __init__(self, account_ids, allow_order=True):
        self.account_ids = list(account_ids)
        self.allow_order_flag = allow_order
        self.orders = []
        self.traders = {aid: FakeTrader(aid, self.orders) for aid in self.account_ids}
        self._saved = {}

    def trader(self, account_id):
        return self.traders[account_id]

    def __enter__(self):
        self._saved = {
            "get_trader": bridge_pool.get_trader,
            "get_account_ref": bridge_pool.get_account_ref,
            "allow_order": bridge_pool.allow_order,
            "_compat": bridge_pool._compat,
            "account_ids": bridge_config.account_ids,
            "list_accounts": bridge_config.list_accounts,
        }

        def get_trader(account_id):
            account_id = str(account_id)
            if account_id not in self.traders:
                raise bridge_pool.BridgeUnavailable("账号 %s 未配置" % account_id)
            return self.traders[account_id]

        bridge_pool.get_trader = get_trader
        bridge_pool.get_account_ref = lambda aid: {"account_id": aid}
        bridge_pool.allow_order = lambda aid: (
            self.allow_order_flag and str(aid) in self.traders)
        bridge_pool._compat = lambda: FakeConstants
        bridge_config.account_ids = lambda enabled_only=True: list(self.account_ids)
        bridge_config.list_accounts = lambda enabled_only=True: []
        return self

    def __exit__(self, *exc):
        bridge_pool.get_trader = self._saved["get_trader"]
        bridge_pool.get_account_ref = self._saved["get_account_ref"]
        bridge_pool.allow_order = self._saved["allow_order"]
        bridge_pool._compat = self._saved["_compat"]
        bridge_config.account_ids = self._saved["account_ids"]
        bridge_config.list_accounts = self._saved["list_accounts"]
        return False
