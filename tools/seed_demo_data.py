"""造一份演示数据库：合成账户、持仓、成交、委托、资金曲线。

用途有两个：
  * 截图和文档 —— 不能拿真实账户的持仓和别名去做公开素材；
  * 新克隆下来的人没有 QMT 也能看到面板长什么样。

用法（会新建一个独立的库，不碰 dashboard.db）：
    python tools/seed_demo_data.py demo.db
    DASHBOARD_DB_PATH=demo.db python app.py

持仓里特意混了股票、科创板、ETF 和两只可转债 —— 转债列（溢价率/转股价值/强赎）
需要真的有转债持仓才看得到。
"""

import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ACCOUNT = "DEMO00000001"
USERNAME = "demo"
PASSWORD = "demo1234"

# 代码 / 名称 / 持仓 / 成本 / 现价。约 200 万规模，整体盈利。
POSITIONS = [
    ("002716.SZ", "湖南白银", 40000, 10.80, 11.65),
    ("601118.SH", "海南橡胶", 60000, 6.05, 6.67),
    ("600000.SH", "浦发银行", 30000, 8.62, 9.00),
    ("510300.SH", "沪深300ETF", 60000, 3.912, 4.086),
    ("688981.SH", "中芯国际", 2000, 82.30, 88.45),
    ("123281.SZ", "中仑转债", 800, 138.500, 156.440),
    ("111026.SH", "派克转债", 500, 151.200, 160.353),
]

TRADES = [
    ("600000.SH", "浦发银行", 23, 10000, 8.95),
    ("123281.SZ", "中仑转债", 23, 300, 154.200),
    ("688981.SH", "中芯国际", 24, 500, 89.10),
    ("510300.SH", "沪深300ETF", 23, 20000, 4.070),
]

# 转债参考数据。真实部署由 cb.reference 从 akshare 日更，演示库自带两条，
# 这样离线也能看到溢价率/转股价值/强赎那几列。
# 代码 / 简称 / 正股代码 / 正股简称 / 转股价 / 发行规模(亿) / 上市日 / 评级
CB_REFERENCE = [
    ("123281.SZ", "中仑转债", "301565.SZ", "中仑新材", 20.28, 10.68, "2026-08-24", "AA-"),
    ("111026.SH", "派克转债", "605123.SH", "派克新材", 81.80, 15.80, "2026-08-25", "AA"),
]

# 挂着没成的委托 —— 直连之后才有的视图
OPEN_ORDERS = [
    ("O20260830001", "601118.SH", "海南橡胶", 23, 50, 20000, 0, 6.55),
    ("O20260830002", "111026.SH", "派克转债", 24, 55, 200, 60, 162.000),
]


def seed(db_path):
    if os.path.exists(db_path):
        os.remove(db_path)
    os.environ["DASHBOARD_DB_PATH"] = db_path
    os.environ["QMT_DASHBOARD_SKIP_BACKGROUND_TASKS"] = "1"
    os.environ.setdefault("SECRET_KEY", "demo-seed-only")

    import app                                  # 建表 + 拿密码哈希工具

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        "INSERT OR REPLACE INTO users (account_id, username, password, role, account_name, alias) "
        "VALUES (?, ?, ?, 'admin', ?, ?)",
        (ACCOUNT, USERNAME, app.get_password_hash(PASSWORD), "演示账户", "演示账户"))

    market_value = 0.0
    for code, name, volume, cost, price in POSITIONS:
        value = round(price * volume, 2)
        market_value += value
        profit = round((price - cost) * volume, 2)
        cur.execute(
            "INSERT INTO positions (account_id, account_type, avg_price, can_use_volume, "
            "direction, float_profit, frozen_volume, instrument_name, last_price, market_value, "
            "on_road_volume, open_date, open_price, position_profit, profit_rate, secu_account, "
            "stock_code, volume, yesterday_volume, update_time) "
            "VALUES (?,2,?,?,48,?,0,?,?,?,0,'',?,?,?,'',?,?,?,?)",
            (ACCOUNT, cost, volume, profit, name, price, value, cost, profit,
             round((price - cost) / cost, 6), code, volume, volume, now_str))

    cash = 240000.00
    total = round(market_value + cash, 2)
    cur.execute(
        "INSERT OR REPLACE INTO assets (account_id, account_type, cash, current_balance, "
        "fetch_balance, frozen_cash, market_value, total_asset, update_time) "
        "VALUES (?,2,?,?,?,0,?,?,?)",
        (ACCOUNT, cash, cash, cash, round(market_value, 2), total, now_str))

    # 资金曲线：从 30 天前稳步爬到当前总资产。有回撤但整体向上，
    # 不然演示图上是一条毫无说服力的直线。
    random.seed(20260830)
    start = total * 0.918
    for i, days_ago in enumerate(range(30, 0, -1)):
        stamp = (now - timedelta(days=days_ago)).replace(hour=15, minute=0, second=0)
        drift = (30 - days_ago) / 30.0                       # 0 -> 1 线性爬升
        value = start + (total - start) * drift * random.uniform(0.94, 1.05)
        cur.execute(
            "INSERT INTO asset_history (account_id, total_asset, market_value, cash, record_time) "
            "VALUES (?,?,?,?,?)",
            (ACCOUNT, round(value, 2), round(value - cash, 2), cash,
             stamp.strftime("%Y-%m-%d %H:%M:%S")))
    day_open = total * 0.9945                                 # 当日也收红
    steps = list(range(240, -1, -10))
    for i, minutes_ago in enumerate(steps):
        stamp = now - timedelta(minutes=minutes_ago)
        drift = i / max(len(steps) - 1, 1)
        value = day_open + (total - day_open) * drift + total * random.uniform(-0.0012, 0.0012)
        cur.execute(
            "INSERT INTO asset_history (account_id, total_asset, market_value, cash, record_time) "
            "VALUES (?,?,?,?,?)",
            (ACCOUNT, round(value, 2), round(value - cash, 2), cash,
             stamp.strftime("%Y-%m-%d %H:%M:%S")))

    # 每日盈亏：面板的「累计盈亏 / 累计收益率」是从这张表汇总的，不是从持仓浮盈算的。
    # 造 30 个交易日的记录，整体向上但有回撤，收益率口径才对得上。
    running = start
    for days_ago in range(30, 0, -1):
        stamp = (now - timedelta(days=days_ago))
        if stamp.weekday() >= 5:                 # 跳过周末
            continue
        prev = running
        running = prev * (1 + random.uniform(-0.004, 0.011))
        profit = round(running - prev, 2)
        cur.execute(
            "INSERT INTO daily_profits (account_id, date, daily_profit, profit_rate, "
            "total_asset, capital_adjustment) VALUES (?,?,?,?,?,0)",
            (ACCOUNT, stamp.strftime("%Y-%m-%d"), profit,
             round(profit / prev * 100, 4), round(running, 2)))

    for i, (code, name, side, volume, price) in enumerate(TRADES):
        cur.execute(
            "INSERT INTO trades (account_id, account_type, commission, direction, instrument_name, "
            "offset_flag, order_id, order_remark, order_sysid, order_type, secu_account, stock_code, "
            "strategy_name, traded_amount, traded_id, traded_price, traded_time, traded_volume) "
            "VALUES (?,2,?,?,?,49,?,'dashboard',?,?,'',?,'demo',?,?,?,?,?)",
            (ACCOUNT, round(price * volume * 0.0003, 2), side, name,
             "T%03d" % i, "S%03d" % i, side, code,
             round(price * volume, 2), "TD%03d" % i, price,
             int((now - timedelta(minutes=30 * (len(TRADES) - i))).timestamp()), volume))

    # 用本连接建表：cb.reference.ensure_table() 会另开一个连接，而这里的事务
    # 还没提交，SQLite 会直接 database is locked。
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cb_reference (
            bond_code TEXT PRIMARY KEY, bond_name TEXT, stock_code TEXT,
            stock_name TEXT, conversion_price REAL, issue_size REAL,
            list_date TEXT, rating TEXT, apply_date TEXT, apply_code TEXT,
            apply_limit REAL, update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for row in CB_REFERENCE:
        cur.execute(
            "INSERT OR REPLACE INTO cb_reference (bond_code, bond_name, stock_code, "
            "stock_name, conversion_price, issue_size, list_date, rating, apply_date, "
            "apply_code, apply_limit) VALUES (?,?,?,?,?,?,?,?,'','',0)", row)

    for oid, code, name, otype, status, ovol, tvol, price in OPEN_ORDERS:
        cur.execute(
            "INSERT INTO orders (account_id, order_id, order_sysid, stock_code, instrument_name, "
            "order_type, order_status, order_volume, traded_volume, price, traded_price, order_time, "
            "strategy_name, order_remark, status_msg) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0,?,'dashboard','demo','')",
            (ACCOUNT, oid, oid, code, name, otype, status, ovol, tvol, price,
             int((now - timedelta(minutes=12)).timestamp())))

    for i, (code, name, side, volume, price) in enumerate(TRADES):
        cur.execute(
            "INSERT INTO order_audit (account_id, stock_code, side, volume, price, price_type, "
            "order_sys_id, status, message, operator, remark) "
            "VALUES (?,?,?,?,?,'latest',?,'submitted','已报单','demo','dashboard')",
            (ACCOUNT, code, "buy" if side == 23 else "sell", volume, price, "S%03d" % i))

    conn.commit()
    conn.close()
    print("演示库已生成: %s" % db_path)
    print("  账号 %s / 用户 %s / 密码 %s" % (ACCOUNT, USERNAME, PASSWORD))
    profit = sum(round((p - c) * v, 2) for _, _, v, c, p in POSITIONS)
    print("  持仓 %d 只（含 2 只可转债）, 总资产 %.2f, 浮盈 %.2f (%.2f%%)"
          % (len(POSITIONS), total, profit, profit / (total - profit) * 100))
    print("\n启动: DASHBOARD_DB_PATH=%s python app.py" % db_path)


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "demo.db")
