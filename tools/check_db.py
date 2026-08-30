"""查某账号某日的资产快照。用法: python tools/check_db.py <account_id> <YYYY-MM-DD>"""
import sqlite3
import sys

if len(sys.argv) < 3:
    sys.exit("用法: python tools/check_db.py <account_id> <YYYY-MM-DD>")

ACCT = sys.argv[1]
DATE = sys.argv[2]

conn = sqlite3.connect('dashboard.db')
cur = conn.cursor()

# 取该日最后一条 total_asset > 0 的记录
cur.execute("""
    SELECT total_asset, record_time
    FROM asset_history
    WHERE account_id = ? AND date(record_time) = ? AND total_asset > 0
    ORDER BY record_time DESC LIMIT 1
""", (ACCT, DATE))
curr_row = cur.fetchone()
print(f"当日有效收盘资产: {curr_row}")
curr_asset = curr_row[0]

# 取前一日（2026-04-22）的最后一条有效资产
cur.execute("""
    SELECT total_asset, record_time
    FROM asset_history
    WHERE account_id = ? AND date(record_time) = '2026-04-22' AND total_asset > 0
    ORDER BY record_time DESC LIMIT 1
""", (ACCT,))
prev_row = cur.fetchone()
print(f"前日有效收盘资产: {prev_row}")
prev_asset = prev_row[0]

# 当日资金调整
cur.execute("SELECT SUM(amount) FROM capital_adjustments WHERE account_id=? AND date(adjust_time)=?", (ACCT, DATE))
adj = cur.fetchone()[0] or 0
print(f"当日资金调整: {adj}")

daily_profit = curr_asset - prev_asset - adj
denominator = prev_asset + adj
profit_rate = (daily_profit / denominator * 100) if denominator != 0 else 0
print(f"\n修正后 daily_profit: {daily_profit:.2f}")
print(f"修正后 profit_rate: {profit_rate:.4f}%")
print(f"修正后 total_asset: {curr_asset:.2f}")

# 写入修正值
cur.execute("""
    UPDATE daily_profits
    SET daily_profit=?, profit_rate=?, total_asset=?
    WHERE account_id=? AND date=?
""", (daily_profit, profit_rate, curr_asset, ACCT, DATE))
conn.commit()
print(f"\n已更新 daily_profits 中 {ACCT} / {DATE} 的记录")
