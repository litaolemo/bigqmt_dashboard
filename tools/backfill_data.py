import sqlite3
from datetime import datetime, timedelta
import random

DB_PATH = 'dashboard.db'

def backfill():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('PRAGMA journal_mode=WAL')
        cursor = conn.cursor()
        
        # 获取所有用户
        cursor.execute('SELECT account_id FROM users')
        users = [row[0] for row in cursor.fetchall()]
        
        for account_id in users:
            # 1. 获取当前资产作为锚点
            cursor.execute('SELECT total_asset, market_value, cash FROM assets WHERE account_id = ?', (account_id,))
            current_asset_row = cursor.fetchone()
            if not current_asset_row:
                print(f"Skipping {account_id}: no current asset")
                continue
            
            curr_total, curr_market, curr_cash = current_asset_row
            
            # 2. 获取过去30天的所有交易
            start_ts = int((datetime.now() - timedelta(days=30)).timestamp())
            cursor.execute('''
                SELECT date(traded_time, 'unixepoch', 'localtime') as day, 
                       traded_amount
                FROM trades 
                WHERE account_id = ? AND traded_time >= ?
            ''', (account_id, start_ts))
            
            trades = cursor.fetchall()
            daily_trades = {}
            for day, amount in trades:
                daily_trades[day] = daily_trades.get(day, 0) + amount
                
            # 3. 逐日回溯计算
            history_to_insert = []
            temp_total = curr_total
            
            for i in range(1, 31):
                date_dt = datetime.now() - timedelta(days=i)
                date_str = date_dt.strftime('%Y-%m-%d')
                
                # 检查是否已有记录
                cursor.execute('SELECT id FROM asset_history WHERE account_id = ? AND date(record_time) = ?', (account_id, date_str))
                if cursor.fetchone():
                    cursor.execute('SELECT total_asset FROM asset_history WHERE account_id = ? AND date(record_time) = ? ORDER BY record_time DESC LIMIT 1', (account_id, date_str))
                    row = cursor.fetchone()
                    if row:
                        temp_total = row[0]
                    continue
                    
                # 模拟每日利润
                if date_str in daily_trades:
                    trade_vol = daily_trades[date_str]
                    profit_factor = random.uniform(-0.005, 0.008)
                    daily_profit = trade_vol * profit_factor
                else:
                    daily_profit = temp_total * random.uniform(-0.001, 0.0012)
                
                temp_total -= daily_profit
                
                # 插入历史记录
                record_time = f"{date_str} 15:00:00"
                history_to_insert.append((account_id, temp_total, temp_total * 0.7, temp_total * 0.3, record_time))
                
            if history_to_insert:
                cursor.executemany('''
                    INSERT INTO asset_history (account_id, total_asset, market_value, cash, record_time)
                    VALUES (?, ?, ?, ?, ?)
                ''', history_to_insert)
                conn.commit()
                print(f"Successfully backfilled {len(history_to_insert)} records for {account_id}")
            else:
                print(f"No records to backfill for {account_id}")
                
        conn.close()
    except Exception as e:
        print(f"Error during backfill: {e}")

if __name__ == '__main__':
    backfill()
