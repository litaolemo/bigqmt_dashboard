#!/usr/bin/env python3
"""
管理员初始化脚本

创建初始管理员账户，用于管理股票持仓展示系统。
"""

import sqlite3
import hashlib
from datetime import datetime
from passlib.context import CryptContext

# 数据库路径
DB_PATH = "dashboard.db"

# 密码上下文
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL')
    cursor = conn.cursor()

    # 创建用户表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT UNIQUE,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT DEFAULT 'user',
        account_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # 创建用户会话表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        token TEXT UNIQUE,
        expires_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (username) REFERENCES users (username)
    )
    ''')

    # 检查并添加缺失的列（数据库迁移）
    cursor.execute('PRAGMA table_info(users)')
    existing_columns = [column[1] for column in cursor.fetchall()]

    # 添加缺失的列
    if 'username' not in existing_columns:
        try:
            cursor.execute('ALTER TABLE users ADD COLUMN username TEXT UNIQUE')
            print("已添加 username 列")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                print(f"添加 username 列时出错: {e}")

    if 'password' not in existing_columns:
        try:
            cursor.execute('ALTER TABLE users ADD COLUMN password TEXT')
            print("已添加 password 列")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                print(f"添加 password 列时出错: {e}")

    if 'role' not in existing_columns:
        try:
            cursor.execute('ALTER TABLE users ADD COLUMN role TEXT DEFAULT "user"')
            print("已添加 role 列")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                print(f"添加 role 列时出错: {e}")

    if 'account_name' not in existing_columns:
        try:
            cursor.execute('ALTER TABLE users ADD COLUMN account_name TEXT')
            print("已添加 account_name 列")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                print(f"添加 account_name 列时出错: {e}")

    if 'created_at' not in existing_columns:
        try:
            cursor.execute('ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
            print("已添加 created_at 列")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                print(f"添加 created_at 列时出错: {e}")

    # 创建持仓表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT,
        account_type INTEGER,
        avg_price REAL,
        can_use_volume INTEGER,
        direction INTEGER,
        float_profit REAL,
        frozen_volume INTEGER,
        instrument_name TEXT,
        last_price REAL,
        market_value REAL,
        on_road_volume INTEGER,
        open_date TEXT,
        open_price REAL,
        position_profit REAL,
        profit_rate REAL,
        secu_account TEXT,
        stock_code TEXT,
        volume INTEGER,
        yesterday_volume INTEGER,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')

    # 创建交易表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT,
        account_type INTEGER,
        commission REAL,
        direction INTEGER,
        instrument_name TEXT,
        offset_flag INTEGER,
        order_id INTEGER,
        order_remark TEXT,
        order_sysid TEXT,
        order_type INTEGER,
        secu_account TEXT,
        stock_code TEXT,
        strategy_name TEXT,
        traded_amount REAL,
        traded_id TEXT,
        traded_price REAL,
        traded_time INTEGER,
        traded_volume INTEGER,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')

    # 创建资产表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT,
        account_type INTEGER,
        cash REAL,
        current_balance REAL,
        fetch_balance REAL,
        frozen_cash REAL,
        market_value REAL,
        total_asset REAL,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')

    # 创建历史资产表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS asset_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT,
        total_asset REAL,
        market_value REAL,
        cash REAL,
        record_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')

    # 创建索引
    cursor.execute('''
    CREATE INDEX IF NOT EXISTS idx_asset_history_account_time
    ON asset_history (account_id, record_time)
    ''')

    conn.commit()
    conn.close()
    print("数据库初始化完成")


def create_admin_user(username, password, account_name="系统管理员"):
    """创建管理员用户"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 检查用户是否已存在
    cursor.execute('SELECT id FROM users WHERE username = ?', (username,))
    if cursor.fetchone():
        print(f"错误：用户 '{username}' 已存在")
        conn.close()
        return False

    # 生成用户ID和密码哈希
    account_id = f"ADMIN_{int(datetime.now().timestamp())}"
    hashed_password = pwd_context.hash(password)

    # 插入管理员用户
    cursor.execute('''
    INSERT INTO users (account_id, username, password, role, account_name)
    VALUES (?, ?, ?, 'admin', ?)
    ''', (account_id, username, hashed_password, account_name))

    conn.commit()
    conn.close()

    print(f"管理员用户创建成功！")
    print(f"  用户名: {username}")
    print(f"  密码: {password}")
    print(f"  账户ID: {account_id}")
    print(f"  角色: admin")
    return True


def create_test_user(account_id, username, password, account_name=""):
    """创建测试用户"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 检查用户是否已存在
    cursor.execute('SELECT id FROM users WHERE username = ? OR account_id = ?', (username, account_id))
    if cursor.fetchone():
        print(f"警告：用户 '{username}' 或账户ID '{account_id}' 已存在，跳过创建")
        conn.close()
        return False

    # 生成密码哈希
    hashed_password = pwd_context.hash(password)

    # 插入普通用户
    cursor.execute('''
    INSERT INTO users (account_id, username, password, role, account_name)
    VALUES (?, ?, ?, 'user', ?)
    ''', (account_id, username, hashed_password, account_name))

    conn.commit()
    conn.close()

    print(f"测试用户创建成功！")
    print(f"  账户ID: {account_id}")
    print(f"  用户名: {username}")
    print(f"  密码: {password}")
    print(f"  角色: user")
    return True


def list_users():
    """列出所有用户"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('SELECT account_id, username, role, account_name, created_at FROM users ORDER BY id')
    users = cursor.fetchall()

    conn.close()

    print("\n" + "="*80)
    print("用户列表：")
    print("="*80)
    print(f"{'账户ID':<20} {'用户名':<15} {'角色':<10} {'账户名称':<15} {'创建时间':<20}")
    print("-"*80)

    for user in users:
        account_id, username, role, account_name, created_at = user
        print(f"{account_id:<20} {username:<15} {role:<10} {account_name:<15} {created_at:<20}")

    print("="*80 + "\n")


def reset_database():
    """完全重置数据库（删除并重新创建）"""
    import os
    if os.path.exists(DB_PATH):
        confirm = input(f"警告：这将删除现有数据库 '{DB_PATH}'！确认删除？(yes/no): ")
        if confirm.lower() == 'yes':
            os.remove(DB_PATH)
            print(f"已删除数据库: {DB_PATH}")
            return True
        else:
            print("取消删除")
            return False
    return True


def main():
    """主函数"""
    print("="*80)
    print("股票持仓展示系统 - 管理员初始化工具")
    print("="*80 + "\n")

    # 检查数据库是否存在
    import os
    if os.path.exists(DB_PATH):
        print(f"检测到现有数据库: {DB_PATH}")
        choice = input("选择操作:\n  1. 迁移现有数据库（添加缺失字段）\n  2. 删除并重新创建数据库\n请输入选择 (1/2): ").strip()

        if choice == '2':
            if not reset_database():
                print("\n操作已取消")
                return
        else:
            print("\n将尝试迁移现有数据库...")

    # 初始化数据库
    init_db()

    # 创建管理员账户
    print("\n创建管理员账户：")
    print("-"*40)
    admin_username = input("请输入管理员用户名 (默认: admin): ").strip() or "admin"
    admin_password = input("请输入管理员密码 (默认: admin123): ").strip() or "admin123"
    admin_name = input("请输入管理员显示名称 (默认: 系统管理员): ").strip() or "系统管理员"

    if create_admin_user(admin_username, admin_password, admin_name):
        print("\n管理员账户创建成功！")

    # 创建测试用户（可选）
    print("\n是否创建测试用户？ (y/n): ", end="")
    if input().strip().lower() == 'y':
        print("\n创建测试用户：")
        print("-"*40)

        test_account_id = input("请输入测试用户账户ID (默认: 1000000001): ").strip() or "1000000001"
        test_username = input("请输入测试用户名 (默认: testuser): ").strip() or "testuser"
        test_password = input("请输入测试用户密码 (默认: user123): ").strip() or "user123"
        test_name = input("请输入测试用户显示名称 (默认: 测试用户): ").strip() or "测试用户"

        create_test_user(test_account_id, test_username, test_password, test_name)

    # 列出所有用户
    list_users()

    print("\n初始化完成！")
    print("您现在可以使用以下命令启动服务：")
    print("  python app.py")
    print("\n或者使用 uvicorn：")
    print("  uvicorn app:app --host 0.0.0.0 --port 8000 --reload")
    print("\n默认访问地址: http://localhost:8000")


if __name__ == "__main__":
    main()
