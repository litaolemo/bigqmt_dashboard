#!/usr/bin/env python3
"""
生成假订单数据脚本

用于模拟股票委托订单数据，包括买入和卖出委托单。
支持生成各种状态的订单数据。
"""

import random
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any


# 订单状态枚举
class OrderStatus:
    ORDER_UNREPORTED = 48      # 未报
    ORDER_WAIT_REPORTING = 49  # 待报
    ORDER_REPORTED = 50        # 已报
    ORDER_REPORTED_CANCEL = 51 # 已报待撤
    ORDER_PARTSUCC_CANCEL = 52 # 部成待撤
    ORDER_PART_CANCEL = 53     # 部撤
    ORDER_CANCELED = 54        # 已撤
    ORDER_PART_SUCC = 55       # 部成
    ORDER_SUCCEEDED = 56       # 已成
    ORDER_JUNK = 57            # 废单
    ORDER_UNKNOWN = 255        # 未知


# 订单类型
class OrderType:
    BUY = 23   # 买入
    SELL = 24  # 卖出


class OrderStatusName:
    """订单状态名称映射"""
    STATUS_MAP = {
        48: "未报",
        49: "待报",
        50: "已报",
        51: "已报待撤",
        52: "部成待撤",
        53: "部撤",
        54: "已撤",
        55: "部成",
        56: "已成",
        57: "废单",
        255: "未知"
    }


class FakeOrderGenerator:
    """假订单数据生成器"""

    def __init__(self):
        # 股票池
        self.stocks = [
            {"code": "600519.SH", "name": "贵州茅台", "price": 1850.00},
            {"code": "600036.SH", "name": "招商银行", "price": 32.50},
            {"code": "600637.SH", "name": "东方明珠", "price": 13.50},
            {"code": "600498.SH", "name": "烽火通信", "price": 38.90},
            {"code": "601399.SH", "name": "国机重装", "price": 6.50},
            {"code": "600629.SH", "name": "华建集团", "price": 24.80},
            {"code": "000858.SZ", "name": "五粮液", "price": 165.00},
            {"code": "000001.SZ", "name": "平安银行", "price": 12.50},
            {"code": "300750.SZ", "name": "宁德时代", "price": 195.00},
            {"code": "002594.SZ", "name": "比亚迪", "price": 255.00},
        ]

        # 策略名称
        self.strategies = [
            "first_to_second",
            "momentum_strategy",
            "mean_reversion",
            "grid_trading",
            "arbitrage",
        ]

        # 账户ID列表
        self.account_ids = [
            "1000000001",
            "1000000002",
            "1000000003",
        ]

    def generate_price(self, base_price: float, range_pct: float = 0.02) -> float:
        """生成价格（在基准价格附近波动）"""
        change = random.uniform(-range_pct, range_pct)
        return round(base_price * (1 + change), 2)

    def generate_volume(self, min_vol: int = 100, max_vol: int = 10000) -> int:
        """生成数量（100的整数倍）"""
        return random.randint(min_vol, max_vol) // 100 * 100

    def generate_order_id(self) -> int:
        """生成订单ID"""
        return random.randint(1000000000, 9999999999)

    def generate_order_sysid(self) -> str:
        """生成订单系统ID"""
        return str(random.randint(10000, 99999))

    def generate_traded_id(self) -> str:
        """生成成交ID"""
        return str(random.randint(1000000, 9999999))

    def generate_timestamp(self, hours_ago: int = 2) -> int:
        """生成时间戳（最近几小时内）"""
        now = datetime.now()
        delta = timedelta(hours=random.uniform(0, hours_ago))
        t = now - delta
        return int(t.timestamp())

    def generate_single_order(
        self,
        account_id: str = None,
        order_type: int = None,
        order_status: int = None
    ) -> Dict[str, Any]:
        """生成单个订单"""
        stock = random.choice(self.stocks)

        # 确定订单类型（买入或卖出）
        if order_type is None:
            order_type = random.choice([OrderType.BUY, OrderType.SELL])

        # 确定订单状态
        if order_status is None:
            # 随机生成订单状态，偏向未完成的状态
            weights = [
                5,   # 未报
                5,   # 待报
                30,  # 已报
                10,  # 已报待撤
                10,  # 部成待撤
                5,   # 部撤
                15,  # 已撤
                15,  # 部成
                5,   # 已成
            ]
            order_status = random.choices(
                [48, 49, 50, 51, 52, 53, 54, 55, 56],
                weights=weights
            )[0]

        # 生成委托价格
        order_price = self.generate_price(stock["price"])

        # 生成委托数量
        order_volume = self.generate_volume()

        # 根据订单状态生成已成交数量
        if order_status == OrderStatus.ORDER_SUCCEEDED:
            traded_volume = order_volume
        elif order_status == OrderStatus.ORDER_PART_SUCC:
            # 部成：至少成交100，最多成交 order_volume - 100
            min_trade = min(100, order_volume - 100)
            max_trade = max(order_volume - 100, 100)
            if max_trade > min_trade:
                traded_volume = random.randint(min_trade, max_trade) // 100 * 100
            else:
                traded_volume = 100
        elif order_status == OrderStatus.ORDER_PART_CANCEL:
            # 部撤：成交一部分，然后撤销剩余
            max_trade = max(order_volume // 2, 100)
            traded_volume = random.randint(100, max_trade) // 100 * 100
        else:
            traded_volume = 0

        # 确保 traded_volume 不超过 order_volume
        traded_volume = min(traded_volume, order_volume)

        # 计算委托金额和已成交金额
        order_amount = round(order_price * order_volume, 2)
        traded_amount = round(order_price * traded_volume, 2)

        order = {
            "account_id": account_id or random.choice(self.account_ids),
            "account_type": 2,
            "order_id": self.generate_order_id(),
            "order_sysid": self.generate_order_sysid(),
            "stock_code": stock["code"],
            "instrument_name": stock["name"],
            "order_type": order_type,
            "order_status": order_status,
            "order_status_name": OrderStatusName.STATUS_MAP.get(order_status, "未知"),
            "direction": order_type,  # 23=买入, 24=卖出
            "offset_flag": random.choice([48, 49]),  # 开仓/平仓
            "order_price": order_price,
            "order_volume": order_volume,
            "order_amount": order_amount,
            "traded_price": order_price if traded_volume > 0 else 0,
            "traded_volume": traded_volume,
            "traded_amount": traded_amount,
            "traded_id": self.generate_traded_id() if traded_volume > 0 else "",
            "strategy_name": random.choice(self.strategies),
            "order_remark": f"{('buy' if order_type == OrderType.BUY else 'sell')}_{random.randint(1, 20)}",
            "secu_account": "A000000000",
            "commission": round(traded_amount * 0.0003, 2) if traded_volume > 0 else 0,
            "order_time": self.generate_timestamp(),
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        return order

    def generate_orders(
        self,
        count: int = 20,
        account_id: str = None,
        order_type: int = None,
        order_status: int = None
    ) -> List[Dict[str, Any]]:
        """生成多个订单"""
        orders = []
        for _ in range(count):
            order = self.generate_single_order(account_id, order_type, order_status)
            orders.append(order)
        return orders

    def get_pending_buy_orders(self, account_id: str = None) -> Dict[str, Dict[str, Any]]:
        """
        获取未成交的买入委托单

        返回: dict {stock_code: order_info}
        """
        # 未成交的状态：未报、待报、已报、部成
        pending_statuses = [
            OrderStatus.ORDER_UNREPORTED,
            OrderStatus.ORDER_WAIT_REPORTING,
            OrderStatus.ORDER_REPORTED,
            OrderStatus.ORDER_PART_SUCC,
        ]

        # 生成买入订单
        orders = self.generate_orders(
            count=random.randint(5, 15),
            account_id=account_id,
            order_type=OrderType.BUY
        )

        # 筛选未成交的订单
        pending_orders = [
            order for order in orders
            if order["order_status"] in pending_statuses
        ]

        # 转换为 {stock_code: order_info} 格式
        result = {}
        for order in pending_orders:
            stock_code = order["stock_code"]
            # 如果同一股票有多个订单，保留最新的
            if stock_code not in result or order["order_time"] > result[stock_code]["order_time"]:
                result[stock_code] = order

        return result

    def get_all_orders_by_status(
        self,
        status: int = None,
        account_id: str = None
    ) -> List[Dict[str, Any]]:
        """获取指定状态的订单"""
        return self.generate_orders(
            count=random.randint(10, 30),
            account_id=account_id,
            order_status=status
        )

    def save_to_json(self, data: Any, filename: str):
        """保存数据到JSON文件"""
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"数据已保存到: {filename}")

    def print_orders(self, orders: List[Dict[str, Any]], title: str = "订单列表"):
        """打印订单信息"""
        print(f"\n{'='*100}")
        print(f"{title}")
        print(f"{'='*100}")
        print(f"{'订单ID':<12} {'股票代码':<12} {'股票名称':<12} {'类型':<6} {'状态':<10} {'委托价格':<10} "
              f"{'委托数量':<10} {'已成交数量':<10} {'委托金额':<12}")
        print(f"{'-'*100}")

        for order in orders:
            type_str = "买入" if order["order_type"] == OrderType.BUY else "卖出"
            print(f"{order['order_id']:<12} {order['stock_code']:<12} {order['instrument_name']:<12} "
                  f"{type_str:<6} {order['order_status_name']:<10} {order['order_price']:<10.2f} "
                  f"{order['order_volume']:<10} {order['traded_volume']:<10} {order['order_amount']:<12.2f}")

        print(f"{'='*100}\n")

    def print_pending_buy_orders(self, pending_orders: Dict[str, Dict[str, Any]]):
        """打印未成交买入订单"""
        print(f"\n{'='*120}")
        print("未成交的买入委托单")
        print(f"{'='*120}")
        print(f"{'股票代码':<12} {'股票名称':<12} {'订单状态':<10} {'委托价格':<10} {'委托数量':<10} "
              f"{'已成交数量':<10} {'委托金额':<12} {'订单时间':<20}")
        print(f"{'-'*120}")

        for stock_code, order in pending_orders.items():
            order_time = datetime.fromtimestamp(order["order_time"]).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{order['stock_code']:<12} {order['instrument_name']:<12} {order['order_status_name']:<10} "
                  f"{order['order_price']:<10.2f} {order['order_volume']:<10} {order['traded_volume']:<10} "
                  f"{order['order_amount']:<12.2f} {order_time:<20}")

        print(f"{'='*120}\n")


def main():
    """主函数 - 演示各种订单数据生成"""
    generator = FakeOrderGenerator()

    print("="*60)
    print("股票订单数据生成器")
    print("="*60)

    # 1. 生成所有订单
    print("\n1. 生成所有订单数据...")
    all_orders = generator.generate_orders(count=20)
    generator.print_orders(all_orders, "所有订单")
    generator.save_to_json(all_orders, "all_orders.json")

    # 2. 生成未成交的买入委托单
    print("\n2. 生成未成交的买入委托单...")
    pending_buy_orders = generator.get_pending_buy_orders(account_id="1000000001")
    generator.print_pending_buy_orders(pending_buy_orders)
    generator.save_to_json(pending_buy_orders, "pending_buy_orders.json")

    # 3. 生成已报状态的订单
    print("\n3. 生成已报状态的订单...")
    reported_orders = generator.get_all_orders_by_status(
        status=OrderStatus.ORDER_REPORTED,
        account_id="1000000001"
    )
    generator.print_orders(reported_orders, "已报订单")
    generator.save_to_json(reported_orders, "reported_orders.json")

    # 4. 生成部成状态的订单
    print("\n4. 生成部成状态的订单...")
    part_succ_orders = generator.get_all_orders_by_status(
        status=OrderStatus.ORDER_PART_SUCC,
        account_id="1000000001"
    )
    generator.print_orders(part_succ_orders, "部成订单")
    generator.save_to_json(part_succ_orders, "part_succ_orders.json")

    # 5. 生成各种状态的统计
    print("\n5. 订单状态统计...")
    status_orders = generator.generate_orders(count=100)
    status_count = {}
    for order in status_orders:
        status = order["order_status_name"]
        status_count[status] = status_count.get(status, 0) + 1

    print("\n订单状态分布:")
    for status, count in sorted(status_count.items(), key=lambda x: x[1], reverse=True):
        print(f"  {status}: {count}")

    print("\n生成完成！")
    print("\n生成的文件:")
    print("  - all_orders.json: 所有订单数据")
    print("  - pending_buy_orders.json: 未成交买入委托单")
    print("  - reported_orders.json: 已报订单")
    print("  - part_succ_orders.json: 部成订单")


if __name__ == "__main__":
    main()
