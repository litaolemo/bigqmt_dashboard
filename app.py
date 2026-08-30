from fastapi import FastAPI, Request, Query, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
import uvicorn
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
import threading
import time
import shutil
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pydantic import BaseModel
import pymysql
import akshare as ak
import pandas as pd
import concurrent.futures
import requests
import asyncio

import os

# Tushare：惰性代理，未配置 TUSHARE_TOKEN 时 pro.* 抛 TushareUnavailable，
# 所有调用点本来就在 try/except 里，自动降级为字段留空。
import settings
from plugins.tushare_client import pro, is_available as tushare_available

# 大QMT 直连层
import dbaccess
from bridge import config as bridge_config
from bridge import instruments
from bridge import market as bridge_market
from bridge import orders as bridge_orders
from bridge import pool as bridge_pool
from sync import callbacks as sync_callbacks
from sync import poller as sync_poller
from sync import sinks as sync_sinks

# 可转债
from cb import ipo as cb_ipo
from cb import metrics as cb_metrics
from cb import reference as cb_reference
from cb import service as cb_service

# 全局行情缓存
GLOBAL_MARKET_MIN_DATA = {}
GLOBAL_MARKET_MIN_DATA_RAW = {}
GLOBAL_MARKET_LAST_CLOSE = {}   # 昨收价缓存: {ts_code: float}
_LAST_CLOSE_FETCH_DATE = None    # 记录上次拉取日期，每天只拉一次

# QMT实时价格缓存: {stock_code: {'last_price': float, 'update_time': datetime, 'minute_ohlc': {time_str: {'open','high','low','close'}}}}
GLOBAL_QMT_PRICE_CACHE = {}

# 标记当天是否已下发过回补指令（避免重复），格式: {account_id: True}
_BACKFILL_KLINE_ISSUED = {}

# 标记 akshare 返回非今日数据时的初始化空缓存日志是否已打印, 避免每次轮询重复打
_A_INIT_EMPTY_NOTIFIED = set()

# 交易日判断缓存: {date_str (YYYY-MM-DD): bool}
_TRADE_DATE_CACHE = {}
_TRADE_DATE_FETCH_DATE = None

# 安全配置
# 改造前这里有一个可用的默认值，任何人拿到源码就能伪造本站 JWT。现在未配置就每进程
# 随机生成：重启会让 JWT 失效（DB 里的 user_sessions 仍有效，见 verify_user_session），
# 但绝不会留一个公开可知的签名密钥。生产请显式设置 SECRET_KEY。
_LEGACY_DEFAULT_SECRET = "your-secret-key-change-me-in-production"
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == _LEGACY_DEFAULT_SECRET:
    import secrets as _secrets
    if SECRET_KEY == _LEGACY_DEFAULT_SECRET:
        print("[安全] SECRET_KEY 仍是公开的历史默认值，已忽略并改用随机密钥")
    else:
        print("[安全] 未设置 SECRET_KEY 环境变量，本次启动使用随机密钥（重启后 JWT 失效）")
    SECRET_KEY = _secrets.token_urlsafe(48)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 设置为 1 天

# PUSH_API_KEY 已删除：没有推送入口就没有要校验的推送方。

# 密码上下文
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 数据缓存
asset_cache = {}

# 5分钟涨跌幅缓存: {stock_code: {'history': deque[(datetime, price)]}}
price_5min_cache = {}
PRICE_WINDOW_SECONDS = 300
PRICE_HISTORY_RETENTION_SECONDS = 1200

# pending_sell_commands / pending_buy_commands / pending_t0_commands 已删除。
# 它们是「等 QMT 客户端来取」的指令队列。直连之后下单是当场报的，没有东西需要排队。

# 账号同步/数据时刻（内存中，不持久化）。
# 在线状态现在由 account_liveness() 直接读 sync.poller 的状态算，这两个字典只剩
# 删账号时一并清理的作用，保留是为了不改动 clear_account_runtime_state 的语义。
account_last_sync = {}

# "活跃账户"判断阈值（天）：最近一次推送数据距今超过此天数则视为不活跃。
# 采用相对最新推送时间的方式，可正确处理节假日（节假日期间无人推送，阈值随之滚动）。
# 若需覆盖超长假期，可适当调大该值。
STALE_ACCOUNT_THRESHOLD_DAYS = 3

# 账号行情数据时间（内存中，不持久化）
# 格式: { account_id: 'YYYYMMDD HH:MM:SS' }
account_data_time = {}

# OAuth2密码承载器
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/token")

# 数据模型
class UserCreate(BaseModel):
    username: str
    password: str
    account_id: str
    account_name: str

# 观察者注册：只需用户名+密码（与交易账号体系隔离）
class ViewerCreate(BaseModel):
    username: str
    password: str

class CapitalAdjustment(BaseModel):
    account_id: str
    amount: float
    remark: str = ""
    adjust_time: str | None = None

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: str | None = None

class AliasUpdate(BaseModel):
    account_id: str
    alias: str

class PositionLock(BaseModel):
    account_id: str
    stock_code: str
    is_locked: bool

class LLMConfig(BaseModel):
    api_url: str = ""
    api_key: str = ""
    model: str = ""

class ResearchBoardParseRequest(BaseModel):
    content: str

class ResearchBoardRecordEdit(BaseModel):
    stock_name: str = ""
    stock_code: str = ""
    logic: str = ""
    target_market_value_yi: float | None = None
    industry: str = ""
    concept: str = ""
    limit_up_reason: str = ""
    topic: str = ""

# 后台任务只在真正起服务时拉起。改造前是在模块顶层直接 start_background_tasks()，
# 于是 `import app`（测试、运维脚本）也会起一堆线程去写 DB —— tests 里临时目录被
# 后台线程占住删不掉就是这么来的。直连大QMT 后这里还会挂账号轮询和行情订阅，更不能
# 在 import 期启动。QMT_DASHBOARD_SKIP_BACKGROUND_TASKS=1 可显式关掉。
@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.getenv("QMT_DASHBOARD_SKIP_BACKGROUND_TASKS") != "1":
        start_background_tasks()
    yield


# 初始化FastAPI应用
app = FastAPI(title="股票持仓展示", description="实时股票持仓监控系统",
              version="1.0.0", lifespan=lifespan)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有方法
    allow_headers=["*"],  # 允许所有头部
)

# 数据库连接
# 数据库位置。默认仓库根目录下的 dashboard.db；部署时可用 DASHBOARD_DB_PATH
# 指到别处（挂载卷、独立数据盘），也方便拿副本跑而不碰生产库。
DB_PATH = os.getenv("DASHBOARD_DB_PATH", "dashboard.db")

def get_db_connection():
    """获取数据库连接并设置 WAL 模式"""
    conn = sqlite3.connect(DB_PATH)
    # 启用 WAL 模式 (Write-Ahead Logging)
    # WAL 模式可以显著提高并发读写性能，减少数据库锁定冲突
    conn.execute('PRAGMA journal_mode=WAL')
    # 设置繁忙等待超时时间（毫秒）
    conn.execute('PRAGMA busy_timeout=5000')
    return conn

# 初始化数据库
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 创建用户表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT UNIQUE,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT DEFAULT 'user', -- 'admin' 或 'user'
        account_name TEXT,
        alias TEXT,
        clear_password TEXT,
        position_factor REAL DEFAULT 1.0,
        open_count_factor REAL DEFAULT 1.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # 创建用户会话表，用于持久化登录状态
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

    # 观察者(viewer)用户：与交易账号完全独立的一套注册/登录体系，登录后只能查看「历史买入列表」
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS viewer_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    # 观察者登录流水（近30天登录次数统计）
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS viewer_logins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        ip TEXT,
        login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    # 观察者每日在线时长（秒），由前端心跳累计
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS viewer_daily_online (
        username TEXT,
        date TEXT,
        online_seconds INTEGER DEFAULT 0,
        PRIMARY KEY (username, date)
    )
    ''')
    # 观察者「按 IP」每日在线时长（识别一个账号从哪些 IP 用、各用多久）
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS viewer_ip_online (
        username TEXT,
        ip TEXT,
        date TEXT,
        online_seconds INTEGER DEFAULT 0,
        PRIMARY KEY (username, ip, date)
    )
    ''')
    # 观察者「按 IP」最近活跃时间（用于并发检测）
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS viewer_ip_last_seen (
        username TEXT,
        ip TEXT,
        last_seen TIMESTAMP,
        PRIMARY KEY (username, ip)
    )
    ''')
    # 观察者并发登录事件（同一账号两个不同 IP 在 90s 内同时活跃时记一条）
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS viewer_concurrency_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        ip_a TEXT,
        ip_b TEXT,
        event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    # 交易账户登录流水（近30天登录次数统计）
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_logins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT,
        username TEXT,
        login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    # 兼容旧库：viewer_logins 补 ip 字段
    try:
        cursor.execute('PRAGMA table_info(viewer_logins)')
        if 'ip' not in [c[1] for c in cursor.fetchall()]:
            cursor.execute('ALTER TABLE viewer_logins ADD COLUMN ip TEXT')
    except Exception:
        pass

    # 如果表已存在，添加缺失的字段
    cursor.execute('PRAGMA table_info(users)')
    columns = [column[1] for column in cursor.fetchall()]
    
    if 'username' not in columns:
        cursor.execute('ALTER TABLE users ADD COLUMN username TEXT UNIQUE')
    
    if 'password' not in columns:
        cursor.execute('ALTER TABLE users ADD COLUMN password TEXT')
    
    if 'role' not in columns:
        cursor.execute('ALTER TABLE users ADD COLUMN role TEXT DEFAULT "user"')
    
    if 'alias' not in columns:
        cursor.execute('ALTER TABLE users ADD COLUMN alias TEXT')
    
    if 'clear_password' not in columns:
        cursor.execute('ALTER TABLE users ADD COLUMN clear_password TEXT')

    if 'position_factor' not in columns:
        cursor.execute('ALTER TABLE users ADD COLUMN position_factor REAL DEFAULT 1.0')

    if 'open_count_factor' not in columns:
        cursor.execute('ALTER TABLE users ADD COLUMN open_count_factor REAL DEFAULT 1.0')
    
    if 'if_delete' not in columns:
        cursor.execute('ALTER TABLE users ADD COLUMN if_delete INTEGER DEFAULT 0')

    if 'is_dormant' not in columns:
        cursor.execute('ALTER TABLE users ADD COLUMN is_dormant INTEGER DEFAULT 0')
    
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
        current_change REAL, -- 当日涨跌幅
        topic_reason TEXT, -- 题材
        secu_account TEXT,
        stock_code TEXT,
        volume INTEGER,
        yesterday_volume INTEGER,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')

    # 检查 positions 表是否缺失字段
    cursor.execute('PRAGMA table_info(positions)')
    pos_columns = [column[1] for column in cursor.fetchall()]
    if 'current_change' not in pos_columns:
        cursor.execute('ALTER TABLE positions ADD COLUMN current_change REAL')
    if 'topic_reason' not in pos_columns:
        cursor.execute('ALTER TABLE positions ADD COLUMN topic_reason TEXT')
    
    # 创建策略配置保存表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS strategy_configs (
        account_id TEXT PRIMARY KEY,
        config_content TEXT,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS app_settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS research_board_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_name TEXT,
        stock_code TEXT,
        logic TEXT,
        topic TEXT,
        industry TEXT,
        concept TEXT,
        limit_up_reason TEXT,
        limit_up_trade_date TEXT,
        target_market_value_yi REAL,
        current_market_value_yi REAL,
        current_change REAL,
        raw_text TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    cursor.execute('PRAGMA table_info(research_board_records)')
    research_columns = [column[1] for column in cursor.fetchall()]
    for col_name in ['topic', 'industry', 'concept', 'limit_up_reason', 'limit_up_trade_date']:
        if col_name not in research_columns:
            cursor.execute(f'ALTER TABLE research_board_records ADD COLUMN {col_name} TEXT')

    # 记录板每次输入的原始消息归档，方便复核解析结果
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS research_board_inputs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT,
        content TEXT,
        created_by TEXT,
        parse_source TEXT,
        llm_raw_response TEXT,
        parsed_records_json TEXT,
        record_ids TEXT,
        error TEXT,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        finished_at TIMESTAMP
    )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_research_board_inputs_created_at ON research_board_inputs(created_at DESC)')

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
        UNIQUE(order_id, order_sysid),
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')
    
    # account_commands（待取指令队列）已废弃：直连之后没有客户端来取指令。
    # 下单留痕改用 order_audit，做T开关改用 t0_status。旧库里的残留表不动，
    # 读它的代码已经全部删掉。

    # 为旧表添加唯一索引（如果不存在）
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_order_unique ON trades (order_id, order_sysid)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_trades_account_time ON trades (account_id, traded_time DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_trades_time ON trades (traded_time DESC)')

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

    # 创建历史资产表（用于资金曲线）
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

    # 创建历史资产索引
    cursor.execute('''
    CREATE INDEX IF NOT EXISTS idx_asset_history_account_time
    ON asset_history (account_id, record_time)
    ''')

    # 为 assets 表添加唯一索引（如果不存在）
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_account_unique ON assets (account_id)')

    # 创建持仓索引
    cursor.execute('''
    CREATE INDEX IF NOT EXISTS idx_positions_account_time
    ON positions (account_id, update_time)
    ''')
    
    # 创建登录尝试记录表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS login_attempts (
        ip TEXT PRIMARY KEY,
        attempts INTEGER DEFAULT 0,
        first_failure_time TIMESTAMP,
        blocked_until TIMESTAMP
    )
    ''')

    # 创建每日盈亏缓存表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS daily_profits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT,
        date TEXT, -- 格式：YYYY-MM-DD
        daily_profit REAL, -- 当日盈亏
        profit_rate REAL, -- 当日收益率
        total_asset REAL, -- 当日收盘总资产
        capital_adjustment REAL DEFAULT 0, -- 当日资金调整
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(account_id, date) ON CONFLICT REPLACE,
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')

    # 创建资金调整记录表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS capital_adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT,
        amount REAL, -- 正数表示增加资金，负数表示减少资金
        remark TEXT,
        adjust_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')
    
    # 创建持仓锁定表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS position_locks (
        account_id TEXT,
        stock_code TEXT,
        is_locked INTEGER DEFAULT 0,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (account_id, stock_code),
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')

    # 创建做T状态持久表（页面刷新后仍能恢复做T开关状态）
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS t0_status (
        account_id TEXT,
        stock_code TEXT,
        enabled INTEGER DEFAULT 1,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (account_id, stock_code),
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')

    # 创建交易状态控制表（停止交易 / 恢复交易）
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS trading_status (
        account_id TEXT PRIMARY KEY,
        is_stopped INTEGER DEFAULT 0,
        buy_stopped INTEGER DEFAULT 0,
        sell_stopped INTEGER DEFAULT 0,
        stopped_at TIMESTAMP,
        resumed_at TIMESTAMP,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users (account_id)
    )
    ''')
    # 兼容旧表：如果列不存在则添加
    try:
        cursor.execute('ALTER TABLE trading_status ADD COLUMN buy_stopped INTEGER DEFAULT 0')
    except Exception:
        pass
    try:
        cursor.execute('ALTER TABLE trading_status ADD COLUMN sell_stopped INTEGER DEFAULT 0')
    except Exception:
        pass
    # 迁移旧数据: is_stopped=1 的行补齐 buy_stopped/sell_stopped
    try:
        cursor.execute('UPDATE trading_status SET buy_stopped=1, sell_stopped=1 WHERE is_stopped=1 AND (buy_stopped IS NULL OR buy_stopped=0 OR sell_stopped IS NULL OR sell_stopped=0)')
    except Exception:
        pass

    # 活动委托表：直连之后才有的东西。改造前服务端只看得到成交，
    # 「挂着没成的单」完全是盲区，撤单更无从谈起。
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        account_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        order_sysid TEXT DEFAULT '',
        stock_code TEXT,
        instrument_name TEXT,
        order_type INTEGER,
        order_status INTEGER,
        order_volume INTEGER,
        traded_volume INTEGER,
        price REAL,
        traded_price REAL,
        order_time INTEGER,
        strategy_name TEXT,
        order_remark TEXT,
        status_msg TEXT,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (account_id, order_id)
    )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_account_time ON orders (account_id, order_time DESC)')

    # 下单审计流水：谁、什么时候、下了什么单、成没成、失败原因。
    # 改造前下单失败发生在 QMT 客户端里，服务端根本不知道；直连之后每一笔的
    # 结果都在服务端手上，必须留痕。
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS order_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        stock_code TEXT,
        side TEXT,
        volume INTEGER,
        price REAL,
        price_type TEXT,
        order_sys_id TEXT,
        status TEXT,
        message TEXT,
        operator TEXT,
        remark TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_order_audit_account_time ON order_audit (account_id, created_at DESC)')

    conn.commit()
    conn.close()

# 初始化数据库
init_db()

def migrate_daily_profits_unique():
    """将旧库的 daily_profits UNIQUE 约束迁移为 ON CONFLICT REPLACE，避免导入时报错"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_profits'")
        row = cursor.fetchone()
        if row and 'ON CONFLICT REPLACE' not in (row[0] or ''):
            cursor.executescript('''
                PRAGMA foreign_keys = OFF;
                BEGIN;
                CREATE TABLE daily_profits_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT,
                    date TEXT,
                    daily_profit REAL,
                    profit_rate REAL,
                    total_asset REAL,
                    capital_adjustment REAL DEFAULT 0,
                    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_id, date) ON CONFLICT REPLACE,
                    FOREIGN KEY (account_id) REFERENCES users (account_id)
                );
                INSERT OR REPLACE INTO daily_profits_new
                    SELECT id, account_id, date, daily_profit, profit_rate,
                           total_asset, capital_adjustment, update_time
                    FROM daily_profits;
                DROP TABLE daily_profits;
                ALTER TABLE daily_profits_new RENAME TO daily_profits;
                COMMIT;
                PRAGMA foreign_keys = ON;
            ''')
            conn.commit()
            print("[迁移] daily_profits 已升级为 ON CONFLICT REPLACE")
        conn.close()
    except Exception as e:
        print(f"[迁移] daily_profits 迁移出错: {e}")

migrate_daily_profits_unique()

# 密码哈希辅助函数
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

# 生成JWT令牌
def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# 获取用户信息
def get_user(username: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE username = ? AND (if_delete != 1 OR if_delete IS NULL)', (username,))
    user = cursor.fetchone()
    conn.close()
    if user:
        columns = ["id", "account_id", "username", "password", "role", "account_name", "alias", "created_at", "if_delete"]
        return dict(zip(columns, user))
    return None

# 获取用户信息通过account_id
def get_user_by_account_id(account_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE account_id = ?', (account_id,))
    user = cursor.fetchone()
    conn.close()
    if user:
        columns = ["id", "account_id", "username", "password", "role", "account_name", "alias", "created_at", "if_delete"]
        return dict(zip(columns, user))
    return None

# 登录限制相关辅助函数
def get_login_attempts(ip: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT attempts, first_failure_time, blocked_until FROM login_attempts WHERE ip = ?', (ip,))
    row = cursor.fetchone()
    conn.close()
    if row:
        # 转换为 datetime 对象
        first_failure = datetime.fromisoformat(row[1]) if row[1] else None
        blocked_until = datetime.fromisoformat(row[2]) if row[2] else None
        return {"attempts": row[0], "first_failure_time": first_failure, "blocked_until": blocked_until}
    return None

def update_login_attempts(ip: str, attempts: int, first_failure_time: datetime, blocked_until: datetime = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
    INSERT OR REPLACE INTO login_attempts (ip, attempts, first_failure_time, blocked_until)
    VALUES (?, ?, ?, ?)
    ''', (ip, attempts, 
          first_failure_time.isoformat() if first_failure_time else None, 
          blocked_until.isoformat() if blocked_until else None))
    conn.commit()
    conn.close()

def reset_login_attempts(ip: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM login_attempts WHERE ip = ?', (ip,))
    conn.commit()
    conn.close()

# 会话管理
def save_user_session(username: str, token: str, expires_delta: timedelta):
    conn = get_db_connection()
    cursor = conn.cursor()
    expires_at = datetime.utcnow() + expires_delta
    cursor.execute('''
    INSERT INTO user_sessions (username, token, expires_at)
    VALUES (?, ?, ?)
    ''', (username, token, expires_at.isoformat()))
    conn.commit()
    conn.close()

def verify_user_session(token: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT username, expires_at FROM user_sessions WHERE token = ?', (token,))
    row = cursor.fetchone()
    conn.close()
    if row:
        username, expires_at_str = row
        expires_at = datetime.fromisoformat(expires_at_str)
        if datetime.utcnow() < expires_at:
            return username
    return None

def delete_user_session(token: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM user_sessions WHERE token = ?', (token,))
    conn.commit()
    conn.close()

def clean_expired_sessions():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM user_sessions WHERE expires_at < ?', (datetime.utcnow().isoformat(),))
    conn.commit()
    conn.close()

# 记录最近一次"通过认证的请求"时间，用于判断当前是否有用户在使用看板
# （前端登录后每 ~30 秒轮询，会持续刷新此时间；无人使用则实时行情任务不刷新）
_LAST_AUTH_TS = {"ts": 0.0}


def has_active_user(window_seconds=180):
    """最近 window_seconds 内有过通过认证的请求，则认为有用户在线使用。"""
    return (time.time() - _LAST_AUTH_TS["ts"]) < window_seconds


# 验证用户并获取当前用户
async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    # 首先尝试从数据库验证 session
    username = verify_user_session(token)
    
    if not username:
        # 如果数据库没有，再尝试 JWT 解码验证（作为备份或兼容性处理）
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            if payload.get("type") == "viewer":   # 观察者令牌不得访问任何账户接口
                raise credentials_exception
            username: str = payload.get("sub")
            if username is None:
                raise credentials_exception
        except JWTError:
            raise credentials_exception
            
    user = get_user(username=username)
    if user is None:
        raise credentials_exception
    _LAST_AUTH_TS["ts"] = time.time()   # 标记有用户在线（供实时行情任务判断）
    return user

# 验证是否为管理员
async def get_current_admin(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return current_user

# ============ 观察者(viewer) 独立体系：注册/登录/鉴权/在线统计（只允许看历史买入列表） ============
_VIEWER_LAST_BEAT = {}  # username -> 上次心跳时间戳（内存，用于累计在线时长）

def get_viewer(username: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT id, username, password FROM viewer_users WHERE username = ?', (username,))
    row = cur.fetchone()
    conn.close()
    return {"id": row[0], "username": row[1], "password": row[2]} if row else None

def save_viewer(username: str, password: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('INSERT INTO viewer_users (username, password) VALUES (?, ?)',
                (username, get_password_hash(password)))
    conn.commit()
    conn.close()

def record_viewer_login(username: str, ip: str = None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('INSERT INTO viewer_logins (username, ip, login_time) VALUES (?, ?, ?)',
                (username, ip, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()

_VIEWER_IP_LAST_BEAT = {}  # (username, ip) -> ts，按 IP 累计在线时长

def add_viewer_ip_online(username: str, ip: str, seconds: int):
    """按 IP 累计在线时长 + 刷新该 IP 最近活跃时间（seconds<=0 时只刷新 last_seen）。"""
    today = datetime.now().strftime('%Y-%m-%d')
    now_s = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    cur = conn.cursor()
    if seconds and seconds > 0:
        cur.execute('INSERT OR IGNORE INTO viewer_ip_online (username, ip, date, online_seconds) VALUES (?, ?, ?, 0)', (username, ip, today))
        cur.execute('UPDATE viewer_ip_online SET online_seconds = online_seconds + ? WHERE username=? AND ip=? AND date=?', (seconds, username, ip, today))
    cur.execute('INSERT OR REPLACE INTO viewer_ip_last_seen (username, ip, last_seen) VALUES (?, ?, ?)', (username, ip, now_s))
    conn.commit()
    conn.close()

def detect_viewer_concurrency(username: str, ip: str):
    """本 IP 心跳时，若同账号另一 IP 近 90s 也活跃 → 记一次并发（每 5 分钟最多记一次，避免刷屏）。"""
    try:
        now = datetime.now()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT ip, last_seen FROM viewer_ip_last_seen WHERE username=? AND ip!=?", (username, ip))
        other_ip = None
        for oip, oseen in cur.fetchall():
            if not oseen:
                continue
            try:
                dt = datetime.strptime(str(oseen)[:19], '%Y-%m-%d %H:%M:%S')
            except Exception:
                continue
            if (now - dt).total_seconds() <= 90:
                other_ip = oip
                break
        if other_ip:
            cur.execute("SELECT MAX(event_time) FROM viewer_concurrency_log WHERE username=?", (username,))
            last_evt = cur.fetchone()[0]
            recent = False
            if last_evt:
                try:
                    recent = (now - datetime.strptime(str(last_evt)[:19], '%Y-%m-%d %H:%M:%S')).total_seconds() < 300
                except Exception:
                    recent = False
            if not recent:
                cur.execute("INSERT INTO viewer_concurrency_log (username, ip_a, ip_b, event_time) VALUES (?, ?, ?, ?)",
                            (username, ip, other_ip, now.strftime('%Y-%m-%d %H:%M:%S')))
                conn.commit()
        conn.close()
    except Exception as e:
        print(f"[并发检测] 失败: {e}")

def record_user_login(account_id: str, username: str = None):
    """记录交易账户登录（近30天登录次数统计用）。"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('INSERT INTO user_logins (account_id, username, login_time) VALUES (?, ?, ?)',
                    (account_id, username, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[登录统计] 记录账户登录失败: {e}")

def add_viewer_online_seconds(username: str, seconds: int):
    if seconds <= 0:
        return
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO viewer_daily_online (username, date, online_seconds) VALUES (?, ?, 0)',
                (username, today))
    cur.execute('UPDATE viewer_daily_online SET online_seconds = online_seconds + ? WHERE username = ? AND date = ?',
                (seconds, username, today))
    conn.commit()
    conn.close()

async def get_current_viewer(token: str = Depends(oauth2_scheme)):
    """校验观察者令牌（type=viewer 的 JWT）。"""
    credentials_exception = HTTPException(status_code=401, detail="Could not validate credentials",
                                          headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "viewer":
            raise credentials_exception
        username = payload.get("sub")
    except JWTError:
        raise credentials_exception
    if not username or not get_viewer(username):
        raise credentials_exception
    _LAST_AUTH_TS["ts"] = time.time()
    return {"username": username, "role": "viewer", "is_viewer": True}

async def get_user_or_viewer(token: str = Depends(oauth2_scheme)):
    """历史买入列表：账户用户 或 观察者 均可访问。"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") == "viewer":
            username = payload.get("sub")
            if username and get_viewer(username):
                _LAST_AUTH_TS["ts"] = time.time()
                return {"username": username, "role": "viewer", "is_viewer": True}
            raise HTTPException(status_code=401, detail="Could not validate credentials",
                                headers={"WWW-Authenticate": "Bearer"})
    except JWTError:
        pass
    return await get_current_user(token)

# 保存用户（支持注册）
def save_user(account_id: str, username: str = None, password: str = None, account_name: str = None, role: str = "user"):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if username and password:
        # 注册新用户
        hashed_password = get_password_hash(password)
        cursor.execute('''
        INSERT OR IGNORE INTO users (account_id, username, password, role, account_name) 
        VALUES (?, ?, ?, ?, ?)''', (account_id, username, hashed_password, role, account_name))
    else:
        # 更新现有用户
        cursor.execute('''
        INSERT OR IGNORE INTO users (account_id, role, account_name) 
        VALUES (?, ?, ?)''', (account_id, role, account_name))
    
    conn.commit()
    conn.close()


def _normalize_trade_factor(value, field_name: str) -> float:
    """规范化仓位/开仓数量倍率，允许范围 0-1.5。"""
    try:
        factor = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field_name} 必须是数字")

    if factor < 0 or factor > 1.5:
        raise HTTPException(status_code=400, detail=f"{field_name} 取值范围必须在 0 到 1.5 之间")

    return round(factor, 4)


def _safe_db_trade_factor(value, default: float = 1.0) -> float:
    """读取数据库中的倍率值，异常时回退到默认值。"""
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return default

    if factor < 0 or factor > 1.5:
        return default
    return round(factor, 4)


def _list_active_account_ids(cursor):
    cursor.execute('''
        SELECT account_id FROM users
        WHERE (if_delete != 1 OR if_delete IS NULL)
          AND account_id IS NOT NULL
          AND TRIM(account_id) != ''
    ''')
    return [row[0] for row in cursor.fetchall()]

# 保存持仓数据
def save_positions(account_id, positions):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 删除当天已存在的旧持仓数据（保留历史快照，仅更新当天最新快照）
    cursor.execute('DELETE FROM positions WHERE account_id = ? AND date(update_time) = ?', (account_id, today))
    
    # 插入新持仓数据
    for position in positions:
        cursor.execute('''
        INSERT INTO positions (
            account_id, account_type, avg_price, can_use_volume, direction, float_profit, 
            frozen_volume, instrument_name, last_price, market_value, on_road_volume, 
            open_date, open_price, position_profit, profit_rate, current_change, topic_reason, secu_account, stock_code, 
            volume, yesterday_volume, update_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            account_id, position.get('account_type'), position.get('avg_price'), 
            position.get('can_use_volume'), position.get('direction'), position.get('float_profit'),
            position.get('frozen_volume'), position.get('instrument_name'), position.get('last_price'),
            position.get('market_value'), position.get('on_road_volume'), position.get('open_date'),
            position.get('open_price'), position.get('position_profit'), position.get('profit_rate'),
            position.get('current_change'), position.get('topic_reason'),
            position.get('secu_account'), position.get('stock_code'), position.get('volume'),
            position.get('yesterday_volume'), now
        ))
    
    conn.commit()
    conn.close()

def update_qmt_price_cache(positions):
    """用QMT推送的持仓last_price更新实时价格缓存，合成分钟OHLC"""
    global GLOBAL_QMT_PRICE_CACHE
    now = datetime.now()
    minute_str = now.strftime('%H:%M')
    
    for pos in positions:
        code = pos.get('stock_code')
        last_price = pos.get('last_price')
        if not code or last_price is None:
            continue
        
        if code not in GLOBAL_QMT_PRICE_CACHE:
            GLOBAL_QMT_PRICE_CACHE[code] = {
                'last_price': float(last_price),
                'update_time': now,
                'minute_ohlc': {}
            }
        else:
            cache = GLOBAL_QMT_PRICE_CACHE[code]
            cache['last_price'] = float(last_price)
            cache['update_time'] = now
        
        cache = GLOBAL_QMT_PRICE_CACHE[code]
        price = float(last_price)
        
        if minute_str not in cache['minute_ohlc']:
            cache['minute_ohlc'][minute_str] = {
                'open': price,
                'high': price,
                'low': price,
                'close': price
            }
        else:
            minute_data = cache['minute_ohlc'][minute_str]
            minute_data['high'] = max(minute_data['high'], price)
            minute_data['low'] = min(minute_data['low'], price)
            minute_data['close'] = price

def backfill_minute_bars(codes):
    """从大QMT 直接拉当日分钟线，填进走势图缓存。返回补齐了几个代码。

    改造前这事要绕一大圈：服务端往 account_commands 塞一条 backfill_kline 指令 →
    QMT 客户端下次轮询取走 → 客户端拉分钟涨跌幅 → 再 push 回服务端 → 服务端用昨收价
    把涨跌幅换算回价格。链条长、要等客户端轮询，昨收价拿不到时整只票直接放弃
    （旧日志里的「昨收价不存在，无法转换涨跌幅」）。
    现在一次 get_market_data_ex 拿到真实 OHLC，不需要昨收价，也不需要客户端配合。
    """
    global GLOBAL_MARKET_MIN_DATA_RAW, GLOBAL_MARKET_MIN_DATA
    wanted = [c for c in (codes or []) if c]
    if not wanted:
        return 0
    data = bridge_market.get_minute_bars(wanted, count=241, period="1m")
    if not data:
        return 0

    filled_codes = 0
    for code, bars in data.items():
        rows = _minute_bar_rows(bars)
        if not rows:
            continue
        bucket = GLOBAL_MARKET_MIN_DATA_RAW.setdefault(code, {})
        for time_str, ohlc in rows:
            bucket[time_str] = ohlc
        if bucket:
            ordered = sorted(bucket.keys())
            GLOBAL_MARKET_MIN_DATA[code] = [bucket[t]['close'] for t in ordered][-240:]
            filled_codes += 1
    if filled_codes:
        print(f"[回补] 从大QMT 补齐 {filled_codes} 只标的的分钟线")
    return filled_codes


def _minute_bar_rows(bars):
    """把 get_market_data_ex 的单只标的结果摊成 [(HH:MM, {open,high,low,close})]。

    大QMT 返回的可能是 DataFrame（时间做索引）也可能是 dict，两种都要吃得下；
    午休和集合竞价一并滤掉，跟原走势图口径一致。
    """
    if bars is None:
        return []
    try:
        records = bars.to_dict("index") if hasattr(bars, "to_dict") else dict(bars)
    except Exception:
        return []

    rows = []
    for raw_time, values in records.items():
        if not isinstance(values, dict):
            continue
        time_str = _bar_time_label(raw_time, values.get("time"))
        if not time_str or not ('09:30' <= time_str <= '15:00'):
            continue
        if '11:31' <= time_str <= '12:59':
            continue
        try:
            close = float(values.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        rows.append((time_str, {
            "open": float(values.get("open") or close),
            "high": float(values.get("high") or close),
            "low": float(values.get("low") or close),
            "close": close,
        }))
    return rows


def _bar_time_label(index_value, time_field):
    """从 K 线索引或 time 字段里取出 HH:MM。"""
    for candidate in (index_value, time_field):
        if candidate is None:
            continue
        text = str(candidate)
        if len(text) >= 12 and text[:8].isdigit():          # 20260830093000
            return "%s:%s" % (text[8:10], text[10:12])
        if ":" in text:                                      # 2026-08-30 09:30:00
            parts = text.split(" ")[-1].split(":")
            if len(parts) >= 2 and parts[0].isdigit():
                return "%s:%s" % (parts[0].zfill(2), parts[1])
        if text.isdigit() and len(text) >= 13:                # epoch 毫秒
            try:
                return datetime.fromtimestamp(int(text) / 1000).strftime("%H:%M")
            except (ValueError, OSError):
                continue
    return ""


def get_current_holding_codes(account_id=None):
    """当前实际持仓的股票代码（只取每账号最新快照的 volume>0），去重。
    positions 表是历史累积快照，不加最新快照过滤会把「曾经持有过」的股票全拉出来（几百上千只）。
    account_id=None 返回全体账号并集。"""
    conn = get_db_connection()
    cur = conn.cursor()
    if account_id:
        cur.execute('''SELECT DISTINCT stock_code FROM positions
                       WHERE account_id=? AND volume>0
                         AND update_time=(SELECT MAX(update_time) FROM positions WHERE account_id=?)''',
                    (account_id, account_id))
    else:
        cur.execute('''WITH latest AS (SELECT account_id, MAX(update_time) mt FROM positions GROUP BY account_id)
                       SELECT DISTINCT p.stock_code FROM positions p
                       JOIN latest l ON p.account_id=l.account_id AND p.update_time=l.mt
                       WHERE p.volume>0''')
    codes = [str(r[0]) for r in cur.fetchall() if r[0]]
    conn.close()
    return codes


def save_trades(account_id, trades):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 插入或更新交易数据
    # 注意：traded_time 仅在首次插入时写入，后续更新时保留原始成交时间，
    # 避免 QMT 批量推送时用当前推送时刻覆盖历史成交的真实执行时间。
    for trade in trades:
        cursor.execute('''
        INSERT INTO trades (
            account_id, account_type, commission, direction, instrument_name, offset_flag, 
            order_id, order_remark, order_sysid, order_type, secu_account, stock_code, 
            strategy_name, traded_amount, traded_id, traded_price, traded_time, traded_volume
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id, order_sysid) DO UPDATE SET
            account_id      = excluded.account_id,
            account_type    = excluded.account_type,
            commission      = excluded.commission,
            direction       = excluded.direction,
            instrument_name = excluded.instrument_name,
            offset_flag     = excluded.offset_flag,
            order_remark    = excluded.order_remark,
            order_type      = excluded.order_type,
            secu_account    = excluded.secu_account,
            stock_code      = excluded.stock_code,
            strategy_name   = excluded.strategy_name,
            traded_amount   = excluded.traded_amount,
            traded_id       = excluded.traded_id,
            traded_price    = excluded.traded_price,
            traded_volume   = excluded.traded_volume,
            update_time     = CURRENT_TIMESTAMP,
            traded_time     = CASE
                                WHEN trades.traded_time > 0 THEN trades.traded_time
                                ELSE excluded.traded_time
                              END
        ''', (
            account_id, trade.get('account_type'), trade.get('commission'), trade.get('direction'),
            trade.get('instrument_name'), trade.get('offset_flag'), trade.get('order_id'),
            trade.get('order_remark'), trade.get('order_sysid'), trade.get('order_type'),
            trade.get('secu_account'), trade.get('stock_code'), trade.get('strategy_name'),
            trade.get('traded_amount'), trade.get('traded_id'), trade.get('traded_price'),
            trade.get('traded_time'), trade.get('traded_volume')
        ))
    
    conn.commit()
    conn.close()

# 保存活动委托（直连后新增：改造前服务端看不到未成交的挂单）
def save_orders(account_id, orders, partial=False):
    """写入/更新委托。partial=True 用于实时回报的单条更新。

    委托按 (account_id, order_id) upsert。同一天里 QMT 会重复返回已完成的委托，
    upsert 天然幂等；跨天的旧委托由这里顺手清掉，不另开清理任务。
    """
    if not orders:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for order in orders:
            order_id = order.get("order_id")
            if order_id in (None, ""):
                continue
            cursor.execute('''
            INSERT INTO orders (
                account_id, order_id, order_sysid, stock_code, instrument_name,
                order_type, order_status, order_volume, traded_volume, price,
                traded_price, order_time, strategy_name, order_remark, status_msg,
                update_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, order_id) DO UPDATE SET
                order_sysid     = excluded.order_sysid,
                order_status    = excluded.order_status,
                traded_volume   = excluded.traded_volume,
                traded_price    = excluded.traded_price,
                status_msg      = excluded.status_msg,
                update_time     = CURRENT_TIMESTAMP,
                instrument_name = CASE WHEN excluded.instrument_name != ''
                                       THEN excluded.instrument_name
                                       ELSE orders.instrument_name END
            ''', (
                account_id, str(order_id), order.get("order_sysid", ""),
                order.get("stock_code"), order.get("instrument_name", ""),
                order.get("order_type"), order.get("order_status"),
                order.get("order_volume"), order.get("traded_volume"),
                order.get("price"), order.get("traded_price"),
                order.get("order_time"), order.get("strategy_name", ""),
                order.get("order_remark", ""), order.get("status_msg", ""),
            ))
        if not partial:
            cursor.execute(
                "DELETE FROM orders WHERE account_id = ? AND update_time < datetime('now', 'localtime', '-7 days')",
                (account_id,))
        conn.commit()
    finally:
        conn.close()


def save_order_audit(record):
    """下单/撤单审计落库。bridge.orders 每次下单后都会调到这里。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
        INSERT INTO order_audit (
            account_id, stock_code, side, volume, price, price_type,
            order_sys_id, status, message, operator, remark
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            record.get("account_id"), record.get("stock_code"), record.get("side"),
            record.get("volume"), record.get("price"), record.get("price_type"),
            str(record.get("order_sys_id") or ""), record.get("status"),
            record.get("message"), record.get("operator", ""), record.get("remark", ""),
        ))
        conn.commit()
    finally:
        conn.close()


def get_order_audit(account_id=None, limit=200):
    """审计流水查询，给管理页用。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if account_id and account_id != "all":
            cursor.execute('''
                SELECT account_id, stock_code, side, volume, price, price_type,
                       order_sys_id, status, message, operator, remark, created_at
                FROM order_audit WHERE account_id = ?
                ORDER BY id DESC LIMIT ?
            ''', (account_id, limit))
        else:
            cursor.execute('''
                SELECT account_id, stock_code, side, volume, price, price_type,
                       order_sys_id, status, message, operator, remark, created_at
                FROM order_audit ORDER BY id DESC LIMIT ?
            ''', (limit,))
        columns = ["account_id", "stock_code", "side", "volume", "price", "price_type",
                   "order_sys_id", "status", "message", "operator", "remark", "created_at"]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


# 保存资产数据
def save_asset(account_id, asset):
    conn = get_db_connection()
    cursor = conn.cursor()

    # 使用 INSERT OR REPLACE 替代 删除-插入 逻辑，性能更好且能保持索引完整性
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    now = datetime.now()
    
    # 自动资金调整（出入金）：仅在「盘前 08:00-09:15（不含 09:15）」与「盘后 15:00-16:00」记录。
    # 排除 09:15-09:30 集合竞价/开盘时段——这段是价格形成（市值变动），不是资金流，
    # 不能计入自动资金调整；盘中/夜间断线的瞬时坏数据也一律不记（避免误记巨额"资金变动"）。
    cursor.execute('SELECT total_asset FROM assets WHERE account_id = ?', (account_id,))
    last_asset_row = cursor.fetchone()
    last_total_asset = last_asset_row[0] if last_asset_row and last_asset_row[0] is not None else 0
    current_total_asset = asset.get('total_asset') or 0

    hm = now.hour * 100 + now.minute
    # 注意上界用 < 915：09:15 起进入集合竞价，9:15-9:30 不计入
    in_settlement_window = (800 <= hm < 915) or (1500 <= hm <= 1600)

    is_new_user = last_total_asset == 0 and current_total_asset > 0
    asset_change = current_total_asset - last_total_asset if not is_new_user else current_total_asset

    # 仅在结算时段、总资产>0、且变动金额 > 1 万元时才自动记录
    if in_settlement_window and current_total_asset > 0 and abs(asset_change) > 10000:
        # 检查今天是否已经自动添加过类似的调整（避免重复）
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cursor.execute('''
            SELECT COUNT(*) FROM capital_adjustments
            WHERE account_id = ? AND adjust_time >= ?
            AND remark LIKE '盘后资金变动%'
        ''', (account_id, today_start.strftime('%Y-%m-%d %H:%M:%S')))
        auto_adjust_count = cursor.fetchone()[0]
        if auto_adjust_count == 0:
            if is_new_user:
                remark = f'盘后资金变动自动调整 - 新增用户首次记录 (总资产: {current_total_asset:.2f})'
            else:
                remark = f'盘后资金变动自动调整 (变化前: {last_total_asset:.2f}, 变化后: {current_total_asset:.2f})'
            cursor.execute('''
                INSERT INTO capital_adjustments (account_id, amount, remark, adjust_time)
                VALUES (?, ?, ?, ?)
            ''', (account_id, asset_change, remark, now_str))
            print(f"[自动资金调整] 账户 {account_id}: 调整金额 {asset_change:.2f}")

    cursor.execute('''
    INSERT OR REPLACE INTO assets (
        account_id, account_type, cash, current_balance, fetch_balance, frozen_cash,
        market_value, total_asset, update_time
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        account_id, asset.get('account_type'), asset.get('cash'), asset.get('current_balance'),
        asset.get('fetch_balance'), asset.get('frozen_cash'), asset.get('market_value'),
        asset.get('total_asset'), now_str
    ))

    # 优化历史数据保存逻辑：如果距离上一条记录不到 1 分钟，则更新上一条记录，否则插入新记录
    # 这样可以防止高频 post (如每 5s 一次) 导致数据库记录爆炸
    cursor.execute('''
    SELECT id, record_time FROM asset_history 
    WHERE account_id = ? 
    ORDER BY record_time DESC LIMIT 1
    ''', (account_id,))
    last_record = cursor.fetchone()
    
    should_insert = True
    
    if last_record:
        last_id = last_record[0]
        last_time_str = last_record[1]
        try:
            # 解析记录的时间
            if isinstance(last_time_str, str):
                # 处理 sqlite 可能存储的不同时间格式
                if '.' in last_time_str:
                    last_time = datetime.strptime(last_time_str, '%Y-%m-%d %H:%M:%S.%f')
                else:
                    last_time = datetime.strptime(last_time_str, '%Y-%m-%d %H:%M:%S')
            else:
                last_time = last_time_str
                
            # 如果在 30 秒内，则更新上一条记录（防止短时间内重复推送导致的记录爆炸）
            # 注意：不更新 record_time，否则每次 merge 都重置时钟，导致永远无法触发 INSERT
            if (now - last_time).total_seconds() < 30:
                cursor.execute('''
                UPDATE asset_history 
                SET total_asset = ?, market_value = ?, cash = ?
                WHERE id = ?
                ''', (
                    asset.get('total_asset'), asset.get('market_value'), 
                    asset.get('cash'), last_id
                ))
                should_insert = False
        except Exception as e:
            print(f"解析历史记录时间出错: {e}")
            should_insert = True

    if should_insert and (asset.get('total_asset') or 0) > 0:
        cursor.execute('''
        INSERT INTO asset_history (
            account_id, total_asset, market_value, cash, record_time
        ) VALUES (?, ?, ?, ?, ?)
        ''', (
            account_id, asset.get('total_asset'), asset.get('market_value'),
            asset.get('cash'), now
        ))

    conn.commit()
    conn.close()

# 获取所有用户
def get_users():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT account_id FROM users')
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users


def get_locked_positions_for_account(account_id):
    """按账号读取锁定股票列表；汇总视图返回所有账号锁定股票的并集。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if account_id == 'all':
            cursor.execute('''
                SELECT DISTINCT stock_code
                FROM position_locks
                WHERE is_locked = 1
                ORDER BY stock_code
            ''')
        else:
            cursor.execute('''
                SELECT stock_code
                FROM position_locks
                WHERE account_id = ? AND is_locked = 1
                ORDER BY stock_code
            ''', (account_id,))
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def get_t0_enabled_stocks_for_account(account_id):
    """按账号读取已启用做T股票；汇总视图返回所有账号启用股票的并集。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if account_id == 'all':
            cursor.execute('''
                SELECT DISTINCT stock_code
                FROM t0_status
                WHERE enabled = 1
                ORDER BY stock_code
            ''')
        else:
            cursor.execute('''
                SELECT stock_code
                FROM t0_status
                WHERE account_id = ? AND enabled = 1
                ORDER BY stock_code
            ''', (account_id,))
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


# 获取用户持仓数据
def get_positions(account_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    # 确保 t0_status 表存在（兼容旧库升级）
    cursor.execute('''CREATE TABLE IF NOT EXISTS t0_status (
        account_id TEXT,
        stock_code TEXT,
        enabled INTEGER DEFAULT 1,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (account_id, stock_code)
    )''')
    
    if account_id == 'all':
        # 管理员查看所有持仓，按证券代码合并
        # 只纳入"活跃账户"：其最新持仓快照距离全局最新快照不超过 STALE_ACCOUNT_THRESHOLD_DAYS 天，
        # 避免已停止使用的账户的历史持仓污染汇总数据。
        cursor.execute('''
        WITH account_latest AS (
            SELECT account_id, MAX(update_time) AS latest_time
            FROM positions
            WHERE account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
            GROUP BY account_id
        ),
        global_latest AS (
            SELECT MAX(latest_time) AS global_max FROM account_latest
        )
        SELECT 
            p1.stock_code, 
            MAX(p1.instrument_name) as instrument_name, 
            SUM(p1.volume) as volume, 
            SUM(p1.can_use_volume) as can_use_volume,
            CASE WHEN SUM(p1.volume) > 0 THEN SUM(p1.avg_price * p1.volume) / SUM(p1.volume) ELSE 0 END as avg_price,
            SUM(p1.market_value) as market_value,
            SUM(p1.float_profit) as float_profit,
            SUM(p1.position_profit) as position_profit,
            -- 汇总盈亏率按成本加权（合计浮盈/合计成本）；不能用 MAX——否则会取到某个清仓/0成本账号的率，显示成 0.00% 或偏高
            CASE WHEN SUM(p1.avg_price * p1.volume) > 0
                 THEN SUM(p1.float_profit) / SUM(p1.avg_price * p1.volume)
                 ELSE 0 END as profit_rate,
            SUM(p1.yesterday_volume) as yesterday_volume,
            MAX(p1.last_price) as last_price,
            AVG(p1.current_change) as current_change,
            MAX(p1.account_type) as account_type,
            'ALL' as account_id,
            MAX(p1.topic_reason) as topic_reason,
            CASE WHEN MAX(COALESCE(l.is_locked, 0)) = 1 THEN 1 ELSE 0 END as is_locked,
            CASE WHEN MAX(COALESCE(t.enabled, 0)) = 1 THEN 1 ELSE 0 END as t0_enabled
        FROM positions p1
        JOIN account_latest al ON p1.account_id = al.account_id AND p1.update_time = al.latest_time
        CROSS JOIN global_latest gl
        LEFT JOIN position_locks l ON p1.account_id = l.account_id AND p1.stock_code = l.stock_code
        LEFT JOIN t0_status t ON p1.account_id = t.account_id AND p1.stock_code = t.stock_code
        WHERE al.latest_time >= datetime(gl.global_max, ? )
        GROUP BY p1.stock_code
        ''', (f'-{STALE_ACCOUNT_THRESHOLD_DAYS} days',))
    else:
        # 仅获取最新的持仓快照（避免显示历史重复数据），并关联锁定和做T状态
        cursor.execute('''
        SELECT p.*, COALESCE(l.is_locked, 0) as is_locked, COALESCE(t.enabled, 0) as t0_enabled
        FROM positions p
        LEFT JOIN position_locks l ON p.account_id = l.account_id AND p.stock_code = l.stock_code
        LEFT JOIN t0_status t ON p.account_id = t.account_id AND p.stock_code = t.stock_code
        WHERE p.account_id = ? 
        AND p.update_time = (SELECT MAX(update_time) FROM positions WHERE account_id = ?)
        ''', (account_id, account_id))
    
    columns = [desc[0] for desc in cursor.description]
    positions = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    
    # 计算5分钟涨跌幅 & 重算当日涨跌幅（全局缓存，不按用户区分）
    now = datetime.now()

    for pos in positions:
        stock_code = pos.get('stock_code')
        # 优先用“最新推送的持仓价” last_price（走势图末点也用它），没有再回退分钟缓存
        current_price = pos.get('last_price')
        if not current_price:
            mins = GLOBAL_MARKET_MIN_DATA.get(stock_code)
            if mins and len(mins) > 0 and mins[-1]:
                current_price = mins[-1]

        # 用 GLOBAL_MARKET_LAST_CLOSE 作为同一昨收基准重算当日涨跌幅
        prev_close = GLOBAL_MARKET_LAST_CLOSE.get(stock_code)
        if prev_close and prev_close > 0 and current_price:
            pos['current_change'] = round((float(current_price) - prev_close) / prev_close * 100, 2)

        if stock_code and current_price is not None:
            cached_data = price_5min_cache.setdefault(stock_code, {'history': deque()})

            # 兼容旧缓存结构，避免重启前后切换导致 KeyError
            if 'history' not in cached_data or not isinstance(cached_data['history'], deque):
                cached_data['history'] = deque()

            history = cached_data['history']
            history.append((now, current_price))

            # 清理过旧数据，同时保留一个窗口外的点用于“向前5分钟”定位
            retention_cutoff = now - timedelta(seconds=PRICE_HISTORY_RETENTION_SECONDS)
            while len(history) > 1 and history[1][0] < retention_cutoff:
                history.popleft()

            # 滑动窗口基准：取 <= now-5min 的最近价格；若不足5分钟，退化为最早点
            target_time = now - timedelta(seconds=PRICE_WINDOW_SECONDS)
            baseline_price = None
            for point_time, point_price in reversed(history):
                if point_time <= target_time:
                    baseline_price = point_price
                    break

            if baseline_price is None:
                baseline_price = history[0][1]

            if baseline_price > 0:
                change_5min = (current_price - baseline_price) / baseline_price * 100
                pos['change_5min'] = round(change_5min, 2)
            else:
                pos['change_5min'] = 0.0
        else:
            pos['change_5min'] = 0.0

    return positions

# 获取用户交易数据（最近7天）
def get_trades(account_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    if account_id == 'all':
        cursor.execute("SELECT * FROM trades WHERE account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1) ORDER BY traded_time DESC")
    else:
        cursor.execute('SELECT * FROM trades WHERE account_id = ? ORDER BY traded_time DESC', (account_id,))
    columns = [desc[0] for desc in cursor.description]
    trades = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return trades


def _t0_safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _t0_safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _trade_side(row):
    direction = _t0_safe_int(row.get("direction"))
    if direction == 23:
        return "buy"
    if direction == 24:
        return "sell"
    order_type = _t0_safe_int(row.get("order_type"))
    if order_type == 23:
        return "buy"
    if order_type == 24:
        return "sell"
    return ""


def _round_down_lot(volume):
    return max(0, int(_t0_safe_int(volume) // 100) * 100)


def _cached_price_for_code(stock_code):
    qmt_price = GLOBAL_QMT_PRICE_CACHE.get(stock_code, {}).get("last_price")
    if qmt_price:
        return _t0_safe_float(qmt_price), "qmt"

    minute_prices = GLOBAL_MARKET_MIN_DATA.get(stock_code)
    if minute_prices:
        latest = minute_prices[-1]
        if latest:
            return _t0_safe_float(latest), "minute"

    return 0.0, ""


def build_t0_scan_rows(trade_rows, position_rows, account_aliases=None):
    account_aliases = account_aliases or {}
    positions_by_key = {}
    for pos in position_rows or []:
        account_id = str(pos.get("account_id") or "")
        stock_code = str(pos.get("stock_code") or "")
        if not account_id or not stock_code:
            continue
        positions_by_key[(account_id, stock_code)] = pos

    stats_by_key = {}
    for trade in trade_rows or []:
        account_id = str(trade.get("account_id") or "")
        stock_code = str(trade.get("stock_code") or "")
        side = _trade_side(trade)
        if not account_id or not stock_code or side not in ("buy", "sell"):
            continue

        stat = stats_by_key.setdefault((account_id, stock_code), {
            "account_id": account_id,
            "stock_code": stock_code,
            "stock_name": trade.get("instrument_name") or "",
            "buy_volume": 0,
            "sell_volume": 0,
            "buy_amount": 0.0,
            "sell_amount": 0.0,
            "last_trade_time": 0
        })

        volume = _t0_safe_int(trade.get("traded_volume"))
        price = _t0_safe_float(trade.get("traded_price"))
        amount = _t0_safe_float(trade.get("traded_amount"))
        if amount <= 0 and volume > 0 and price > 0:
            amount = price * volume
        if volume <= 0:
            continue

        if side == "buy":
            stat["buy_volume"] += volume
            stat["buy_amount"] += amount
        else:
            stat["sell_volume"] += volume
            stat["sell_amount"] += amount

        stat["last_trade_time"] = max(stat["last_trade_time"], _t0_safe_int(trade.get("traded_time")))
        if trade.get("instrument_name"):
            stat["stock_name"] = trade.get("instrument_name")

    rows = []
    for key, stat in stats_by_key.items():
        account_id, stock_code = key
        pos = positions_by_key.get(key, {})
        buy_volume = stat["buy_volume"]
        sell_volume = stat["sell_volume"]
        buy_avg = stat["buy_amount"] / buy_volume if buy_volume > 0 else 0.0
        sell_avg = stat["sell_amount"] / sell_volume if sell_volume > 0 else 0.0
        matched_volume = min(buy_volume, sell_volume)
        realized_profit = (sell_avg - buy_avg) * matched_volume if buy_avg > 0 and sell_avg > 0 else 0.0
        net_volume = buy_volume - sell_volume

        current_price = _t0_safe_float(pos.get("last_price"))
        price_source = "position" if current_price > 0 else ""
        if current_price <= 0:
            current_price, price_source = _cached_price_for_code(stock_code)
        if current_price <= 0:
            current_price = buy_avg or sell_avg
            price_source = "trade_avg" if current_price > 0 else ""

        position_volume = _t0_safe_int(pos.get("volume"))
        can_use_volume = _t0_safe_int(pos.get("can_use_volume"))
        stock_name = pos.get("instrument_name") or stat.get("stock_name") or ""

        action = "观察"
        status = "无净成交"
        suggested_volume = 0
        expected_profit = 0.0
        reference_price = 0.0

        if net_volume > 0:
            action = "卖出"
            reference_price = buy_avg
            suggested_volume = _round_down_lot(min(net_volume, can_use_volume))
            if suggested_volume <= 0:
                status = "可用不足"
            elif current_price <= 0:
                status = "缺少现价"
            else:
                expected_profit = (current_price - buy_avg) * suggested_volume
                status = "可卖T" if expected_profit > 0 else "价差不足"
        elif net_volume < 0:
            action = "买入"
            reference_price = sell_avg
            suggested_volume = _round_down_lot(abs(net_volume))
            if suggested_volume <= 0:
                status = "低于整手"
            elif current_price <= 0:
                status = "缺少现价"
            else:
                expected_profit = (sell_avg - current_price) * suggested_volume
                status = "可买T" if expected_profit > 0 else "价差不足"
        elif matched_volume > 0:
            action = "已闭合"
            status = "已完成T"

        is_candidate = suggested_volume >= 100 and expected_profit > 0
        rows.append({
            "account_id": account_id,
            "account_alias": account_aliases.get(account_id) or account_id,
            "stock_code": stock_code,
            "stock_name": stock_name,
            "action": action,
            "status": status,
            "is_candidate": is_candidate,
            "suggested_volume": int(suggested_volume),
            "expected_t_profit": round(expected_profit, 2),
            "realized_t_profit": round(realized_profit, 2),
            "total_t_profit": round(realized_profit + expected_profit, 2),
            "buy_volume": int(buy_volume),
            "sell_volume": int(sell_volume),
            "net_volume": int(net_volume),
            "matched_volume": int(matched_volume),
            "buy_avg_price": round(buy_avg, 3),
            "sell_avg_price": round(sell_avg, 3),
            "reference_price": round(reference_price, 3),
            "current_price": round(current_price, 3),
            "price_source": price_source,
            "position_volume": int(position_volume),
            "can_use_volume": int(can_use_volume),
            "last_trade_time": stat.get("last_trade_time") or 0
        })

    rows.sort(key=lambda r: (
        0 if r["is_candidate"] else 1,
        -r["expected_t_profit"],
        -r["realized_t_profit"],
        r["account_alias"],
        r["stock_code"]
    ))
    return rows


def get_t0_scan(account_id, trade_date):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        account_aliases = {}
        if account_id == "all":
            cursor.execute('''
                SELECT account_id, COALESCE(NULLIF(alias, ''), NULLIF(account_name, ''), NULLIF(username, ''), account_id)
                FROM users
                WHERE COALESCE(if_delete, 0) != 1
                  AND COALESCE(is_dormant, 0) != 1
                  AND account_id IS NOT NULL
                  AND TRIM(account_id) != ''
            ''')
            user_rows = cursor.fetchall()
            account_ids = [row[0] for row in user_rows]
            account_aliases = {row[0]: row[1] for row in user_rows}
        else:
            account_ids = [account_id]
            cursor.execute('''
                SELECT COALESCE(NULLIF(alias, ''), NULLIF(account_name, ''), NULLIF(username, ''), account_id)
                FROM users
                WHERE account_id = ?
            ''', (account_id,))
            row = cursor.fetchone()
            account_aliases = {account_id: row[0] if row else account_id}

        if not account_ids:
            return [], {"total_accounts": 0, "traded_accounts": 0, "scanned_stocks": 0,
                        "candidate_count": 0, "candidate_profit": 0.0,
                        "completed_count": 0, "completed_profit": 0.0}

        placeholders = ",".join("?" for _ in account_ids)
        cursor.execute(f'''
            SELECT account_id, stock_code, instrument_name, direction, order_type,
                   traded_volume, traded_price, traded_amount, traded_time
            FROM trades
            WHERE date(traded_time, 'unixepoch', 'localtime') = ?
              AND account_id IN ({placeholders})
            ORDER BY traded_time DESC
        ''', [trade_date] + account_ids)
        trade_columns = [desc[0] for desc in cursor.description]
        trades = [dict(zip(trade_columns, row)) for row in cursor.fetchall()]

        cursor.execute(f'''
            WITH latest AS (
                SELECT account_id, MAX(update_time) AS latest_time
                FROM positions
                WHERE account_id IN ({placeholders})
                GROUP BY account_id
            )
            SELECT p.account_id, p.stock_code, p.instrument_name, p.volume, p.can_use_volume,
                   p.avg_price, p.last_price, p.market_value, p.update_time
            FROM positions p
            JOIN latest l ON p.account_id = l.account_id AND p.update_time = l.latest_time
        ''', account_ids)
        pos_columns = [desc[0] for desc in cursor.description]
        positions = [dict(zip(pos_columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()

    rows = build_t0_scan_rows(trades, positions, account_aliases)
    candidate_rows = [row for row in rows if row["is_candidate"]]
    completed_rows = [row for row in rows if row["action"] == "已闭合" and row["matched_volume"] > 0]
    summary = {
        "total_accounts": len(account_ids),
        "traded_accounts": len({row["account_id"] for row in rows}),
        "scanned_stocks": len(rows),
        "candidate_count": len(candidate_rows),
        "candidate_profit": round(sum(row["expected_t_profit"] for row in candidate_rows), 2),
        "completed_count": len(completed_rows),
        "completed_profit": round(sum(row["realized_t_profit"] for row in completed_rows), 2)
    }
    return rows, summary

# 获取用户资产数据
def get_asset(account_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    if account_id == 'all':
        cursor.execute('''
        SELECT 
            'ALL' as account_id,
            0 as account_type,
            SUM(cash) as cash,
            SUM(current_balance) as current_balance,
            SUM(fetch_balance) as fetch_balance,
            SUM(frozen_cash) as frozen_cash,
            SUM(market_value) as market_value,
            SUM(total_asset) as total_asset
        FROM assets
        WHERE account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        ''')
    else:
        cursor.execute('SELECT * FROM assets WHERE account_id = ?', (account_id,))
    columns = [desc[0] for desc in cursor.description]
    asset = cursor.fetchone()
    conn.close()
    return dict(zip(columns, asset)) if asset else {}


def _parse_asset_record_time(value):
    if isinstance(value, datetime):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                pass
    raise ValueError(f"Unsupported asset record_time: {value!r}")


def _floor_to_10min(dt):
    return dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)


def _asset_snapshot(record_time, latest_by_account):
    return {
        "record_time": record_time,
        "total_asset": sum(float(v.get("total_asset") or 0) for v in latest_by_account.values()),
        "market_value": sum(float(v.get("market_value") or 0) for v in latest_by_account.values()),
        "cash": sum(float(v.get("cash") or 0) for v in latest_by_account.values()),
    }


def _get_all_asset_history_rows(cursor, hours):
    """Build all-account history by carrying each account's last known value forward."""
    try:
        hours_value = float(hours)
    except (TypeError, ValueError):
        hours_value = 24
    window_start = datetime.now() - timedelta(hours=hours_value)
    window_start_str = window_start.strftime("%Y-%m-%d %H:%M:%S")

    latest_by_account = {}
    anchor_time = None
    cursor.execute('''
        SELECT account_id, total_asset, market_value, cash, record_time
        FROM (
            SELECT
                account_id,
                total_asset,
                market_value,
                cash,
                record_time,
                ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY record_time DESC) as rn
            FROM asset_history
            WHERE record_time < ?
              AND total_asset > 0
              AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        )
        WHERE rn = 1
    ''', (window_start_str,))
    for account_id, total_asset, market_value, cash, record_time in cursor.fetchall():
        dt = _parse_asset_record_time(record_time)
        anchor_time = dt if anchor_time is None or dt > anchor_time else anchor_time
        latest_by_account[account_id] = {
            "total_asset": total_asset,
            "market_value": market_value,
            "cash": cash,
        }

    raw_history = []
    if latest_by_account and anchor_time:
        raw_history.append(_asset_snapshot(anchor_time.strftime("%Y-%m-%d %H:%M"), latest_by_account))

    cursor.execute('''
        SELECT account_id, total_asset, market_value, cash, record_time
        FROM asset_history
        WHERE record_time >= ?
          AND total_asset > 0
          AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        ORDER BY record_time ASC
    ''', (window_start_str,))

    buckets = {}
    for account_id, total_asset, market_value, cash, record_time in cursor.fetchall():
        dt = _parse_asset_record_time(record_time)
        bucket_time = _floor_to_10min(dt)
        buckets.setdefault(bucket_time, []).append((
            dt,
            account_id,
            {
                "total_asset": total_asset,
                "market_value": market_value,
                "cash": cash,
            },
        ))

    for bucket_time in sorted(buckets):
        for _, account_id, values in sorted(buckets[bucket_time], key=lambda item: item[0]):
            latest_by_account[account_id] = values
        snapshot = _asset_snapshot(bucket_time.strftime("%Y-%m-%d %H:%M"), latest_by_account)
        if raw_history and raw_history[-1]["record_time"] == snapshot["record_time"]:
            raw_history[-1] = snapshot
        else:
            raw_history.append(snapshot)

    return raw_history


# 获取用户历史资产数据（用于资金曲线）
def get_asset_history(account_id, hours=24):
    conn = get_db_connection()
    cursor = conn.cursor()

    if account_id == 'all':
        raw_history = _get_all_asset_history_rows(cursor, hours)
    else:
        # 先取一个"窗口前锚点"：最近一条落在时间窗口之前的记录，
        # 防止长假后窗口内无历史数据导致曲线只剩1个点
        anchor_sql_time = "datetime('now', 'localtime', '-' || ? || ' hours')"
        cursor.execute(f'''
            SELECT strftime('%Y-%m-%d %H:%M', record_time) as record_time,
                   total_asset, market_value, cash
            FROM asset_history
            WHERE account_id = ? AND record_time < {anchor_sql_time}
              AND total_asset > 0
            ORDER BY record_time DESC LIMIT 1
        ''', (account_id, hours))

        anchor_rows = [dict(zip([d[0] for d in cursor.description], row))
                       for row in cursor.fetchall()]

        # 单个用户不进行聚合，显示原始数据点以获得最高实时性
        cursor.execute('''
        SELECT 
            strftime('%Y-%m-%d %H:%M', record_time) as record_time,
            total_asset, 
            market_value, 
            cash
        FROM asset_history
        WHERE account_id = ? AND record_time >= datetime('now', 'localtime', '-' || ? || ' hours')
        ORDER BY record_time ASC
        ''', (account_id, hours))

        columns = [desc[0] for desc in cursor.description]
        raw_history = [dict(zip(columns, row)) for row in cursor.fetchall()]

        # 将窗口前锚点插入到开头（如果锚点时间早于窗口内第一条记录）
        if anchor_rows:
            anchor = anchor_rows[0]
            anchor['total_asset'] = float(anchor.get('total_asset', 0) or 0)
            if anchor['total_asset'] > 0:
                if not raw_history or anchor['record_time'] < raw_history[0]['record_time']:
                    raw_history.insert(0, anchor)

    # 对连续重复值去重：保留每段连续相同 total_asset 的首尾两个点，
    # 避免大量相同值（如盘前未开盘时）拉平图表比例尺产生"直线+末端尖刺"
    def dedup_consecutive(records):
        if len(records) <= 2:
            return records
        result = [records[0]]
        for i in range(1, len(records) - 1):
            prev_val = records[i - 1]['total_asset']
            curr_val = records[i]['total_asset']
            next_val = records[i + 1]['total_asset']
            # 保留：值发生变化的点（即"边界点"）
            if curr_val != prev_val or curr_val != next_val:
                result.append(records[i])
        result.append(records[-1])
        return result

    history = dedup_consecutive(raw_history)

    # 实时追加当前最新的资产作为最后一个点
    if account_id == 'all':
        cursor.execute('''
            SELECT SUM(total_asset), SUM(market_value), SUM(cash)
            FROM assets
            WHERE total_asset > 0
              AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        ''')
        current = cursor.fetchone()
        if current and current[0] is not None:
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
            if not history or history[-1]['record_time'] != now_str:
                history.append({
                    "record_time": now_str,
                    "total_asset": current[0],
                    "market_value": current[1],
                    "cash": current[2]
                })
    else:
        cursor.execute('SELECT total_asset, market_value, cash FROM assets WHERE account_id = ?', (account_id,))
        current = cursor.fetchone()
        if current:
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
            if not history or history[-1]['record_time'] != now_str:
                history.append({
                    "record_time": now_str,
                    "total_asset": current[0],
                    "market_value": current[1],
                    "cash": current[2]
                })
    
    conn.close()
    return history

# 获取用户交易统计信息
def get_trade_stats(account_id, date_str=None):
    """
    返回: dict {stock_code: order_info}
    委托状态(order_status)
    # order_type: 23=买入, 24=卖出
    order_status: 
    枚举变量名	值	含义
    xtconstant.ORDER_UNREPORTED	48	未报
    xtconstant.ORDER_WAIT_REPORTING	49	待报
    xtconstant.ORDER_REPORTED	50	已报
    xtconstant.ORDER_REPORTED_CANCEL	51	已报待撤
    xtconstant.ORDER_PARTSUCC_CANCEL	52	部成待撤
    xtconstant.ORDER_PART_CANCEL	53	部撤（已经有一部分成交，剩下的已经撤单）
    xtconstant.ORDER_CANCELED	54	已撤
    xtconstant.ORDER_PART_SUCC	55	部成（已经有一部分成交，剩下的待成交）
    xtconstant.ORDER_SUCCEEDED	56	已成
    xtconstant.ORDER_JUNK	57	废单
    xtconstant.ORDER_UNKNOWN	255	未知  
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    if not date_str:
        # 如果没有提供日期，默认使用今天
        date_str = datetime.now().strftime("%Y-%m-%d")

    # 按指定日期统计
    if account_id == 'all':
        cursor.execute('''
        SELECT
            SUM(CASE WHEN direction =  24 OR order_type = 24 THEN traded_amount ELSE 0 END) as sell_amount,
            SUM(CASE WHEN direction = 23 OR order_type = 23 THEN traded_amount ELSE 0 END) as buy_amount,
            COUNT(CASE WHEN direction = 24 OR order_type = 24 THEN 1 END) as sell_count,
            COUNT(CASE WHEN direction =  23 OR order_type = 23 THEN 1 END) as buy_count
        FROM trades
        WHERE date(traded_time, 'unixepoch', 'localtime') = ?
          AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        ''', (date_str,))
    else:
        cursor.execute('''
        SELECT
            SUM(CASE WHEN direction =  24 OR order_type = 24 THEN traded_amount ELSE 0 END) as sell_amount,
            SUM(CASE WHEN direction = 23 OR order_type = 23 THEN traded_amount ELSE 0 END) as buy_amount,
            COUNT(CASE WHEN direction = 24 OR order_type = 24 THEN 1 END) as sell_count,
            COUNT(CASE WHEN direction =  23 OR order_type = 23 THEN 1 END) as buy_count
        FROM trades
        WHERE account_id = ? AND date(traded_time, 'unixepoch', 'localtime') = ?
        ''', (account_id, date_str))

    stats = cursor.fetchone()
    conn.close()

    return {
        'sell_amount': stats[0] or 0,
        'buy_amount': stats[1] or 0,
        'sell_count': stats[2] or 0,
        'buy_count': stats[3] or 0
    }

# 获取交易日期列表（用于日历标记）
def get_trade_dates(account_id, days=30):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if account_id == 'all':
        # 1. 获取最近30天内有交易的日期
        start_ts = int((datetime.now() - timedelta(days=days)).timestamp())
        cursor.execute('''
        SELECT DISTINCT date(traded_time, 'unixepoch', 'localtime') as trade_date
        FROM trades
        WHERE traded_time >= ?
          AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        ''', (start_ts,))
        trade_dates = [row[0] for row in cursor.fetchall()]

        # 1.1 获取期间所有的资金调整（排除休眠账户）
        # 注意：adjust_time 存储的已经是本地时间，不要再加 'localtime' 修饰符
        cursor.execute('''
        SELECT date(adjust_time) as adj_date, SUM(amount) as total_adj
        FROM capital_adjustments
        WHERE adjust_time >= datetime('now', 'localtime', '-' || ? || ' days')
          AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        GROUP BY adj_date
        ''', (days,))
        adjustments_map = {row[0]: row[1] for row in cursor.fetchall()}

        # 2. 获取汇总资产历史（每天取最后一次所有账号的资产总和）（排除休眠账户）
        # 注意：record_time 存储的已经是本地时间(Python datetime.now())，不要再加 'localtime'
        cursor.execute('''
        SELECT day, SUM(total_asset) as total_asset, SUM(market_value) as market_value, MAX(record_time) as last_time
        FROM (
            SELECT 
                date(record_time) as day,
                account_id,
                total_asset,
                market_value,
                record_time,
                ROW_NUMBER() OVER (PARTITION BY date(record_time), account_id ORDER BY record_time DESC) as rn
            FROM asset_history
            WHERE record_time >= datetime('now', 'localtime', '-' || ? || ' days') AND total_asset > 0
              AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        )
        WHERE rn = 1
        GROUP BY day
        ORDER BY day ASC
        ''', (days,))
        history_rows = cursor.fetchall()
        
        # 对于汇总，prev_total_asset 也需要汇总
        cursor.execute('''
        SELECT SUM(total_asset) 
        FROM asset_history p1
        WHERE total_asset > 0 AND record_time = (
            SELECT MAX(record_time) 
            FROM asset_history p2 
            WHERE p1.account_id = p2.account_id 
            AND total_asset > 0
            AND record_time < datetime('now', 'localtime', '-' || ? || ' days')
        )
        AND p1.account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        ''', (days,))
        base_asset_row = cursor.fetchone()
        prev_total_asset = base_asset_row[0] if base_asset_row else None
    else:
        # 1. 获取最近30天内有交易的日期
        start_ts = int((datetime.now() - timedelta(days=days)).timestamp())
        cursor.execute('''
        SELECT DISTINCT date(traded_time, 'unixepoch', 'localtime') as trade_date
        FROM trades
        WHERE account_id = ? AND traded_time >= ?
        ''', (account_id, start_ts))
        trade_dates = [row[0] for row in cursor.fetchall()]

        # 1.1 获取期间所有的资金调整
        # 注意：adjust_time 存储的已经是本地时间，不要再加 'localtime' 修饰符
        cursor.execute('''
        SELECT date(adjust_time) as adj_date, SUM(amount) as total_adj
        FROM capital_adjustments
        WHERE account_id = ? AND adjust_time >= datetime('now', 'localtime', '-' || ? || ' days')
        GROUP BY adj_date
        ''', (account_id, days))
        adjustments_map = {row[0]: row[1] for row in cursor.fetchall()}

        # 2. 获取资产历史
        # 强化基准资产获取：查找窗口期之前的最后一条记录
        cursor.execute('''
        SELECT total_asset 
        FROM asset_history 
        WHERE account_id = ? AND total_asset > 0 AND record_time < datetime('now', 'localtime', '-' || ? || ' days')
        ORDER BY record_time DESC LIMIT 1
        ''', (account_id, days))
        base_asset_row = cursor.fetchone()
        prev_total_asset = base_asset_row[0] if base_asset_row else None

        # 2. 获取资产历史 - 使用子查询确保获取的是每天最后一条记录的准确数据
        # 注意：record_time 存储的已经是本地时间(Python datetime.now())，不要再加 'localtime'
        cursor.execute('''
        SELECT day, total_asset, market_value, last_time
        FROM (
            SELECT 
                date(record_time) as day,
                total_asset,
                market_value,
                record_time as last_time,
                ROW_NUMBER() OVER (PARTITION BY date(record_time) ORDER BY record_time DESC) as rn
            FROM asset_history
            WHERE account_id = ? AND total_asset > 0 AND record_time >= datetime('now', 'localtime', '-' || ? || ' days')
        )
        WHERE rn = 1
        ORDER BY day ASC
        ''', (account_id, days))
        history_rows = cursor.fetchall()
    
    daily_stats = {}
    for row in history_rows:
        day, total, market, _ = row
        daily_stats[day] = {
            'total_asset': total,
            'market_value': market,
            'position_ratio': (market / total * 100) if total > 0 else 0,
            'profit': None
        }

    # 3. 优先从 daily_profits 缓存表读取盈亏（已在启动时重新计算过）
    if account_id == 'all':
        cursor.execute('''
            SELECT date, SUM(daily_profit) as daily_profit, SUM(total_asset) as total_asset
            FROM daily_profits
            WHERE date >= date('now', 'localtime', '-' || ? || ' days')
              AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
            GROUP BY date
        ''', (days,))
    else:
        cursor.execute('''
            SELECT date, daily_profit, profit_rate, total_asset
            FROM daily_profits
            WHERE account_id = ? AND date >= date('now', 'localtime', '-' || ? || ' days')
        ''', (account_id, days))
    cached_profits = {}
    for row in cursor.fetchall():
        if account_id == 'all':
            d, dp, ta = row
            cached_profits[d] = {'daily_profit': dp, 'total_asset': ta}
        else:
            d, dp, pr, ta = row
            cached_profits[d] = {'daily_profit': dp, 'profit_rate': pr, 'total_asset': ta}

    # 将缓存盈亏写入 daily_stats，缓存中没有的日期再用在线计算兜底
    for d in list(daily_stats.keys()):
        if d in cached_profits:
            cp = cached_profits[d]
            daily_stats[d]['profit'] = cp['daily_profit']
            ta = cp.get('total_asset') or daily_stats[d]['total_asset']
            if account_id == 'all':
                # all 视图没有单独的 profit_rate，用 profit/ta 估算
                daily_stats[d]['profit_rate'] = (cp['daily_profit'] / ta * 100) if ta else 0
            else:
                daily_stats[d]['profit_rate'] = cp.get('profit_rate', 0)

    # 对 daily_profits 中没有命中的日期（如今天），使用在线计算兜底
    today_str = datetime.now().strftime('%Y-%m-%d')
    fallback_days = [d for d in daily_stats if daily_stats[d]['profit'] is None]
    if fallback_days:
        # 今天单独用 calculate_today_profit_info 计算（逐账户求基准再汇总），
        # 避免 all 视图因周末/节假日部分账户漏报导致的基准不一致问题
        if today_str in fallback_days:
            today_info = calculate_today_profit_info(account_id)
            daily_stats[today_str]['profit'] = today_info['today_profit']
            daily_stats[today_str]['profit_rate'] = today_info['today_profit_rate']
            fallback_days = [d for d in fallback_days if d != today_str]

        # 其余非今天的 fallback 日使用顺序累计逻辑兜底
        if fallback_days:
            sorted_days = sorted(daily_stats.keys())
            all_involved_dates = sorted(list(set(sorted_days) | set(adjustments_map.keys())))
            pending_adjustment = 0
            running_prev = prev_total_asset

            for d in all_involved_dates:
                adj_amount = adjustments_map.get(d, 0)
                if d in daily_stats:
                    if daily_stats[d]['profit'] is None:
                        # 需要在线计算
                        if running_prev is not None:
                            base = running_prev + (adj_amount + pending_adjustment)
                            daily_stats[d]['profit'] = daily_stats[d]['total_asset'] - base
                            daily_stats[d]['profit_rate'] = (daily_stats[d]['profit'] / base * 100) if base > 0 else 0
                        else:
                            daily_stats[d]['profit'] = 0
                            daily_stats[d]['profit_rate'] = 0
                    # 已有 cached_profits 的日期，用 cached 的 total_asset 作为基准（更完整）
                    if d in cached_profits and cached_profits[d].get('total_asset'):
                        running_prev = cached_profits[d]['total_asset']
                    else:
                        running_prev = daily_stats[d]['total_asset']
                    pending_adjustment = 0
                else:
                    pending_adjustment += adj_amount

    # 今天的持仓比例/总资产改用实时 assets 表：asset_history 当天最后一条可能是断线/盘前
    # 异常快照（现金≈0 → 持仓比例失真为 99%+），改用实时资产表与顶部卡片保持一致。
    if today_str in daily_stats:
        try:
            if account_id == 'all':
                cursor.execute('''
                    SELECT SUM(total_asset), SUM(market_value) FROM assets
                    WHERE account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
                ''')
            else:
                cursor.execute('SELECT total_asset, market_value FROM assets WHERE account_id = ?', (account_id,))
            live = cursor.fetchone()
            if live and live[0] and live[0] > 0:
                live_total = float(live[0])
                live_market = float(live[1] or 0)
                daily_stats[today_str]['total_asset'] = live_total
                daily_stats[today_str]['market_value'] = live_market
                daily_stats[today_str]['position_ratio'] = live_market / live_total * 100
        except Exception as e:
            print(f"[日历] 今日持仓比例实时计算失败: {e}")

    # 4. 组合结果 (返回所有有交易、有资产记录或有资金调整的日子)
    all_dates = sorted(list(set(trade_dates) | set(daily_stats.keys()) | set(adjustments_map.keys())), reverse=True)
    result = []
    for d in all_dates:
        stats = daily_stats.get(d, {})
        result.append({
            'date': d,
            'has_trade': d in trade_dates,
            'has_adjustment': d in adjustments_map,
            'total_asset': stats.get('total_asset'),
            'profit': stats.get('profit'),
            'profit_rate': stats.get('profit_rate', 0),
            'position_ratio': stats.get('position_ratio', 0)
        })

    conn.close()
    return result

# 批量获取昨收价（每天早上拉一次）
def get_prev_trade_date():
    """用 trade_cal 获取上一个交易日 (YYYYMMDD)"""
    try:
        today = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=15)).strftime('%Y%m%d')
        df_cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=today, is_open='1')
        if df_cal is not None and not df_cal.empty:
            dates = sorted(df_cal['cal_date'].tolist())
            # 排除今天，取最后一个交易日
            past_dates = [d for d in dates if d < today]
            if past_dates:
                return past_dates[-1]
    except Exception as e:
        print(f"获取交易日历失败: {e}")
    # fallback: 往前推一天
    return (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')


def fetch_last_close_batch(codes=None):
    """昨收价缓存：股票用 pro.daily；ETF 用 etf_type_snapshot（pro.daily 不含 ETF，
    否则 ETF 回补/涨跌幅换算会报「昨收价不存在」）。code 均为带后缀 ts_code。"""
    global GLOBAL_MARKET_LAST_CLOSE
    prev_date = get_prev_trade_date()
    print(f"拉取昨收价，基准交易日: {prev_date}")
    # ---- 股票 ----
    try:
        df = pro.daily(trade_date=prev_date)
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = row['ts_code']
                if pd.notna(row['close']) and float(row['close']) > 0:
                    GLOBAL_MARKET_LAST_CLOSE[code] = float(row['close'])
            print(f"昨收价（股票）更新完成（{prev_date}），共 {len(GLOBAL_MARKET_LAST_CLOSE)} 只")
        else:
            print(f"昨收价（股票）数据为空，trade_date={prev_date}")
    except Exception as e:
        print(f"获取股票昨收价失败: {e}")
    # ---- ETF（etf_type_snapshot 有 code/close，pro.daily 覆盖不到）----
    try:
        today = datetime.now().strftime('%Y%m%d')
        conn = get_stock_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT code, close FROM etf_type_snapshot WHERE trade_date=%s", (prev_date,))
        rows = cur.fetchall()
        used = prev_date
        if not rows:   # prev_date 当天快照还没有，退到 < today 的最近一个快照日
            cur.execute("SELECT MAX(trade_date) FROM etf_type_snapshot WHERE trade_date < %s", (today,))
            latest = cur.fetchone()[0]
            if latest:
                cur.execute("SELECT code, close FROM etf_type_snapshot WHERE trade_date=%s", (latest,))
                rows = cur.fetchall()
                used = latest
        conn.close()
        n = 0
        for code, close in rows:
            code = (code or "").strip()
            if code and close is not None and float(close) > 0:
                GLOBAL_MARKET_LAST_CLOSE[code] = float(close)
                n += 1
        print(f"昨收价（ETF）补充完成（{used}），共 {n} 只")
    except Exception as e:
        print(f"获取 ETF 昨收价失败: {e}")

def is_trade_date(date_str=None):
    """用 tushare trade_cal 判断 date_str（YYYY-MM-DD）是否为交易日。
    结果缓存到 _TRADE_DATE_CACHE，每天只用 tushare 查询一次。
    """
    global _TRADE_DATE_CACHE, _TRADE_DATE_FETCH_DATE
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')

    # 周末快速判断（非交易日）
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    if dt.weekday() >= 5:
        return False

    # 缓存命中
    if date_str in _TRADE_DATE_CACHE:
        return _TRADE_DATE_CACHE[date_str]

    # 每天只请求一次 tushare
    today_str = datetime.now().strftime('%Y-%m-%d')
    if _TRADE_DATE_FETCH_DATE == today_str and date_str in _TRADE_DATE_CACHE:
        return _TRADE_DATE_CACHE[date_str]

    try:
        date_compact = dt.strftime('%Y%m%d')
        # 拉取前后 15 天窗口，同时缓存附近日期
        start = (dt - timedelta(days=15)).strftime('%Y%m%d')
        end = (dt + timedelta(days=15)).strftime('%Y%m%d')
        df_cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
        if df_cal is not None and not df_cal.empty:
            trade_dates = set(df_cal['cal_date'].tolist())
            # 缓存窗口内的所有交易日判断
            for d in (dt - timedelta(days=15) + timedelta(n) for n in range(31)):
                d_str = d.strftime('%Y%m%d')
                d_iso = d.strftime('%Y-%m-%d')
                _TRADE_DATE_CACHE[d_iso] = d_str in trade_dates
            _TRADE_DATE_FETCH_DATE = today_str
            return _TRADE_DATE_CACHE[date_str]

        # fallback: 简单按工作日判断
        is_weekday = dt.weekday() < 5
        _TRADE_DATE_CACHE[date_str] = is_weekday
        return is_weekday
    except Exception as e:
        print(f"判断交易日失败: {e}")
        # 出错时按工作日降级判断
        is_weekday = dt.weekday() < 5
        _TRADE_DATE_CACHE[date_str] = is_weekday
        return is_weekday


# 定期获取行情数据 (akshare首次, tushare增量)
def pull_market_rt_min_task():
    global GLOBAL_MARKET_MIN_DATA, GLOBAL_MARKET_MIN_DATA_RAW, _LAST_CLOSE_FETCH_DATE
    while True:
        try:
            now = datetime.now()
            today_str = now.strftime('%Y-%m-%d')

            # 用 tushare 判断今天是否为交易日（包含法定节假日过滤）
            if not is_trade_date(today_str):
                time.sleep(300)  # 非交易日每5分钟检查一次即可
                continue

            # 交易日且在交易时间（9:30开始更新真实连续竞价数据）
            is_trading_time = (now.hour >= 9 and now.minute >= 30 or now.hour >= 10) and now.hour < 15 or (now.hour == 15 and now.minute <= 5)

            # 每天只拉一次昨收价（启动时或日期变更时）
            if _LAST_CLOSE_FETCH_DATE != today_str:
                fetch_last_close_batch()
                _LAST_CLOSE_FETCH_DATE = today_str

            # 当前持仓（最新快照，非历史累积）
            codes = get_current_holding_codes()

            # 无持仓时跳过, 降低轮询频率
            if not codes:
                if not is_trading_time:
                    time.sleep(300)  # 非交易时段无持仓, 每5分钟检查一次
                else:
                    time.sleep(60)
                continue

            import concurrent.futures

            def fetch_code(code):
                try:
                    # 确保缓存 key 存在
                    if code not in GLOBAL_MARKET_MIN_DATA_RAW:
                        GLOBAL_MARKET_MIN_DATA_RAW[code] = {}

                    if is_trading_time:
                        qmt_cache = GLOBAL_QMT_PRICE_CACHE.get(code)
                        use_qmt = False
                        if qmt_cache:
                            elapsed = (now - qmt_cache['update_time']).total_seconds()
                            if elapsed <= 60:
                                use_qmt = True

                        if use_qmt:
                            # 优先使用QMT数据合成OHLC
                            for time_str, ohlc in qmt_cache.get('minute_ohlc', {}).items():
                                if time_str not in GLOBAL_MARKET_MIN_DATA_RAW[code]:
                                    GLOBAL_MARKET_MIN_DATA_RAW[code][time_str] = dict(ohlc)
                                else:
                                    existing = GLOBAL_MARKET_MIN_DATA_RAW[code][time_str]
                                    existing['high'] = max(existing['high'], ohlc['high'])
                                    existing['low'] = min(existing['low'], ohlc['low'])
                                    existing['close'] = ohlc['close']
                        else:
                            # QMT数据超过1分钟未更新，回退到tushare
                            df_min = pro.rt_min(ts_code=code, freq='1MIN')
                            if df_min is not None and not df_min.empty:
                                for _, row in df_min.iterrows():
                                    time_str = str(row['time'])[:5]  # 统一为 HH:MM
                                    GLOBAL_MARKET_MIN_DATA_RAW[code][time_str] = {
                                        'open': float(row.get('open', row['close'])),
                                        'high': float(row.get('high', row['close'])),
                                        'low': float(row.get('low', row['close'])),
                                        'close': float(row['close'])
                                    }
                        time.sleep(0.05)

                    if code in GLOBAL_MARKET_MIN_DATA_RAW and GLOBAL_MARKET_MIN_DATA_RAW[code]:
                        sorted_times = sorted(GLOBAL_MARKET_MIN_DATA_RAW[code].keys())
                        latest_prices = [GLOBAL_MARKET_MIN_DATA_RAW[code][t]['close'] for t in sorted_times][-240:]
                        GLOBAL_MARKET_MIN_DATA[code] = latest_prices
                except Exception as e:
                    pass

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                list(executor.map(fetch_code, codes))

            # 为了启动时能立刻展示（如果是第一次遍历完），立即打印出拉取到的数量
            if GLOBAL_MARKET_MIN_DATA:
                print(f"当前缓存了 {len(GLOBAL_MARKET_MIN_DATA)} 只股票的走势数据")
                
        except Exception as e:
            print(f"执行行情拉取任务出错：{e}")
        
        # 休息 60 秒
        time.sleep(60)

# 清理7天前的交易数据
def clean_old_trades():
    while True:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            seven_days_ago = datetime.now() - timedelta(days=7)
            # 转换为Unix时间戳（秒）
            seven_days_ago_ts = int(seven_days_ago.timestamp())
            cursor.execute('DELETE FROM trades WHERE traded_time < ?', (seven_days_ago_ts,))
            conn.commit()
            conn.close()
            print(f"清理了7天前的交易数据，时间：{datetime.now()}")
        except Exception as e:
            print(f"清理交易数据出错：{e}")
        # 每天执行一次
        time.sleep(86400)

# 每日下午 3:30 执行 VACUUM 操作
def daily_vacuum_task():
    while True:
        try:
            now = datetime.now()
            # 设定目标时间为今天的 15:30
            target_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
            
            # 如果现在的时刻已经过了今天的 15:30，则设定为明天的 15:30
            if now > target_time:
                target_time += timedelta(days=1)
            
            # 计算需要等待的秒数
            wait_seconds = (target_time - now).total_seconds()
            print(f"VACUUM 任务：下次执行时间为 {target_time}，需等待 {wait_seconds:.0f} 秒")
            
            # 等待到目标时间
            time.sleep(wait_seconds)
            
            # 执行 VACUUM
            conn = get_db_connection()
            print(f"开始执行数据库 VACUUM 操作，时间：{datetime.now()}")
            conn.execute('VACUUM')
            conn.close()
            print(f"数据库 VACUUM 操作完成，时间：{datetime.now()}")
            
            # 执行完后休息一分钟，防止在极短时间内重复触发
            time.sleep(60)
            
        except Exception as e:
            print(f"执行 VACUUM 出错：{e}")
            time.sleep(300)

# 每日下午 14:00 执行数据精简：7天之前的 asset_history 只保留每天最后一条
def daily_asset_cleanup_task():
    while True:
        try:
            now = datetime.now()
            # 设定目标时间为今天的 14:00
            target_time = now.replace(hour=14, minute=0, second=0, microsecond=0)
            
            if now > target_time:
                target_time += timedelta(days=1)
            
            wait_seconds = (target_time - now).total_seconds()
            print(f"资产历史清理任务：下次执行时间为 {target_time}，需等待 {wait_seconds:.0f} 秒")
            
            time.sleep(wait_seconds)
            
            print(f"开始清理 7 天前的冗余资产历史记录，时间：{datetime.now()}")
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # 查找 7 天前的数据，并只保留每天 ID 最大的那一条（即时间最晚的一条）
            # 思路：删除那些 (日期在7天前) 且 (ID 不是该日期下最大 ID) 的记录
            # 使用 localtime 确保时区一致性
            cursor.execute('''
            DELETE FROM asset_history 
            WHERE record_time < datetime('now', 'localtime', '-7 days')
            AND id NOT IN (
                SELECT MAX(id) 
                FROM asset_history 
                WHERE record_time < datetime('now', 'localtime', '-7 days')
                GROUP BY date(record_time), account_id
            )
            ''')
            
            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()
            print(f"资产历史清理完成，共删除 {deleted_count} 条冗余记录")
            
            time.sleep(60)
            
        except Exception as e:
            print(f"执行资产历史清理出错：{e}")
            time.sleep(300)

# 计算并缓存每日盈亏数据（排除今天）
def update_daily_profits_cache(account_id):
    """
    计算每日盈亏：
    今日盈亏 = 今日最终总资产 - 昨末总资产 - 今日资金调整(净流入)
    今日收益率 = 今日盈亏 / (昨末总资产 + 今日资金调整(净流入)) * 100
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. 获取所有有记录的日期（排除今天）
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute('''
            SELECT DISTINCT date(record_time) as date
            FROM asset_history
            WHERE account_id = ? AND date(record_time) < ?
            ORDER BY date ASC
        ''', (account_id, today))
        dates = [row[0] for row in cursor.fetchall()]
        
        if not dates:
            conn.close()
            return
            
        for i in range(len(dates)):
            curr_date = dates[i]
            
            # 获取当前日期的最后一条有效资产记录（跳过 total_asset=0 的异常推送）
            cursor.execute('''
                SELECT total_asset 
                FROM asset_history 
                WHERE account_id = ? AND date(record_time) = ? AND total_asset > 0
                ORDER BY record_time DESC LIMIT 1
            ''', (account_id, curr_date))
            curr_asset_row = cursor.fetchone()
            if not curr_asset_row: continue
            curr_asset = curr_asset_row[0]
            
            # 获取当日资金调整
            cursor.execute('''
                SELECT SUM(amount) 
                FROM capital_adjustments 
                WHERE account_id = ? AND date(adjust_time) = ?
            ''', (account_id, curr_date))
            adjustment = cursor.fetchone()[0] or 0
            
            # 获取前一日的最终有效资产（跳过 total_asset=0 的异常推送）
            if i > 0:
                prev_date = dates[i-1]
                cursor.execute('''
                    SELECT total_asset 
                    FROM asset_history 
                    WHERE account_id = ? AND date(record_time) = ? AND total_asset > 0
                    ORDER BY record_time DESC LIMIT 1
                ''', (account_id, prev_date))
                prev_asset_row = cursor.fetchone()
                prev_asset = prev_asset_row[0] if prev_asset_row else None
            else:
                # 如果是第一天，尝试找更早的记录
                cursor.execute('''
                    SELECT total_asset 
                    FROM asset_history 
                    WHERE account_id = ? AND date(record_time) < ? AND total_asset > 0
                    ORDER BY record_time DESC LIMIT 1
                ''', (account_id, curr_date))
                prev_asset_row = cursor.fetchone()
                prev_asset = prev_asset_row[0] if prev_asset_row else None
            
            # 如果没有找到前一日资产（新账户的第一天），则盈亏和收益率为0
            if prev_asset is None:
                daily_profit = 0
                profit_rate = 0
            else:
                # 计算盈亏和收益率
                # 盈亏 = 现资产 - 原资产 - 资金流入
                daily_profit = curr_asset - prev_asset - adjustment
                
                # 收益率 = 盈亏 / (原资产 + 资金调整)
                denominator = prev_asset + max(0, adjustment)
                profit_rate = (daily_profit / denominator * 100) if denominator > 0 else 0
            
            # 存入缓存表
            cursor.execute('''
                INSERT OR REPLACE INTO daily_profits 
                (account_id, date, daily_profit, profit_rate, total_asset, capital_adjustment)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (account_id, curr_date, daily_profit, profit_rate, curr_asset, adjustment))
            
        conn.commit()
        conn.close()
        print(f"账户 {account_id} 的每日盈亏缓存已更新")
    except Exception as e:
        print(f"更新每日盈亏缓存出错: {e}")

# 定时清理过期会话
def daily_session_cleanup_task():
    while True:
        try:
            print(f"开始清理过期会话记录，时间：{datetime.now()}")
            clean_expired_sessions()
            print("过期会话清理完成")
            # 每天执行一次
            time.sleep(24 * 3600)
        except Exception as e:
            print(f"执行过期会话清理出错：{e}")
            time.sleep(3600)

# 定时更新每日盈亏缓存（每天凌晨执行）
def daily_profits_update_task():
    while True:
        try:
            # 计算距离明天凌晨 00:05 的秒数
            now = datetime.now()
            target_time = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            wait_seconds = (target_time - now).total_seconds()
            
            print(f"每日盈亏缓存任务：下次执行时间为 {target_time}，需等待 {wait_seconds:.0f} 秒")
            time.sleep(wait_seconds)
            
            print(f"开始更新所有用户的每日盈亏缓存，时间：{datetime.now()}")
            users = get_users()
            for account_id in users:
                update_daily_profits_cache(account_id)
            
            time.sleep(60)
        except Exception as e:
            print(f"执行每日盈亏缓存更新出错：{e}")
            time.sleep(300)

# 每天早上 9:15 重置走势图缓存（此时集合竞价结束，准备迎接连续竞价数据）
def daily_clear_5min_cache_task():
    global price_5min_cache, GLOBAL_MARKET_MIN_DATA_RAW, GLOBAL_MARKET_MIN_DATA, GLOBAL_QMT_PRICE_CACHE, _A_INIT_EMPTY_NOTIFIED
    while True:
        try:
            # 计算距离明天早上 9:15 的秒数
            now = datetime.now()
            target_time = (now + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
            wait_seconds = (target_time - now).total_seconds()

            print(f"5分钟缓存清空任务：下次执行时间为 {target_time}，需等待 {wait_seconds:.0f} 秒")
            time.sleep(wait_seconds)

            # 清空5分钟缓存
            price_5min_cache.clear()
            # 同时清空行情缓存，确保每天重新从akshare拉取历史数据
            GLOBAL_MARKET_MIN_DATA_RAW.clear()
            GLOBAL_MARKET_MIN_DATA.clear()
            GLOBAL_QMT_PRICE_CACHE.clear()
            _A_INIT_EMPTY_NOTIFIED.clear()
            print(f"已清空5分钟价格缓存、行情缓存和QMT价格缓存，时间：{datetime.now()}")

            time.sleep(60)
        except Exception as e:
            print(f"执行5分钟缓存清空任务出错：{e}")
            time.sleep(300)

# 每日15:30执行数据库备份
def daily_backup_task():
    while True:
        try:
            now = datetime.now()
            # 设定目标时间为今天的 15:30
            target_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
            
            # 如果现在的时刻已经过了今天的 15:30，则设定为明天的 15:30
            if now > target_time:
                target_time += timedelta(days=1)
            
            # 计算需要等待的秒数
            wait_seconds = (target_time - now).total_seconds()
            print(f"数据库备份任务：下次执行时间为 {target_time}，需等待 {wait_seconds:.0f} 秒")
            
            # 等待到目标时间
            time.sleep(wait_seconds)
            
            # 执行备份
            backup_database()
            
            # 执行完后休息一分钟，防止在极短时间内重复触发
            time.sleep(60)
            
        except Exception as e:
            print(f"执行数据库备份任务出错：{e}")
            time.sleep(300)


# 涨跌幅数据源：优先东方财富批量实时行情(盘中实时、收盘即为当日；服务器经 KDL 代理访问)，
# 失败回退 tushare pro.daily 最新交易日（rt_k 实时接口已无权限）。


# 东财代理配置收进 plugins.proxy（转债比价表走同一批接口，两边共用）
from plugins.proxy import get_proxies as get_eastmoney_proxies


# 东财熔断：东财常被封(RemoteDisconnected)，连续失败达阈值后冷却期内直接跳过，避免白试+刷屏
_EM_BREAKER = {"fails": 0, "until": 0.0}
_EM_FAIL_THRESHOLD = 3
_EM_COOLDOWN = 600  # 10 分钟


def fetch_intraday_change_em(codes):
    """东方财富批量行情(备源)，返回 {ts_code: 当日涨跌幅%}。现降为备用（新浪为主）。
    带熔断：连续失败 >= 阈值则冷却 10 分钟内直接跳过，不再请求、不刷屏。
    secid = 市场.代码（沪 SH=1，深/北 SZ/BJ=0）；f3=涨跌幅(fltt=2 直接为百分数)，f12=代码。"""
    if time.time() < _EM_BREAKER["until"]:   # 熔断冷却期内，直接跳过东财
        return {}
    result = {}
    wanted = [c for c in dict.fromkeys(codes or []) if c]
    code_to_ts, secids = {}, []
    for ts in wanted:
        try:
            num, ex = ts.split(".")
        except Exception:
            continue
        code_to_ts[num] = ts
        secids.append(("1." if ex.upper() == "SH" else "0.") + num)
    if not secids:
        return result
    proxies = get_eastmoney_proxies()
    batch_failed = False
    for i in range(0, len(secids), 100):
        batch = secids[i:i + 100]
        try:
            r = requests.get(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                params={"fltt": 2, "secids": ",".join(batch), "fields": "f3,f12"},
                headers={"User-Agent": "Mozilla/5.0", "Connection": "close"},
                proxies=proxies, timeout=12,
            )
            r.raise_for_status()
            for it in ((r.json().get("data") or {}).get("diff") or []):
                ts = code_to_ts.get(str(it.get("f12") or ""))
                pct = it.get("f3")
                if ts and pct is not None and pct != "-":
                    try:
                        pctf = float(pct)
                        if -31 <= pctf <= 31:
                            result[ts] = round(pctf, 2)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            batch_failed = True   # 不再每批刷屏，只在触发熔断时打一条
    # 更新熔断状态
    if result:
        _EM_BREAKER["fails"] = 0
    elif batch_failed:
        _EM_BREAKER["fails"] += 1
        if _EM_BREAKER["fails"] >= _EM_FAIL_THRESHOLD:
            _EM_BREAKER["until"] = time.time() + _EM_COOLDOWN
            _EM_BREAKER["fails"] = 0
            print(f"[东财实时] 连续失败，暂停 {_EM_COOLDOWN // 60} 分钟，改用新浪")
    return result


def _ts_to_sina(ts):
    """600519.SH→sh600519, 002527.SZ→sz002527, 920125.BJ→bj920125。"""
    try:
        num, ex = ts.split(".")
    except Exception:
        return None
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(ex.upper(), "") + num if ex.upper() in ("SH", "SZ", "BJ") else None


def fetch_sina_spot(codes):
    """新浪实时行情(备用源)，返回 {ts_code: 当日涨跌幅%}。东财被封时启用，同样走代理。
    字段顺序: 名称,今开,昨收,现价,最高,最低,...；涨跌幅 = (现价-昨收)/昨收*100。需 Referer + gbk。"""
    result = {}
    sina_map = {}
    for ts in [c for c in dict.fromkeys(codes or []) if c]:
        s = _ts_to_sina(ts)
        if s:
            sina_map[s] = ts
    if not sina_map:
        return result
    proxies = get_eastmoney_proxies()
    syms = list(sina_map.keys())
    for i in range(0, len(syms), 400):
        batch = syms[i:i + 400]
        try:
            r = requests.get(
                "https://hq.sinajs.cn/list=" + ",".join(batch),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn", "Connection": "close"},
                proxies=proxies, timeout=12,
            )
            r.encoding = "gbk"
            for line in r.text.split("\n"):
                if "hq_str_" not in line or '="' not in line:
                    continue
                sym = line.split("hq_str_", 1)[1].split("=", 1)[0].strip()
                ts = sina_map.get(sym)
                if not ts:
                    continue
                fields = line.split('="', 1)[1].rstrip('";').split(",")
                if len(fields) < 4:
                    continue
                try:
                    prevclose, price = float(fields[2]), float(fields[3])
                    if prevclose > 0 and price > 0:
                        chg = round((price - prevclose) / prevclose * 100, 2)
                        if -31 <= chg <= 31:
                            result[ts] = chg
                except (TypeError, ValueError, IndexError):
                    pass
        except Exception as e:
            print(f"[新浪实时] 批量获取失败: {e}")
    return result


# tushare pro.daily 最新交易日涨跌幅（东财失败时的回退）：全市场 daily 按交易日缓存，5 分钟探测一次。
_DAILY_PCT_CACHE = {"trade_date": None, "map": {}, "ts": 0.0}


def fetch_daily_change_map(codes):
    """回退源：tushare pro.daily 最新可用交易日涨跌幅。返回 {ts_code: 涨跌幅%}。"""
    result = {}
    wanted = {c for c in (codes or []) if c}
    if not wanted:
        return result
    try:
        now_ts = time.time()
        if not _DAILY_PCT_CACHE["map"] or (now_ts - _DAILY_PCT_CACHE["ts"]) > 300:
            for td in recent_trade_dates(3):
                if td == _DAILY_PCT_CACHE["trade_date"] and _DAILY_PCT_CACHE["map"]:
                    break
                df = pro.daily(trade_date=td, fields='ts_code,pct_chg')
                if df is not None and not df.empty:
                    _DAILY_PCT_CACHE["map"] = {
                        r['ts_code']: round(float(r['pct_chg']), 2)
                        for _, r in df.iterrows() if pd.notna(r.get('pct_chg'))
                    }
                    _DAILY_PCT_CACHE["trade_date"] = td
                    break
            _DAILY_PCT_CACHE["ts"] = now_ts
        m = _DAILY_PCT_CACHE["map"]
        for c in wanted:
            if c in m:
                result[c] = m[c]
    except Exception as e:
        print(f"[实时涨幅] pro.daily 取涨跌幅失败: {e}")
    return result


def fetch_bridge_change_map(codes):
    """从大QMT 取当日涨跌幅。券商直连行情，比任何爬来的源都准也都稳。"""
    wanted = [c for c in (codes or []) if c]
    if not wanted or not bridge_market.available():
        return {}
    result = {}
    for code, tick in (bridge_market.get_ticks(wanted) or {}).items():
        if not isinstance(tick, dict):
            continue
        try:
            last = float(tick.get("lastPrice") or tick.get("last_price") or 0)
            prev = float(tick.get("lastClose") or tick.get("last_close") or 0)
        except (TypeError, ValueError):
            continue
        if last > 0 and prev > 0:
            result[code] = round((last - prev) / prev * 100, 4)
    return result


def fetch_realtime_change_map(codes):
    """返回 {ts_code: 当日涨跌幅%}。

    改造前是三级爬源：新浪(主) → 东财(备，常被封所以带熔断+隧道代理) → pro.daily(回退)。
    直连之后大QMT 的券商行情才是主源 —— 它就是下单用的那份数据，不会和成交价对不上，
    也不会被谁家风控封 IP。爬源整体降为大QMT 不可用时的兜底。
    """
    wanted = {c for c in (codes or []) if c}
    if not wanted:
        return {}
    result = fetch_bridge_change_map(wanted)          # 大QMT 为主
    missing = wanted - set(result)
    if missing:
        result.update(fetch_sina_spot(missing))       # 新浪兜底
        missing = wanted - set(result)
    if missing:
        result.update(fetch_intraday_change_em(missing))   # 东财兜底（熔断期内自动跳过）
        missing = wanted - set(result)
    if missing:
        result.update(fetch_daily_change_map(missing))     # 都不可用 → pro.daily 最新交易日
    return result


# 统一实时涨跌幅缓存：由 realtime_change_update_task 每 1 分钟批量(tushare rt_k)刷新，
# 供「历史买入列表」等 qmt 不更新涨跌幅的场景读取，避免每个面板各自频繁请求 tushare。
# data: {ts_code: 当日涨跌幅%}；ts: 最近一次成功刷新的 epoch 秒。
_RT_CHANGE_CACHE = {"ts": 0.0, "attempt_ts": 0.0, "data": {}}
_RT_CHANGE_CACHE_MAX_AGE_SECONDS = 75
_RT_CHANGE_REQUEST_REFRESH_THROTTLE_SECONDS = 30


def ensure_realtime_change_cache(codes):
    """接口层兜底刷新实时涨跌幅，避免后台线程或缓存未命中时前端一直显示空值。"""
    wanted = {c for c in (codes or []) if c}
    if not wanted:
        return

    now_ts = time.time()
    data = _RT_CHANGE_CACHE.setdefault("data", {})
    cache_ts = float(_RT_CHANGE_CACHE.get("ts") or 0.0)
    missing_codes = {c for c in wanted if c not in data}
    is_stale = cache_ts <= 0 or now_ts - cache_ts > _RT_CHANGE_CACHE_MAX_AGE_SECONDS
    refresh_codes = wanted if is_stale else missing_codes
    if not refresh_codes:
        return

    attempt_ts = float(_RT_CHANGE_CACHE.get("attempt_ts") or 0.0)
    if now_ts - attempt_ts < _RT_CHANGE_REQUEST_REFRESH_THROTTLE_SECONDS:
        return
    _RT_CHANGE_CACHE["attempt_ts"] = now_ts

    try:
        changes = fetch_realtime_change_map(refresh_codes)
        if changes:
            data.update(changes)
            _RT_CHANGE_CACHE["ts"] = now_ts
    except Exception as e:
        print(f"[实时涨幅] 接口兜底刷新失败: {e}")


def get_market_today_codes_for_rt():
    """取当日 stock_market_data 报出股票的 ts_code 列表（供统一实时涨幅刷新）。失败返回空。"""
    try:
        today = datetime.now().strftime("%Y%m%d")
        conn = get_stock_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT stock_code FROM stock_market_data WHERE date_string = %s", (today,))
        rows = cursor.fetchall()
        conn.close()
        return [_code_to_ts_code(r[0]) for r in rows if r and r[0]]
    except Exception as e:
        print(f"[实时涨幅] 取当日报出代码失败: {e}")
        return []


def realtime_change_update_task():
    """交易日 09:15-15:00、且有用户在线使用看板时，每 10 秒统一刷新「记录板 + 历史买入列表」实时涨跌幅。
    数据源：东方财富(主) → 新浪(被封时备用) → pro.daily(回退)，均走 KDL 代理。
    缓存全部所需行情到 _RT_CHANGE_CACHE 供各面板共享读取；盘后 15:00 以后、或无人登录时不更新。
    本地 sqlite 每 10 秒写回；MySQL 同步与「当日报出代码」查询节流到 60 秒，避免过频。"""
    last_mysql_sync = 0.0
    last_market_fetch = 0.0
    market_codes = []
    while True:
        try:
            now = datetime.now()
            if not is_trade_date(now.strftime('%Y-%m-%d')):
                time.sleep(300)
                continue
            cur_hm = now.strftime('%H:%M')
            # 仅 09:15(集合竞价起)-15:00 刷新实时行情；盘后(15:00 以后)不再更新，缓存保留当日收盘值
            if not ('09:15' <= cur_hm <= '15:00'):
                time.sleep(60)
                continue
            # 仅当有用户在线使用看板时才刷新（无人登录则不刷新，不打扰东财、不消耗代理）
            if not has_active_user():
                time.sleep(30)
                continue
            nowt = time.time()
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT stock_code FROM research_board_records "
                           "WHERE stock_code IS NOT NULL AND stock_code != ''")
            board_set = {row[0] for row in cursor.fetchall() if row[0]}
            conn.close()
            # 历史买入列表「当日报出代码」变化慢，节流 60 秒查询一次
            if nowt - last_market_fetch > 60 or not market_codes:
                market_codes = get_market_today_codes_for_rt()
                last_market_fetch = nowt
            all_codes = board_set | set(market_codes)
            if all_codes:
                changes = fetch_realtime_change_map(all_codes)
                if changes:
                    _RT_CHANGE_CACHE["data"].update(changes)   # 共享缓存：所有面板读这里
                    _RT_CHANGE_CACHE["ts"] = nowt
                    board_changes = {c: p for c, p in changes.items() if c in board_set}
                    if board_changes:
                        conn2 = get_db_connection()
                        cursor2 = conn2.cursor()
                        for code, pct in board_changes.items():
                            cursor2.execute("UPDATE research_board_records SET current_change = ? "
                                            "WHERE stock_code = ?", (pct, code))
                        conn2.commit()
                        conn2.close()
                        # MySQL 同步节流 60 秒（实时值已在本地/缓存，MySQL 镜像不必每 10 秒写）
                        if nowt - last_mysql_sync > 60:
                            last_mysql_sync = nowt
                            try:
                                sync_local_research_records()
                            except Exception as e:
                                print(f"[记录板] 实时涨幅同步 MySQL 失败: {e}")
        except Exception as e:
            print(f"[实时涨幅] 统一刷新任务出错: {e}")
        # 盘中每 10 秒刷新一次实时行情
        time.sleep(10)


def pull_stock_basic_cache_task():
    """启动后预热 bak_basic + etf_type_snapshot 内存缓存 + 每小时刷新，避免下单搜索每次打 MySQL。"""
    # 稍等一下让模块完成加载（_STOCK_BASIC_CACHE / load_stock_basic_items 在文件后部定义）
    time.sleep(5)
    while True:
        try:
            _STOCK_BASIC_CACHE["loaded_at"] = None  # 强制下一次走刷新分支
            items = load_stock_basic_items()
            print(f"[股票基础] 内存缓存已刷新，共 {len(items)} 只")
        except Exception as e:
            print(f"[股票基础] 刷新失败: {e}")
        try:
            _ETF_BASIC_CACHE["loaded_at"] = None
            etfs = load_etf_items()
            print(f"[ETF基础] 内存缓存已刷新，共 {len(etfs)} 只")
        except Exception as e:
            print(f"[ETF基础] 刷新失败: {e}")
        time.sleep(3600)


def akshare_auto_update_task():
    """每天早上(07:xx)检查并 pip 升级 akshare（接口常变，保持最新）。新版本在下次重启后生效。"""
    import subprocess
    import sys
    last_done_date = None
    while True:
        try:
            now = datetime.now()
            if now.hour == 7 and last_done_date != now.strftime('%Y-%m-%d'):
                last_done_date = now.strftime('%Y-%m-%d')
                try:
                    old_ver = getattr(ak, '__version__', '?')
                except Exception:
                    old_ver = '?'
                try:
                    out = subprocess.run(
                        [sys.executable, '-m', 'pip', 'install', '--upgrade', 'akshare'],
                        capture_output=True, text=True, timeout=900,
                    )
                    tail = (out.stdout or '') + (out.stderr or '')
                    print(f"[akshare升级] 旧版本={old_ver}，pip 退出码={out.returncode}；输出尾部: {tail[-300:].strip()}")
                    print("[akshare升级] 完成（如有新版本，将在下次重启服务后生效）")
                except Exception as e:
                    print(f"[akshare升级] pip 执行失败: {e}")
        except Exception as e:
            print(f"[akshare升级] 任务出错: {e}")
        time.sleep(1800)  # 每 30 分钟检查一次是否到点


# 启动定时线程
def start_background_tasks():
    # 0. 启动时交易日检查 (用 tushare 判断，非交易日自动停止所有账号交易)
    threading.Thread(target=startup_trading_day_check, daemon=True).start()
    # 1. 交易数据清理线程
    threading.Thread(target=clean_old_trades, daemon=True).start()
    # 2. 每日 VACUUM 线程
    threading.Thread(target=daily_vacuum_task, daemon=True).start()
    # 3. 每日资产历史清理线程
    threading.Thread(target=daily_asset_cleanup_task, daemon=True).start()
    # 4. 每日过期会话清理线程
    threading.Thread(target=daily_session_cleanup_task, daemon=True).start()
    # 5. 每日盈亏缓存更新线程
    threading.Thread(target=daily_profits_update_task, daemon=True).start()
    # 6. 每天早上9:15重置走势图缓存（清理集合竞价数据，准备接收连续竞价数据）
    threading.Thread(target=daily_clear_5min_cache_task, daemon=True).start()
    # 7. 启动时回填数据
    threading.Thread(target=start_backfill, daemon=True).start()
    # 8. 每日15:30数据库备份线程
    threading.Thread(target=daily_backup_task, daemon=True).start()
    # 9. 定期拉取行情数据
    threading.Thread(target=pull_market_rt_min_task, daemon=True).start()
    # 10. 09:15-15:00 且有用户在线时，每10秒刷新记录板/历史买入实时涨跌幅(东财→新浪→pro.daily)
    threading.Thread(target=realtime_change_update_task, daemon=True).start()
    # 11. 启动预热 + 每小时刷新 bak_basic 内存缓存（股票搜索框走该缓存，避免每次打MySQL）
    threading.Thread(target=pull_stock_basic_cache_task, daemon=True).start()
    # 12. 每天早上 pip 升级 akshare（保持接口最新，新版本下次重启生效）
    threading.Thread(target=akshare_auto_update_task, daemon=True).start()
    # 13. 大QMT 直连：账号轮询 + 实时委托/成交回报 + 连接探活
    start_bridge_tasks()
    # 14. 可转债参考数据日更 + 打新债
    threading.Thread(target=cb_daily_task, daemon=True, name="cb-daily").start()


def cb_daily_task():
    """转债参考数据日更（转股价/正股/申购信息），顺带触发打新债。

    转股价会因下修、送转而变，不能只在首次启动时拉一次。
    """
    last_refresh = ""
    while True:
        today = datetime.now().strftime('%Y-%m-%d')
        try:
            if last_refresh != today and cb_reference.is_stale(max_age_hours=20):
                cb_reference.refresh_all()
                last_refresh = today
        except Exception as e:
            print(f"[转债] 参考数据日更失败: {e}")

        try:
            # 09:30 之后打新债；ipo.run_once 内部保证每账号每天只跑一次
            if is_trading_session() and datetime.now().hour >= 9:
                cb_ipo.run_once()
        except Exception as e:
            print(f"[打新债] 执行失败: {e}")

        time.sleep(600)


def start_bridge_tasks():
    """拉起直连相关的后台任务。没有配置账号时安静退出，服务照常起。"""
    if not bridge_config.account_ids():
        print("[bridge] 未配置账号，直连任务未启动（面板仍可查看历史数据）")
        return
    sync_poller.start_all()
    sync_callbacks.start_all()
    threading.Thread(target=bridge_health_task, daemon=True, name="qmt-health").start()


def bridge_health_task():
    """定期 ping 每个账号，结果喂给前端的在线状态。

    轮询本身已经能反映「拉不到数据」，探活额外给出往返延迟，
    用来区分「大QMT 没开」和「大QMT 开着但慢」。
    """
    while True:
        for account_id in bridge_config.account_ids():
            bridge_pool.check_health(account_id)
        time.sleep(30 if is_trading_session() else 120)

# 启动定时清理线程（保留旧函数名以防万一，但内部调用新的统一启动函数）
def start_cleanup_thread():
    start_background_tasks()

# 自动回填历史数据逻辑
def backfill_asset_history(account_id):
    """
    根据交易记录和当前资产，回填过去30天的资产历史记录
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. 获取当前资产作为锚点
        cursor.execute('SELECT total_asset, market_value, cash FROM assets WHERE account_id = ?', (account_id,))
        current_asset_row = cursor.fetchone()
        if not current_asset_row:
            conn.close()
            return
        
        curr_total, curr_market, curr_cash = current_asset_row
        
        # 2. 获取过去30天的所有交易
        start_ts = int((datetime.now() - timedelta(days=30)).timestamp())
        cursor.execute('''
            SELECT date(traded_time, 'unixepoch', 'localtime') as day, 
                   direction, traded_amount, stock_code
            FROM trades 
            WHERE account_id = ? AND traded_time >= ?
            ORDER BY traded_time DESC
        ''', (account_id, start_ts))
        
        trades = cursor.fetchall()
        daily_trades = {}
        for day, direction, amount, code in trades:
            if day not in daily_trades:
                daily_trades[day] = []
            daily_trades[day].append({'dir': direction, 'amt': amount})

        
    except Exception as e:
        print(f"回填历史数据出错: {e}")

# 启动时补全缺失的交易日余额数据
def backfill_missing_trading_days():
    """
    启动时检查：如果今天非周六/周日，对每个账户检查近期是否有缺失的交易日数据（asset_history），用上一个有记录的交易日数据补全，防止每日盈亏计算出现断档。
    """
    try:
        today = datetime.now()
        today_str = today.strftime('%Y-%m-%d')
        # 如果今天不是交易日（周末或假期），跳过
        if not is_trade_date(today_str):
            print(f"今日 {today_str} 非交易日，跳过余额数据补全检查。")
            return

        yesterday = today - timedelta(days=1)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT account_id FROM users WHERE (if_delete != 1 OR if_delete IS NULL)')
        accounts = [row[0] for row in cursor.fetchall()]
        conn.close()

        for account_id in accounts:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()

                # 获取最近30天内有记录的所有日期
                cursor.execute('''
                    SELECT DISTINCT date(record_time) as day
                    FROM asset_history
                    WHERE account_id = ?
                      AND record_time >= datetime('now', 'localtime', '-30 days')
                    ORDER BY day ASC
                ''', (account_id,))
                existing_dates = set(row[0] for row in cursor.fetchall())

                if not existing_dates:
                    conn.close()
                    continue

                # 找到最新有记录的日期，检查其后到昨天之间的工作日是否有缺失
                last_date = max(existing_dates)
                last_dt = datetime.strptime(last_date, '%Y-%m-%d')

                missing_days = []
                check_dt = last_dt + timedelta(days=1)
                while check_dt <= yesterday:
                    check_str = check_dt.strftime('%Y-%m-%d')
                    # 用真实交易日历判断（排除假期，如五一、国庆等）
                    if is_trade_date(check_str) and check_str not in existing_dates:
                        missing_days.append(check_str)
                    check_dt += timedelta(days=1)

                if not missing_days:
                    conn.close()
                    continue

                print(f"账户 {account_id} 检测到缺失的交易日数据：{missing_days}，开始补全...")

                for missing_day in missing_days:
                    # 取该缺失日之前最近一条记录
                    cursor.execute('''
                        SELECT total_asset, market_value, cash
                        FROM asset_history
                        WHERE account_id = ? AND date(record_time) < ?
                        ORDER BY record_time DESC LIMIT 1
                    ''', (account_id, missing_day))
                    prev_row = cursor.fetchone()
                    if not prev_row:
                        continue

                    total_asset, market_value, cash = prev_row
                    # 以收市时间 15:00:00 作为补全时间戳
                    fill_time = f"{missing_day} 15:00:00"

                    # 避免重复插入
                    cursor.execute(
                        'SELECT COUNT(*) FROM asset_history WHERE account_id = ? AND record_time = ?',
                        (account_id, fill_time)
                    )
                    if cursor.fetchone()[0] == 0:
                        cursor.execute('''
                            INSERT INTO asset_history (account_id, total_asset, market_value, cash, record_time)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (account_id, total_asset, market_value, cash, fill_time))
                        print(f"  已为账户 {account_id} 补全 {missing_day} 余额数据（总资产: {total_asset:.2f}）")

                conn.commit()
                conn.close()
            except Exception as e:
                print(f"补全账户 {account_id} 交易日数据时出错: {e}")
                try:
                    conn.close()
                except Exception:
                    pass

        print("交易日余额数据补全检查完成。")
    except Exception as e:
        print(f"执行交易日余额数据补全检查出错: {e}")


def recalculate_recent_daily_profits(account_id, n_trading_days=5):
    """
    重新计算账户最近 n_trading_days 个交易日（周一至周五）的每日盈亏缓存。
    补全空缺交易日数据后，需要重算受影响的近期记录以确保准确。
    """
    try:
        today = datetime.now().date()

        # 向前遍历找出最近 n_trading_days 个真实交易日（不含今天）
        recent_trading_days = []
        check = today - timedelta(days=1)
        while len(recent_trading_days) < n_trading_days:
            check_str = check.strftime('%Y-%m-%d')
            if is_trade_date(check_str):
                recent_trading_days.append(check_str)
            check -= timedelta(days=1)

        # 还需要最早那天的前一个有效资产作为基准
        earliest_day = recent_trading_days[-1]

        conn = get_db_connection()
        cursor = conn.cursor()

        for curr_date in recent_trading_days:
            # 当日最后一条有效资产（跳过 total_asset=0 的异常推送）
            cursor.execute('''
                SELECT total_asset FROM asset_history
                WHERE account_id = ? AND date(record_time) = ? AND total_asset > 0
                ORDER BY record_time DESC LIMIT 1
            ''', (account_id, curr_date))
            row = cursor.fetchone()
            if not row:
                continue
            curr_asset = row[0]

            # 当日资金调整
            cursor.execute('''
                SELECT SUM(amount) FROM capital_adjustments
                WHERE account_id = ? AND date(adjust_time) = ?
            ''', (account_id, curr_date))
            adjustment = cursor.fetchone()[0] or 0

            # 前一日的最终有效资产（跳过 total_asset=0 的异常推送）
            cursor.execute('''
                SELECT total_asset FROM asset_history
                WHERE account_id = ? AND date(record_time) < ? AND total_asset > 0
                ORDER BY record_time DESC LIMIT 1
            ''', (account_id, curr_date))
            prev_row = cursor.fetchone()
            prev_asset = prev_row[0] if prev_row else None

            if prev_asset is None:
                daily_profit = 0
                profit_rate = 0
            else:
                daily_profit = curr_asset - prev_asset - adjustment
                denominator = prev_asset + max(0, adjustment)
                profit_rate = (daily_profit / denominator * 100) if denominator > 0 else 0

            cursor.execute('''
                INSERT OR REPLACE INTO daily_profits
                (account_id, date, daily_profit, profit_rate, total_asset, capital_adjustment)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (account_id, curr_date, daily_profit, profit_rate, curr_asset, adjustment))

        conn.commit()
        conn.close()
        print(f"账户 {account_id} 近 {n_trading_days} 个交易日盈亏缓存已重新计算")
    except Exception as e:
        print(f"重新计算账户 {account_id} 近 {n_trading_days} 个交易日盈亏出错: {e}")


def fix_anomalous_daily_profits():
    """
    启动时检测并修复异常的 daily_profits 记录。
    异常判定：total_asset=0 或 profit_rate=-100 或
             |daily_profit| > prev_total_asset * 0.5（单日盈亏超50%视为异常）。
    修复方式：删除异常记录，让 update_daily_profits_cache 重新计算。
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 找出 total_asset=0 或 profit_rate<=-99 的明显异常记录
        cursor.execute('''
            SELECT id, account_id, date, daily_profit, profit_rate, total_asset
            FROM daily_profits
            WHERE total_asset = 0 OR profit_rate <= -99
        ''')
        bad_rows = cursor.fetchall()

        if not bad_rows:
            print("[启动检查] daily_profits 无异常记录")
            conn.close()
            return

        print(f"[启动检查] 发现 {len(bad_rows)} 条异常 daily_profits 记录，准备清除重算：")
        affected_accounts = set()
        ids_to_delete = []
        for row in bad_rows:
            rid, acct, dt, profit, rate, asset = row
            print(f"  account={acct} date={dt} profit={profit:.2f} rate={rate:.2f}% total={asset:.2f}")
            ids_to_delete.append(rid)
            affected_accounts.add(acct)

        # 删除异常记录
        cursor.executemany('DELETE FROM daily_profits WHERE id=?', [(i,) for i in ids_to_delete])
        conn.commit()
        conn.close()
        print(f"[启动检查] 已删除 {len(ids_to_delete)} 条异常记录")

        # 对受影响账户重新计算全量盈亏缓存
        for acct in affected_accounts:
            update_daily_profits_cache(acct)
            print(f"[启动检查] 账户 {acct} 盈亏缓存已重算")

    except Exception as e:
        print(f"[启动检查] 修复异常 daily_profits 出错: {e}")


# 在启动时为所有用户执行回填
def start_backfill():
    """启动回填：先补全交易日数据，然后尝试akshare直接填充当日走势图"""
    # 先补全缺失的交易日余额数据，防止盈亏计算断档
    backfill_missing_trading_days()
    users = get_users()
    for account_id in users:
        update_daily_profits_cache(account_id)
        recalculate_recent_daily_profits(account_id, n_trading_days=5)
    fix_anomalous_daily_profits()

    # ---- 走势图分钟数据启动回填 ----
    try:
        now = datetime.now()
        # 只在交易日交易时段才需要回填
        if now.weekday() >= 5 or now.hour < 9 or (now.hour == 9 and now.minute < 30) or now.hour >= 15:
            print("[启动回填] 非交易时段，跳过走势图分钟数据回填")
        else:
            print("[启动回填] 开始向QMT下发回补指令...")

            # 获取所有有持仓的股票代码
            # 当前持仓（最新快照，非历史累积的1000多只）
            all_codes = get_current_holding_codes()

            import concurrent.futures

            # 过滤出当前内存中确实缺少数据的股票
            missing_codes = []
            for code in all_codes:
                raw = GLOBAL_MARKET_MIN_DATA_RAW.get(code)
                if not raw:
                    missing_codes.append(code)
                    continue
                trading_times = [t for t in raw.keys() if t >= '09:30' and not ('11:31' <= t <= '12:59')]
                if len(trading_times) < 3:
                    missing_codes.append(code)

            if missing_codes:
                print(f"[启动回填] 发现 {len(missing_codes)} 只标的缺失，直接向大QMT 拉分钟线...")
                # 改造前是下发指令给客户端、再 sleep(180) 等它推回来。
                # 现在同步拿到结果，不用等。
                backfill_minute_bars(missing_codes)

                # 检查哪些股票仍然缺失，进行akshare兜底
                still_missing = []
                for code in missing_codes:
                    raw = GLOBAL_MARKET_MIN_DATA_RAW.get(code)
                    if not raw:
                        still_missing.append(code)
                        continue
                    trading_times = [t for t in raw.keys() if t >= '09:30' and not ('11:31' <= t <= '12:59')]
                    if len(trading_times) < 3:
                        still_missing.append(code)

                if still_missing:
                    print(f"[启动回填] 3分钟后仍有 {len(still_missing)} 只股票未收到QMT回补数据，等待实时推送补充: {still_missing}")
                else:
                    print("[启动回填] 3分钟后验收：QMT已成功回传所有缺失数据")
            else:
                print("[启动回填] 内存中已有充足数据，无需回补")
    except Exception as e:
        print(f"[启动回填] 走势图回填出错: {e}")


def startup_trading_day_check():
    """
    启动时用 tushare 判断今天是否为交易日:
    - 非交易日 → 自动给所有账号发送停止交易信号
    - 交易日 → 自动清除停止交易信号
    """
    try:
        # 等待几秒确保 tushare pro 初始化完成
        time.sleep(3)
        today_str = datetime.now().strftime('%Y-%m-%d')
        is_trade = is_trade_date(today_str)

        conn = get_db_connection()
        cursor = conn.cursor()
        # 确保 trading_status 表存在
        cursor.execute('''CREATE TABLE IF NOT EXISTS trading_status (
            account_id TEXT PRIMARY KEY,
            is_stopped INTEGER DEFAULT 0,
            buy_stopped INTEGER DEFAULT 0,
            sell_stopped INTEGER DEFAULT 0,
            stopped_at TIMESTAMP,
            resumed_at TIMESTAMP,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES users (account_id)
        )''')
        # 兼容旧表：如果列不存在则添加
        try:
            cursor.execute('ALTER TABLE trading_status ADD COLUMN buy_stopped INTEGER DEFAULT 0')
        except Exception:
            pass
        try:
            cursor.execute('ALTER TABLE trading_status ADD COLUMN sell_stopped INTEGER DEFAULT 0')
        except Exception:
            pass
        # 迁移旧数据: is_stopped=1 的行补齐 buy_stopped/sell_stopped
        try:
            cursor.execute('UPDATE trading_status SET buy_stopped=1, sell_stopped=1 WHERE is_stopped=1 AND (buy_stopped IS NULL OR buy_stopped=0 OR sell_stopped IS NULL OR sell_stopped=0)')
        except Exception:
            pass
        cursor.execute('SELECT account_id FROM users WHERE if_delete != 1 OR if_delete IS NULL')
        accounts = [row[0] for row in cursor.fetchall()]
        conn.commit()

        if not is_trade:
            # 非交易日: 停止所有账号交易（买入+卖出）
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for aid in accounts:
                cursor.execute('''
                    INSERT INTO trading_status (account_id, is_stopped, buy_stopped, sell_stopped, stopped_at, update_time)
                    VALUES (?, 1, 1, 1, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                    is_stopped=1, buy_stopped=1, sell_stopped=1, stopped_at=excluded.stopped_at, update_time=excluded.update_time
                ''', (aid, now_str, now_str))
            conn.commit()
            print(f"[启动检查] 今日 {today_str} 非交易日，已自动停止 {len(accounts)} 个账号的交易")
        else:
            # 交易日: 恢复所有账号交易
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for aid in accounts:
                cursor.execute('''
                    UPDATE trading_status SET is_stopped=0, buy_stopped=0, sell_stopped=0, resumed_at=?, update_time=?
                    WHERE account_id=? AND (is_stopped=1 OR buy_stopped=1 OR sell_stopped=1)
                ''', (now_str, now_str, aid))
            conn.commit()
            print(f"[启动检查] 今日 {today_str} 是交易日，已自动恢复所有账号交易")

        conn.close()
    except Exception as e:
        print(f"[启动检查] 交易日判断失败: {e}")


# 静态文件服务
app.mount("/static", StaticFiles(directory="."), name="static")

# 主页面路由
@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index_vue.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), status_code=200)


# Vue JS 文件路由
@app.get("/vue-app.js")
async def read_vue_app():
    with open("vue-app.js", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), status_code=200, media_type="application/javascript")

# 用户注册路由
@app.post("/api/register")
async def register(user: UserCreate):
    # 检查用户名是否已存在
    existing_user = get_user(user.username)
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    
    # 检查account_id是否已存在
    existing_account = get_user_by_account_id(user.account_id)
    if existing_account:
        raise HTTPException(status_code=400, detail="Account ID already registered")
    
    # 保存用户
    save_user(user.account_id, user.username, user.password, user.account_name, "user")
    return {"status": "success", "message": "User registered successfully"}

# ============ 观察者(viewer) 接口：独立注册/登录，只能看历史买入列表 ============
@app.post("/api/viewer/register")
def viewer_register(payload: ViewerCreate):   # 同步 def：跑在线程池，不占/不被事件循环拥塞拖累
    username = (payload.username or "").strip()
    if not username or not payload.password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")
    if get_viewer(username):
        raise HTTPException(status_code=400, detail="该用户名已被注册")
    save_viewer(username, payload.password)
    return {"status": "success", "message": "注册成功，请登录"}

@app.post("/api/viewer/token")
def viewer_login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):   # 同步 def：线程池执行，避免登录卡顿
    # 与账户登录同款限流：同一 IP 连错5次锁5分钟（key 用 viewer: 前缀，与交易账户登录互不干扰）
    client_ip = request.client.host
    key = f"viewer:{client_ip}"
    now = datetime.now()
    attempts_info = get_login_attempts(key)
    if attempts_info and attempts_info["blocked_until"]:
        if now < attempts_info["blocked_until"]:
            wait_time = int((attempts_info["blocked_until"] - now).total_seconds())
            raise HTTPException(status_code=403, detail=f"登录失败次数过多，请在 {wait_time} 秒后重试。")
        else:
            reset_login_attempts(key)
            attempts_info = None

    viewer = get_viewer(form_data.username)
    if not viewer or not verify_password(form_data.password, viewer["password"]):
        if not attempts_info:
            update_login_attempts(key, 1, now)
        else:
            first_failure = attempts_info["first_failure_time"]
            attempts = attempts_info["attempts"]
            if now - first_failure > timedelta(minutes=1):
                update_login_attempts(key, 1, now)
            else:
                attempts += 1
                blocked_until = now + timedelta(minutes=5) if attempts >= 5 else None
                update_login_attempts(key, attempts, first_failure, blocked_until)
        raise HTTPException(status_code=401, detail="用户名或密码错误",
                            headers={"WWW-Authenticate": "Bearer"})

    reset_login_attempts(key)
    record_viewer_login(viewer["username"], client_ip)
    access_token = create_access_token(
        data={"sub": viewer["username"], "type": "viewer"},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {"access_token": access_token, "token_type": "bearer", "is_viewer": True, "username": viewer["username"]}

@app.get("/api/viewer/me")
def viewer_me(viewer: dict = Depends(get_current_viewer)):
    return {"username": viewer["username"], "is_viewer": True}

@app.post("/api/viewer/heartbeat")
def viewer_heartbeat(request: Request, viewer: dict = Depends(get_current_viewer)):
    """前端每 30s 上报一次；连续心跳间隔累计为在线时长（总时长 + 按 IP），并做并发检测。"""
    username = viewer["username"]
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    # 总在线时长（按用户，wall-clock）
    last = _VIEWER_LAST_BEAT.get(username)
    _VIEWER_LAST_BEAT[username] = now
    if last is not None:
        delta = int(now - last)
        if 0 < delta <= 90:   # 超过90s视为断开/新会话，不计入
            add_viewer_online_seconds(username, delta)
    # 按 IP 在线时长 + 最近活跃
    kip = (username, ip)
    lastip = _VIEWER_IP_LAST_BEAT.get(kip)
    _VIEWER_IP_LAST_BEAT[kip] = now
    dip = int(now - lastip) if lastip is not None else 0
    add_viewer_ip_online(username, ip, dip if (0 < dip <= 90) else 0)
    # 并发检测（同账号多 IP 同时活跃）
    detect_viewer_concurrency(username, ip)
    return {"status": "ok"}

@app.get("/api/viewer/stats")
async def viewer_stats(current_user: dict = Depends(get_current_admin)):
    """管理员查看观察者近30天登录次数 + 每日在线时长。"""
    since_dt = datetime.now() - timedelta(days=30)
    since_ts = since_dt.strftime('%Y-%m-%d 00:00:00')
    since_date = since_dt.strftime('%Y-%m-%d')
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT v.username, v.created_at,
               (SELECT COUNT(*) FROM viewer_logins l WHERE l.username=v.username AND l.login_time>=?) AS login_30d,
               (SELECT MAX(login_time) FROM viewer_logins l WHERE l.username=v.username) AS last_login,
               (SELECT COALESCE(SUM(online_seconds),0) FROM viewer_daily_online o WHERE o.username=v.username AND o.date>=?) AS online_30d
        FROM viewer_users v
        ORDER BY last_login DESC
    ''', (since_ts, since_date))
    rows = cur.fetchall()
    viewers = []
    for uname, created, login30, last_login, online30 in rows:
        cur.execute('SELECT date, online_seconds FROM viewer_daily_online WHERE username=? AND date>=? ORDER BY date DESC',
                    (uname, since_date))
        daily = [{"date": d, "seconds": s} for d, s in cur.fetchall()]
        viewers.append({
            "username": uname, "created_at": created,
            "login_count_30d": login30 or 0, "last_login": last_login,
            "online_seconds_30d": online30 or 0, "daily_online": daily,
        })
    conn.close()
    return {"viewers": viewers}

@app.get("/api/admin/user-stats")
async def admin_user_stats(current_user: dict = Depends(get_current_admin)):
    """管理员：交易账户 + 观察用户的登录统计（近30天登录次数、最近登录；观察者含累计在线时长）。
    交易账户的在线/最近同步状态由前端用 users 列表合并，这里只出登录流水统计。"""
    since_dt = datetime.now() - timedelta(days=30)
    since_ts = since_dt.strftime('%Y-%m-%d 00:00:00')
    since_date = since_dt.strftime('%Y-%m-%d')
    conn = get_db_connection()
    cur = conn.cursor()
    # 交易账户
    cur.execute('SELECT account_id, username, alias, account_name, role, COALESCE(is_dormant,0) FROM users WHERE (if_delete != 1 OR if_delete IS NULL)')
    urows = cur.fetchall()
    account_users = []
    for account_id, username, alias, account_name, role, dormant in urows:
        cur.execute('SELECT COUNT(*), MAX(login_time) FROM user_logins WHERE account_id=? AND login_time>=?', (account_id, since_ts))
        cnt30, _ = cur.fetchone()
        cur.execute('SELECT MAX(login_time) FROM user_logins WHERE account_id=?', (account_id,))
        last_login = cur.fetchone()[0]
        account_users.append({
            "account_id": account_id, "username": username,
            "alias": alias or account_name or account_id, "role": role,
            "is_dormant": dormant, "login_count_30d": cnt30 or 0, "last_login": last_login,
        })
    # 观察用户（含每 IP 明细：登录次数/在线时长/最近活跃 + 并发检测）
    cur.execute('SELECT username, created_at FROM viewer_users')
    vrows = cur.fetchall()
    viewers = []
    for u, c in vrows:
        cur.execute('SELECT COUNT(*), MAX(login_time) FROM viewer_logins WHERE username=? AND login_time>=?', (u, since_ts))
        lc, ll = cur.fetchone()
        cur.execute('SELECT COALESCE(SUM(online_seconds),0) FROM viewer_daily_online WHERE username=? AND date>=?', (u, since_date))
        os_total = cur.fetchone()[0]
        cur.execute('SELECT ip, COUNT(*), MAX(login_time) FROM viewer_logins WHERE username=? AND login_time>=? GROUP BY ip', (u, since_ts))
        login_by_ip = {(ip or '未知'): (cnt, lt) for ip, cnt, lt in cur.fetchall()}
        cur.execute('SELECT ip, COALESCE(SUM(online_seconds),0) FROM viewer_ip_online WHERE username=? AND date>=? GROUP BY ip', (u, since_date))
        online_by_ip = {(ip or '未知'): s for ip, s in cur.fetchall()}
        cur.execute('SELECT ip, last_seen FROM viewer_ip_last_seen WHERE username=?', (u,))
        seen_by_ip = {(ip or '未知'): ls for ip, ls in cur.fetchall()}
        ips = []
        for ip in (set(login_by_ip) | set(online_by_ip) | set(seen_by_ip)):
            cnt, lt = login_by_ip.get(ip, (0, None))
            ips.append({"ip": ip, "login_count": cnt, "last_login": lt,
                        "online_seconds": online_by_ip.get(ip, 0) or 0, "last_seen": seen_by_ip.get(ip)})
        ips.sort(key=lambda x: (x["last_seen"] or x["last_login"] or ""), reverse=True)
        cur.execute('SELECT ip FROM viewer_logins WHERE username=? ORDER BY login_time DESC LIMIT 1', (u,))
        r = cur.fetchone()
        last_login_ip = (r[0] if r and r[0] else None)
        cur.execute('SELECT COUNT(*), MAX(event_time) FROM viewer_concurrency_log WHERE username=? AND event_time>=?', (u, since_ts))
        conc_cnt, conc_last = cur.fetchone()
        viewers.append({
            "username": u, "created_at": c,
            "login_count_30d": lc or 0, "last_login": ll, "online_seconds_30d": os_total or 0,
            "last_login_ip": last_login_ip, "distinct_ip_30d": len(ips),
            "concurrency_count_30d": conc_cnt or 0, "concurrency_last": conc_last,
            "ips": ips,
        })
    conn.close()
    account_users.sort(key=lambda x: (x["last_login"] or ""), reverse=True)
    viewers.sort(key=lambda x: (x["last_login"] or ""), reverse=True)
    return {"account_users": account_users, "viewers": viewers}

# 获取令牌路由
@app.post("/api/token")
def login_for_access_token(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):   # 同步 def：线程池执行，避免登录卡顿
    client_ip = request.client.host
    now = datetime.now()
    
    # 检查是否被锁定
    attempts_info = get_login_attempts(client_ip)
    if attempts_info and attempts_info["blocked_until"]:
        if now < attempts_info["blocked_until"]:
            wait_time = int((attempts_info["blocked_until"] - now).total_seconds())
            raise HTTPException(
                status_code=403,
                detail=f"登录失败次数过多，请在 {wait_time} 秒后重试。"
            )
        else:
            # 锁定时间已过，清除记录
            reset_login_attempts(client_ip)
            attempts_info = None

    user = get_user(form_data.username)
    if not user or not verify_password(form_data.password, user["password"]):
        # 登录失败，记录尝试
        if not attempts_info:
            update_login_attempts(client_ip, 1, now)
        else:
            first_failure = attempts_info["first_failure_time"]
            attempts = attempts_info["attempts"]
            
            # 如果距离第一次失败已经超过1分钟，重置计数
            if now - first_failure > timedelta(minutes=1):
                update_login_attempts(client_ip, 1, now)
            else:
                attempts += 1
                blocked_until = None
                if attempts >= 5:
                    blocked_until = now + timedelta(minutes=5)
                update_login_attempts(client_ip, attempts, first_failure, blocked_until)
        
        raise HTTPException(
            status_code=401,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 登录成功，重置尝试记录
    reset_login_attempts(client_ip)
    record_user_login(user.get("account_id"), user.get("username"))

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]}, expires_delta=access_token_expires
    )
    
    # 将会话保存到数据库
    save_user_session(user["username"], access_token, access_token_expires)
    
    return {"access_token": access_token, "token_type": "bearer"}

# 退出登录接口
@app.post("/api/logout")
async def logout(token: str = Depends(oauth2_scheme)):
    delete_user_session(token)
    return {"status": "success", "message": "已成功退出登录"}

# 获取当前用户信息
@app.get("/api/users/me")
async def read_users_me(current_user: dict = Depends(get_current_user)):
    # 移除密码等敏感信息
    current_user.pop("password", None)
    return current_user

# 账号指令模型
class CommandCreate(BaseModel):
    account_id: str
    command_type: str
    command_data: str = ""
    password: str = ""

# 设置清仓密码模型
class SetClearPassword(BaseModel):
    account_id: str
    password: str

# 修改登录密码模型
class ChangePassword(BaseModel):
    old_password: str
    new_password: str

# 用户管理模型 (管理员使用)
class UserManagement(BaseModel):
    username: str
    password: str
    account_id: str
    account_name: str = ""
    role: str = "user"


class TradeFactorsUpdate(BaseModel):
    account_id: str
    position_factor: float
    open_count_factor: float

# 修改登录密码
@app.post("/api/account/change-password")
async def change_login_password(data: ChangePassword, current_user: dict = Depends(get_current_user)):
    if not verify_password(data.old_password, current_user["password"]):
        raise HTTPException(status_code=400, detail="旧密码不正确")
    
    hashed_password = get_password_hash(data.new_password)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET password = ? WHERE username = ?', (hashed_password, current_user["username"]))
    conn.commit()
    conn.close()
    
    return {"status": "success", "message": "登录密码已更新"}

# 创建新用户 (仅限管理员)
@app.post("/api/admin/create-user")
async def create_user(data: UserManagement, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可创建用户")
    
    # 检查用户名是否已存在（未删除的）
    if get_user(data.username):
        raise HTTPException(status_code=400, detail="用户名已存在")
    
    # 检查 account_id 是否已存在（未删除的）
    if get_user_by_account_id(data.account_id):
        raise HTTPException(status_code=400, detail="该账号ID已被绑定")
    
    # 检查是否已存在被删除的用户（if_delete=1）
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users WHERE username = ? AND if_delete = 1', (data.username,))
    deleted_user = cursor.fetchone()
    
    if deleted_user:
        # 恢复被删除的用户
        hashed_password = get_password_hash(data.password)
        cursor.execute('''
            UPDATE users 
            SET account_id = ?, password = ?, account_name = ?, role = ?, if_delete = 0
            WHERE username = ?
        ''', (data.account_id, hashed_password, data.account_name, data.role, data.username))
        conn.commit()
        conn.close()
        return {"status": "success", "message": f"用户 {data.username} 已恢复"}
    
    # 检查 account_id 是否存在于被删除的用户中
    cursor.execute('SELECT id FROM users WHERE account_id = ? AND if_delete = 1', (data.account_id,))
    deleted_account = cursor.fetchone()
    
    if deleted_account:
        # 恢复被删除的账号
        hashed_password = get_password_hash(data.password)
        cursor.execute('''
            UPDATE users 
            SET username = ?, password = ?, account_name = ?, role = ?, if_delete = 0
            WHERE account_id = ?
        ''', (data.username, hashed_password, data.account_name, data.role, data.account_id))
        conn.commit()
        conn.close()
        return {"status": "success", "message": f"账号 {data.account_id} 已恢复"}
    
    conn.close()
    save_user(data.account_id, data.username, data.password, data.account_name, data.role)
    return {"status": "success", "message": f"用户 {data.username} 创建成功"}

# 删除用户（软删除，设置 if_delete=1）
@app.post("/api/admin/delete-user")
async def delete_user(data: dict, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可删除用户")
    
    username = data.get("username")
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    
    # 不能删除自己
    if username == current_user["username"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录用户")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 检查用户是否存在且未删除
    cursor.execute('SELECT id FROM users WHERE username = ? AND (if_delete != 1 OR if_delete IS NULL)', (username,))
    user = cursor.fetchone()
    
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="用户不存在或已被删除")
    
    # 软删除：设置 if_delete = 1
    cursor.execute('UPDATE users SET if_delete = 1 WHERE username = ?', (username,))
    conn.commit()
    conn.close()
    
    return {"status": "success", "message": f"用户 {username} 已删除"}

# 切换账户休眠状态（仅管理员）

ACCOUNT_DELETE_TABLES = (
    "positions",
    "trades",
    "assets",
    "asset_history",
    "daily_profits",
    "capital_adjustments",
    "position_locks",
    "t0_status",
    "trading_status",
    "orders",
    "order_audit",
    "strategy_configs",
)


def clear_account_runtime_state(account_id: str):
    for cache in (
        account_last_sync,
        account_data_time,
        _BACKFILL_KLINE_ISSUED,
        asset_cache,
    ):
        cache.pop(account_id, None)
    try:
        _LAST_STRATEGY_INI_CONTENT.pop(account_id, None)
    except NameError:
        pass


@app.post("/api/admin/delete-account")
async def delete_account(data: dict, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可删除账号")

    account_id = (data.get("account_id") or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id 不能为空")
    if account_id == "all":
        raise HTTPException(status_code=400, detail="不能删除汇总账号")
    if account_id == current_user.get("account_id"):
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT username FROM users WHERE account_id = ?", (account_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="账号不存在")

        username = row[0]
        if username and username == current_user.get("username"):
            raise HTTPException(status_code=400, detail="不能删除当前登录用户")

        deleted_counts = {}
        if username:
            cursor.execute("DELETE FROM user_sessions WHERE username = ?", (username,))
            deleted_counts["user_sessions"] = cursor.rowcount

        for table in ACCOUNT_DELETE_TABLES:
            cursor.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
            deleted_counts[table] = cursor.rowcount

        cursor.execute("DELETE FROM users WHERE account_id = ?", (account_id,))
        deleted_counts["users"] = cursor.rowcount
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"删除账号失败: {exc}")
    finally:
        conn.close()

    clear_account_runtime_state(account_id)
    return {
        "status": "success",
        "message": f"账号 {account_id} 及其相关数据已删除",
        "deleted_counts": deleted_counts,
    }
@app.post("/api/admin/toggle-dormant")
async def toggle_dormant(data: dict, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")

    account_id = data.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id 不能为空")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT is_dormant FROM users WHERE account_id = ? AND (if_delete != 1 OR if_delete IS NULL)', (account_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="用户不存在")

    new_dormant = 0 if row[0] else 1
    cursor.execute('UPDATE users SET is_dormant = ? WHERE account_id = ?', (new_dormant, account_id))
    conn.commit()
    conn.close()

    state = "休眠" if new_dormant else "激活"
    return {"status": "success", "is_dormant": bool(new_dormant), "message": f"账户 {account_id} 已{state}"}

# 获取所有用户列表 (仅限管理员，带基本信息)
@app.get("/api/admin/users")
async def admin_get_users(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="没有权限")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT username, account_id, account_name, role, alias, created_at, COALESCE(is_dormant, 0) FROM users WHERE if_delete != 1 OR if_delete IS NULL')
    rows = cursor.fetchall()
    conn.close()
    
    users = []
    for row in rows:
        users.append({
            "username": row[0],
            "account_id": row[1],
            "account_name": row[2],
            "role": row[3],
            "alias": row[4],
            "created_at": row[5],
            "is_dormant": bool(row[6])
        })
    
    return {"status": "success", "users": users}

# 设置/更新清仓密码
@app.post("/api/account/clear-password")
async def set_clear_password(data: SetClearPassword, current_user: dict = Depends(get_current_user)):
    # 只有管理员或账号本人可以设置密码
    if current_user["role"] != "admin" and current_user["account_id"] != data.account_id:
        raise HTTPException(status_code=403, detail="没有权限设置此账号的清仓密码")

    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('UPDATE users SET clear_password = ? WHERE account_id = ?', (data.password, data.account_id))
    conn.commit()
    conn.close()
    
    return {"status": "success", "message": "清仓密码已设置"}

# 更新账号别名
@app.post("/api/account/alias")
async def update_account_alias(update: AliasUpdate, current_user: dict = Depends(get_current_user)):
    # 检查权限：普通用户只能更新自己的账号，管理员可以更新所有账号
    if current_user["role"] != "admin" and current_user["account_id"] != update.account_id:
        raise HTTPException(status_code=403, detail="没有权限修改此账号别名")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET alias = ? WHERE account_id = ?', (update.alias, update.account_id))
    conn.commit()
    conn.close()
    
    return {"status": "success", "message": "账号别名已更新"}


@app.get("/api/account/trade-factors")
async def get_trade_factors(account_id: str, current_user: dict = Depends(get_current_user)):
    if account_id == 'all':
        if current_user["role"] != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可查看所有账号参数")
    elif current_user["role"] != "admin" and current_user["account_id"] != account_id:
        raise HTTPException(status_code=403, detail="没有权限查看此账号参数")

    conn = get_db_connection()
    cursor = conn.cursor()

    if account_id == 'all':
        account_ids = _list_active_account_ids(cursor)
        if not account_ids:
            conn.close()
            return {
                "status": "success",
                "account_id": "all",
                "position_factor": 1.0,
                "open_count_factor": 1.0,
                "is_mixed": False,
                "mixed_fields": [],
                "account_count": 0,
            }

        cursor.execute('''
            SELECT COALESCE(position_factor, 1.0), COALESCE(open_count_factor, 1.0)
            FROM users
            WHERE (if_delete != 1 OR if_delete IS NULL)
              AND account_id IS NOT NULL
              AND TRIM(account_id) != ''
        ''')
        rows = cursor.fetchall()
        conn.close()

        position_values = [_safe_db_trade_factor(row[0]) for row in rows]
        open_count_values = [_safe_db_trade_factor(row[1]) for row in rows]
        position_unique = sorted(set(position_values))
        open_count_unique = sorted(set(open_count_values))
        mixed_fields = []
        if len(position_unique) > 1:
            mixed_fields.append('position_factor')
        if len(open_count_unique) > 1:
            mixed_fields.append('open_count_factor')

        return {
            "status": "success",
            "account_id": "all",
            "position_factor": None if 'position_factor' in mixed_fields else position_unique[0],
            "open_count_factor": None if 'open_count_factor' in mixed_fields else open_count_unique[0],
            "is_mixed": bool(mixed_fields),
            "mixed_fields": mixed_fields,
            "account_count": len(rows),
        }

    cursor.execute('''
        SELECT COALESCE(position_factor, 1.0), COALESCE(open_count_factor, 1.0)
        FROM users
        WHERE account_id = ? AND (if_delete != 1 OR if_delete IS NULL)
    ''', (account_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")

    return {
        "status": "success",
        "account_id": account_id,
        "position_factor": _safe_db_trade_factor(row[0]),
        "open_count_factor": _safe_db_trade_factor(row[1]),
        "is_mixed": False,
        "mixed_fields": [],
        "account_count": 1,
    }


@app.post("/api/account/trade-factors")
async def update_trade_factors(data: TradeFactorsUpdate, current_user: dict = Depends(get_current_user)):
    position_factor = _normalize_trade_factor(data.position_factor, '仓位参数')
    open_count_factor = _normalize_trade_factor(data.open_count_factor, '开仓数量参数')

    conn = get_db_connection()
    cursor = conn.cursor()

    if data.account_id == 'all':
        if current_user["role"] != "admin":
            conn.close()
            raise HTTPException(status_code=403, detail="仅管理员可修改所有账号参数")

        account_ids = _list_active_account_ids(cursor)
        if not account_ids:
            conn.close()
            return {"status": "success", "message": "当前没有可下发参数的账号", "affected_accounts": 0}

        cursor.execute('''
            UPDATE users
            SET position_factor = ?, open_count_factor = ?
            WHERE (if_delete != 1 OR if_delete IS NULL)
              AND account_id IS NOT NULL
              AND TRIM(account_id) != ''
        ''', (position_factor, open_count_factor))
        affected_accounts = len(account_ids)
        message = f"已为 {affected_accounts} 个账号统一下发开仓参数"
    else:
        if current_user["role"] != "admin" and current_user["account_id"] != data.account_id:
            conn.close()
            raise HTTPException(status_code=403, detail="没有权限修改此账号参数")

        cursor.execute('''
            UPDATE users
            SET position_factor = ?, open_count_factor = ?
            WHERE account_id = ? AND (if_delete != 1 OR if_delete IS NULL)
        ''', (position_factor, open_count_factor, data.account_id))
        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="账号不存在")
        affected_accounts = 1
        message = f"账号 {data.account_id} 的开仓参数已更新"

    conn.commit()
    conn.close()

    return {
        "status": "success",
        "message": message,
        "affected_accounts": affected_accounts,
        "position_factor": position_factor,
        "open_count_factor": open_count_factor,
    }

# 下发停止交易 / 恢复交易指令
# action 支持: stop (停止全部), stop_buy (停止买入), stop_sell (停止卖出), resume (恢复全部), resume_buy (恢复买入), resume_sell (恢复卖出)
@app.post("/api/account/stop-trading")
async def stop_trading_command(command: CommandCreate, current_user: dict = Depends(get_current_user)):
    # 只有管理员或账号本人可以下发指令
    if current_user["role"] != "admin" and current_user["account_id"] != command.account_id:
        raise HTTPException(status_code=403, detail="没有权限对此账号下发指令")

    conn = get_db_connection()
    cursor = conn.cursor()

    # command_data 支持: stop / stop_buy / stop_sell / resume / resume_buy / resume_sell
    action = command.command_data
    valid_actions = ("stop", "stop_buy", "stop_sell", "resume", "resume_buy", "resume_sell")
    if action not in valid_actions:
        conn.close()
        return {"status": "error", "message": f"无效的指令，应为 {valid_actions}"}

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 确定 buy_stopped / sell_stopped 的变化
    if action == "stop":
        buy_stopped_val, sell_stopped_val = 1, 1
        msg = f"账号 {command.account_id} 已停止交易（买入+卖出），客户端将在下次同步时撤销所有挂单"
    elif action == "stop_buy":
        buy_stopped_val, sell_stopped_val = 1, None  # None 表示不改变
        msg = f"账号 {command.account_id} 已停止买入，客户端将在下次同步时撤销买入挂单"
    elif action == "stop_sell":
        buy_stopped_val, sell_stopped_val = None, 1
        msg = f"账号 {command.account_id} 已停止卖出，客户端将在下次同步时撤销卖出挂单"
    elif action == "resume":
        buy_stopped_val, sell_stopped_val = 0, 0
        msg = f"账号 {command.account_id} 已恢复交易（买入+卖出）"
    elif action == "resume_buy":
        buy_stopped_val, sell_stopped_val = 0, None
        msg = f"账号 {command.account_id} 已恢复买入"
    elif action == "resume_sell":
        buy_stopped_val, sell_stopped_val = None, 0
        msg = f"账号 {command.account_id} 已恢复卖出"
    else:
        conn.close()
        return {"status": "error", "message": "未知指令"}

    # 先确保记录存在
    cursor.execute('''
        INSERT OR IGNORE INTO trading_status (account_id, is_stopped, buy_stopped, sell_stopped)
        VALUES (?, 0, 0, 0)
    ''', (command.account_id,))

    # 构建 SET 子句
    set_parts = []
    params = []
    if buy_stopped_val is not None:
        set_parts.append("buy_stopped = ?")
        params.append(buy_stopped_val)
    if sell_stopped_val is not None:
        set_parts.append("sell_stopped = ?")
        params.append(sell_stopped_val)

    # 如果设置了买入/卖出停止，更新对应时间戳
    if buy_stopped_val == 1 or sell_stopped_val == 1:
        set_parts.append("stopped_at = ?")
        params.append(now_str)
    if buy_stopped_val == 0 and sell_stopped_val == 0:
        set_parts.append("resumed_at = ?")
        params.append(now_str)

    # 更新 is_stopped 为 buy_stopped OR sell_stopped (兼容旧字段)
    set_parts.append("is_stopped = (CASE WHEN (")
    if buy_stopped_val is not None:
        set_parts[-1] += "?"
        params.append(buy_stopped_val)
    else:
        set_parts[-1] += "buy_stopped"
    set_parts[-1] += " OR "
    if sell_stopped_val is not None:
        set_parts[-1] += "?"
        params.append(sell_stopped_val)
    else:
        set_parts[-1] += "sell_stopped"
    set_parts[-1] += ") THEN 1 ELSE 0 END)"

    set_parts.append("update_time = ?")
    params.append(now_str)

    params.append(command.account_id)
    cursor.execute(f"UPDATE trading_status SET {', '.join(set_parts)} WHERE account_id = ?", params)

    conn.commit()
    conn.close()

    return {"status": "success", "message": msg}


# 获取交易状态
@app.get("/api/account/trading-status")
async def get_trading_status(account_id: str, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin" and current_user["account_id"] != account_id:
        raise HTTPException(status_code=403, detail="没有权限查看此账号状态")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT is_stopped, buy_stopped, sell_stopped, stopped_at FROM trading_status WHERE account_id = ?', (account_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return {"account_id": account_id, "is_stopped": bool(row[0]), "buy_stopped": bool(row[1]), "sell_stopped": bool(row[2]), "stopped_at": row[3]}
    else:
        return {"account_id": account_id, "is_stopped": False, "buy_stopped": False, "sell_stopped": False, "stopped_at": None}


# 下发一键清仓指令
@app.post("/api/account/clear-positions")
async def clear_positions_command(command: CommandCreate,
                                  current_user: dict = Depends(get_current_user)):
    """一键清仓：遍历持仓逐笔报卖单，返回每一笔的结果。"""
    if current_user["role"] != "admin" and current_user["account_id"] != command.account_id:
        raise HTTPException(status_code=403, detail="没有权限对此账号下发指令")

    error = verify_clear_password(command.account_id, command.password)
    if error:
        return {"status": "error", "message": error}

    operator = current_user.get("username") or current_user.get("account_id") or ""
    results = clear_account_positions(command.account_id, operator)
    return batch_order_response(results, "账号 %s 一键清仓" % command.account_id)


@app.post("/api/admin/clear-all-positions")
async def clear_all_positions_command(command: CommandCreate,
                                      current_user: dict = Depends(get_current_user)):
    """所有已配置账号一键清仓，仅管理员。"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")

    error = verify_clear_password(current_user["account_id"], command.password)
    if error:
        return {"status": "error", "message": "管理员" + error}

    operator = current_user.get("username") or current_user.get("account_id") or ""
    results = []
    for account_id in bridge_config.account_ids():
        results.extend(clear_account_positions(account_id, operator))
    if not results:
        return {"status": "error", "message": "没有已配置的大QMT 账号"}
    return batch_order_response(results, "全账号一键清仓")


def account_liveness(account_id, now_ts=None):
    """账号在线状态。

    改造前的判据是「QMT 客户端最后一次 push 距今 <= 60s」：客户端进程没了、
    网断了，都要等一整个超时窗口才看得出来，而且服务端完全被动。
    现在看两样东西——服务端自己最后一次成功同步的时刻（sync.poller），
    以及桥接层最近一次 ping 的往返延迟（bridge.pool）。
    """
    now_ts = now_ts if now_ts is not None else datetime.now().timestamp()
    state = sync_poller.last_state(account_id) or {}
    health = bridge_pool.last_health(account_id) or {}
    last = state.get("at") if state.get("ok") else None
    online = None
    if state:
        online = bool(state.get("ok")) and (now_ts - (state.get("at") or 0)) <= 60
    return {
        "last_sync": last,
        "online": online,
        "data_time": (datetime.fromtimestamp(last).strftime('%Y%m%d %H:%M:%S')
                      if last else None),
        "data_delayed": bool(last and (now_ts - last) > 30),
        "sync_error": state.get("error", "") or health.get("error", ""),
        "bridge_latency_ms": health.get("latency_ms"),
    }


def _safe_today_rate(account_id):
    """账号当日收益率（给账号下拉展示用，失败返回 None，与选中该号时的当日盈亏率一致）。"""
    try:
        return round(calculate_today_profit_info(account_id)["today_profit_rate"], 2)
    except Exception:
        return None


@app.get("/api/users")
async def get_all_users(current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()

    # 批量查询所有账号的交易停止状态
    cursor.execute('SELECT account_id, is_stopped, buy_stopped, sell_stopped FROM trading_status')
    trading_status_map = {row[0]: {"is_stopped": bool(row[1]), "buy_stopped": bool(row[2]), "sell_stopped": bool(row[3])} for row in cursor.fetchall()}
    
    def _get_trading_status(aid):
        s = trading_status_map.get(aid, {})
        return s.get("is_stopped", False), s.get("buy_stopped", False), s.get("sell_stopped", False)
    
    if current_user["role"] == "admin":
        # 管理员可以查看所有用户，并添加“所有账号”选项
        users = [{"account_id": "all", "alias": "所有账号汇总", "last_sync": None, "online": None, "data_time": None, "data_delayed": False, "trading_stopped": False, "buy_stopped": False, "sell_stopped": False, "today_profit_rate": _safe_today_rate('all')}]
        cursor.execute('SELECT account_id, alias, COALESCE(is_dormant, 0) FROM users WHERE if_delete != 1 OR if_delete IS NULL')
        rows = cursor.fetchall()
        now_ts = datetime.now().timestamp()
        now_dt = datetime.now()
        for row in rows:
            aid = row[0]
            liveness = account_liveness(aid, now_ts)
            last = liveness["last_sync"]
            online = liveness["online"]
            data_time = liveness["data_time"]
            data_delayed = liveness["data_delayed"]
            trading_stopped, buy_stopped, sell_stopped = _get_trading_status(aid)
            users.append({"account_id": aid, "alias": row[1], "is_dormant": bool(row[2]), "last_sync": last, "online": online, "data_time": data_time, "data_delayed": data_delayed, "trading_stopped": trading_stopped, "buy_stopped": buy_stopped, "sell_stopped": sell_stopped, "today_profit_rate": _safe_today_rate(aid),
                          "sync_error": liveness["sync_error"],
                          "bridge_latency_ms": liveness["bridge_latency_ms"]})
    else:
        # 普通用户只能查看自己
        cursor.execute('SELECT account_id, alias, COALESCE(is_dormant, 0) FROM users WHERE account_id = ? AND (if_delete != 1 OR if_delete IS NULL)', (current_user["account_id"],))
        rows = cursor.fetchall()
        now_ts = datetime.now().timestamp()
        now_dt = datetime.now()
        users = []
        for row in rows:
            aid = row[0]
            liveness = account_liveness(aid, now_ts)
            last = liveness["last_sync"]
            online = liveness["online"]
            data_time = liveness["data_time"]
            data_delayed = liveness["data_delayed"]
            trading_stopped, buy_stopped, sell_stopped = _get_trading_status(aid)
            users.append({"account_id": aid, "alias": row[1], "is_dormant": bool(row[2]), "last_sync": last, "online": online, "data_time": data_time, "data_delayed": data_delayed, "trading_stopped": trading_stopped, "buy_stopped": buy_stopped, "sell_stopped": sell_stopped, "today_profit_rate": _safe_today_rate(aid),
                          "sync_error": liveness["sync_error"],
                          "bridge_latency_ms": liveness["bridge_latency_ms"]})
    
    conn.close()
    return {"users": users}

@app.get("/api/market-data/rt-min")
async def get_market_rt_min_data(current_user: dict = Depends(get_current_user)):
    """返回所有股票的分钟走势和昨收价缓存"""
    return JSONResponse(content={
        "prices": GLOBAL_MARKET_MIN_DATA,
        "last_close": GLOBAL_MARKET_LAST_CLOSE
    })

# 数据获取路由（带权限控制）
@app.get("/api/data")
async def get_data(account_id: str = Query(None, description="用户账号ID"), current_user: dict = Depends(get_current_user)):
    # 如果没有指定account_id，返回当前用户的数据
    if not account_id:
        account_id = current_user["account_id"]
    
    # 检查权限：管理员可以查看所有用户，普通用户只能查看自己
    if current_user["role"] != "admin" and account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    
    # 获取指定用户的数据
    positions = get_positions(account_id)
    trades = get_trades(account_id)
    asset = get_asset(account_id)
    
    # 移除不必要的字段
    for position in positions:
        position.pop('id', None)
        position.pop('update_time', None)

    # 转债持仓补上溢价率/转股价值/强赎进度，前端据此展开转债列
    positions = cb_service.enrich_positions(positions)
    
    for trade in trades:
        trade.pop('id', None)
        trade.pop('update_time', None)
    
    if asset:
        asset.pop('id', None)
        asset.pop('update_time', None)
        asset.pop('account_id', None)
    
    # 获取累计盈亏和收益率；今日收益只计算一次，后续汇总复用同一快照
    today_profit_info = calculate_today_profit_info(account_id)
    profit_info = calculate_total_profit_info(account_id, precomputed_today_info=today_profit_info)
    month_profit_info = calculate_month_profit_info(account_id, precomputed_today_info=today_profit_info)

    # 获取锁定的股票列表：单账号按账号过滤，汇总视图返回并集
    locked_positions = get_locked_positions_for_account(account_id)

    return {
        "trades": trades,
        "positions": positions,
        "locked_positions": locked_positions,
        "asset": asset,
        "total_profit": profit_info["total_profit"],
        "total_profit_rate": profit_info["total_profit_rate"],
        "today_profit": today_profit_info["today_profit"],
        "today_profit_rate": today_profit_info["today_profit_rate"],
        "month_profit": month_profit_info["month_profit"],
        "month_profit_rate": month_profit_info["month_profit_rate"],
        "display_mode": is_display_mode(),
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

# 手动重算今日盈亏接口（仅管理员）
@app.post("/api/admin/recalc-daily-rates")
async def recalc_daily_rates(current_user: dict = Depends(get_current_user)):
    """用修正后的公式重算 daily_profits 历史每日收益率（修复大额提现/入金导致的收益率爆表）。
    昨末资产 = total_asset - daily_profit - capital_adjustment；
    分母 = 昨末资产 + max(0, capital_adjustment)（出金不缩小当日本金基数）。"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, daily_profit, profit_rate, total_asset, capital_adjustment FROM daily_profits")
    rows = cursor.fetchall()
    updated = 0
    changed = 0
    for rid, daily_profit, old_rate, total_asset, capital_adjustment in rows:
        dp = daily_profit or 0
        ta = total_asset or 0
        adj = capital_adjustment or 0
        prev_asset = ta - dp - adj
        denominator = prev_asset + max(0, adj)
        new_rate = round(dp / denominator * 100, 4) if denominator > 0 else 0
        cursor.execute("UPDATE daily_profits SET profit_rate = ? WHERE id = ?", (new_rate, rid))
        updated += 1
        if old_rate is None or abs(float(old_rate or 0) - new_rate) > 1e-6:
            changed += 1
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"已重算 {updated} 条历史每日收益率，修正 {changed} 条", "count": updated, "changed": changed}


@app.post("/api/admin/recalculate-today")
async def recalculate_today(current_user: dict = Depends(get_current_user)):
    """
    手动修正今日盈亏偏差。
    主要处理两种场景：
    1. 今日新加入的账户 —— 其初始资产不应计入当日盈亏，
       自动补录一条 capital_adjustment，金额等于当前总资产。
    2. 今日新标记为休眠的账户 —— 其昨末资产已被排除，但当前资产也已排除，
       两端同步处理无需特殊修正，本接口只处理场景1。
    """
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db_connection()
    cursor = conn.cursor()

    # 找出所有活跃（非休眠、未删除）账户中，今日在 assets 表有资产，
    # 但在 asset_history 里找不到今日之前的任何有效记录的账户。
    # 这类账户是"今日新加入"，其全部资产不应计入盈亏。
    cursor.execute('''
        SELECT a.account_id, a.total_asset
        FROM assets a
        JOIN users u ON a.account_id = u.account_id
        WHERE (u.is_dormant IS NULL OR u.is_dormant = 0)
          AND (u.if_delete IS NULL OR u.if_delete != 1)
          AND a.total_asset > 0
          AND NOT EXISTS (
              SELECT 1 FROM asset_history ah
              WHERE ah.account_id = a.account_id
                AND ah.record_time < date('now', 'localtime')
                AND ah.total_asset > 0
          )
    ''')
    new_accounts = cursor.fetchall()

    adjusted = []
    skipped = []
    for account_id, total_asset in new_accounts:
        # 避免今日重复补录（检查是否已有"重算"类型的调整）
        cursor.execute('''
            SELECT COUNT(*) FROM capital_adjustments
            WHERE account_id = ? AND adjust_time >= ?
              AND remark LIKE '重算-新账户初始资产%'
        ''', (account_id, today_start))
        already_done = cursor.fetchone()[0]

        if already_done:
            skipped.append(account_id)
            continue

        cursor.execute('''
            INSERT INTO capital_adjustments (account_id, amount, remark, adjust_time)
            VALUES (?, ?, ?, ?)
        ''', (
            account_id,
            total_asset,
            f'重算-新账户初始资产 (总资产: {total_asset:.2f})',
            now_str
        ))
        adjusted.append({"account_id": account_id, "amount": round(total_asset, 2)})

    conn.commit()
    conn.close()

    # 重新计算汇总今日盈亏
    today_info = calculate_today_profit_info('all')

    return {
        "status": "success",
        "adjusted_accounts": adjusted,
        "skipped_accounts": skipped,
        "today_profit": round(today_info["today_profit"], 2),
        "today_profit_rate": round(today_info["today_profit_rate"], 4),
        "message": f"完成：补录 {len(adjusted)} 个新账户，跳过 {len(skipped)} 个已处理账户"
    }

# 资金调整接口
@app.post("/api/capital-adjust")
async def adjust_capital(adj: CapitalAdjustment, current_user: dict = Depends(get_current_user)):
    # 检查权限
    if current_user["role"] != "admin" and adj.account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 如果提供了 adjust_time，则手动插入时间，否则使用数据库默认值
    if adj.adjust_time:
        cursor.execute('''
            INSERT INTO capital_adjustments (account_id, amount, remark, adjust_time)
            VALUES (?, ?, ?, ?)
        ''', (adj.account_id, adj.amount, adj.remark, adj.adjust_time))
    else:
        cursor.execute('''
            INSERT INTO capital_adjustments (account_id, amount, remark)
            VALUES (?, ?, ?)
        ''', (adj.account_id, adj.amount, adj.remark))
        
    conn.commit()
    conn.close()
    
    # 重新触发累计盈亏计算
    try:
        update_daily_profits_cache(adj.account_id)
    except Exception as e:
        print(f"Update daily profits cache error after capital adjust: {e}")
        
    return {"status": "success", "message": "资金调整已记录"}

# 获取资金调整记录
@app.get("/api/capital-adjusts")
async def get_capital_adjusts(account_id: str, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin" and account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, amount, remark, adjust_time 
        FROM capital_adjustments 
        WHERE account_id = ? 
        ORDER BY adjust_time DESC
    ''', (account_id,))
    columns = [desc[0] for desc in cursor.description]
    adjusts = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return {"adjusts": adjusts}

# 删除资金调整记录
@app.delete("/api/capital-adjust/{adj_id}")
async def delete_capital_adjust(adj_id: int, current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 先查出所属账号
    cursor.execute('SELECT account_id FROM capital_adjustments WHERE id = ?', (adj_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Adjustment record not found")
    
    account_id = row[0]
    if current_user["role"] != "admin" and account_id != current_user["account_id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Not enough permissions")
    
    cursor.execute('DELETE FROM capital_adjustments WHERE id = ?', (adj_id,))
    conn.commit()
    conn.close()
    
    # 重新触发累计盈亏计算
    try:
        update_daily_profits_cache(account_id)
    except Exception as e:
        print(f"Update daily profits cache error after deleting capital adjust: {e}")
        
    return {"status": "success", "message": "记录已删除"}

def calculate_total_profit_info(account_id, precomputed_today_info=None):
    """
    计算累计盈亏和累计收益率
    逻辑: 
    累计盈亏 = SUM(历史每日盈亏) + 今日实时盈亏
    累计收益率 = SUM(历史每日收益率) + 今日实时收益率
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    if account_id == 'all':
        # 1. 获取今日实时数据
        today_info = precomputed_today_info or calculate_today_profit_info('all')
        
        # 2. 获取所有用户的历史累计盈亏和累计收益率（排除休眠账户）
        # 累计收益率应该是所有账户收益率的总和（每个账户独立计算后相加）
        cursor.execute('''
            SELECT SUM(daily_profit), max(total_profit_rate)
            FROM (
                SELECT account_id, SUM(daily_profit) as daily_profit, sum(profit_rate) as total_profit_rate
                FROM daily_profits
                WHERE account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
                GROUP BY account_id
            )
        ''')
        hist_row = cursor.fetchone()
        historical_profit = hist_row[0] if hist_row and hist_row[0] is not None else 0
        historical_rate = hist_row[1] if hist_row and hist_row[1] is not None else 0
        
        conn.close()
        
        return {
            "total_profit": historical_profit + today_info["today_profit"],
            "total_profit_rate": historical_rate + today_info["today_profit_rate"]
        }

    # 1. 获取今日实时数据
    today_info = precomputed_today_info or calculate_today_profit_info(account_id)
    today_profit = today_info["today_profit"]
    today_profit_rate = today_info["today_profit_rate"]
    
    # 2. 获取历史累计盈亏和累计收益率 (从缓存表)
    cursor.execute('''
        SELECT SUM(daily_profit), SUM(profit_rate) 
        FROM daily_profits 
        WHERE account_id = ?
    ''', (account_id,))
    hist_row = cursor.fetchone()
    historical_profit = hist_row[0] if hist_row and hist_row[0] is not None else 0
    historical_rate = hist_row[1] if hist_row and hist_row[1] is not None else 0
    
    # 3. 检查是否为新账户 (没有历史盈亏缓存且今日是第一次记录)
    # 如果历史记录总数 <= 1，说明是刚创建的账户，累计盈亏和收益率应基于今日变动
    cursor.execute('SELECT COUNT(*) FROM asset_history WHERE account_id = ?', (account_id,))
    history_count = cursor.fetchone()[0]
    
    conn.close()
    
    # 4. 最终汇总
    total_profit = historical_profit + today_profit
    total_profit_rate = historical_rate + today_profit_rate
    
    # 特殊处理：如果是新账户同步的第一天，且没有历史盈亏，累计盈亏和收益率默认为 0
    # 只有当有了第二个数据点，产生了资产变动，才开始计算收益
    if historical_profit == 0 and history_count <= 1:
        total_profit = 0
        total_profit_rate = 0
    
    return {
        "total_profit": total_profit,
        "total_profit_rate": total_profit_rate
    }


def calculate_month_profit_info(account_id, precomputed_today_info=None):
    """本月盈亏与收益率（从本月1日起按每日累加 + 今日实时；daily_profits 已扣资金调整，故收益率从0起算）。"""
    month_start = datetime.now().strftime('%Y-%m-01')
    today_info = precomputed_today_info or calculate_today_profit_info(account_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    if account_id == 'all':
        cursor.execute('''
            SELECT SUM(daily_profit), MAX(month_rate) FROM (
                SELECT account_id, SUM(daily_profit) as daily_profit, SUM(profit_rate) as month_rate
                FROM daily_profits
                WHERE date >= ?
                  AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
                GROUP BY account_id
            )
        ''', (month_start,))
    else:
        cursor.execute('''
            SELECT SUM(daily_profit), SUM(profit_rate)
            FROM daily_profits
            WHERE account_id = ? AND date >= ?
        ''', (account_id, month_start))
    hist_row = cursor.fetchone()
    conn.close()
    hist_profit = hist_row[0] if hist_row and hist_row[0] is not None else 0
    hist_rate = hist_row[1] if hist_row and hist_row[1] is not None else 0
    return {
        "month_profit": hist_profit + today_info["today_profit"],
        "month_profit_rate": hist_rate + today_info["today_profit_rate"],
    }


def calculate_today_profit_info(account_id):
    """
    计算今日盈亏和收益率
    计算公式:
    今日盈亏 = 当前总资产 - 昨末总资产 - 今日资金调整
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    if account_id == 'all':
        # 1. 获取所有当前总资产之和（排除休眠账户）
        cursor.execute('''
            SELECT SUM(total_asset) FROM assets
            WHERE account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        ''')
        current_asset = cursor.fetchone()[0] or 0

        # 2. 获取昨末总资产之和（排除休眠账户）
        # 我们需要每个用户昨末资产的汇总
        cursor.execute('''
            SELECT SUM(total_asset) FROM (
                SELECT total_asset, ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY record_time DESC) as rn
                FROM asset_history
                WHERE record_time < date('now', 'localtime') AND total_asset > 0
                  AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
            ) WHERE rn = 1
        ''')
        yesterday_asset = cursor.fetchone()[0] or 0

        # 如果某些用户没有昨末记录，尝试获取今天最早的有效记录
        if yesterday_asset == 0:
            cursor.execute('''
                SELECT SUM(total_asset) FROM (
                    SELECT total_asset, ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY record_time ASC) as rn
                    FROM asset_history
                    WHERE record_time >= date('now', 'localtime') AND total_asset > 0
                      AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
                ) WHERE rn = 1
            ''')
            yesterday_asset = cursor.fetchone()[0] or current_asset

        # 3. 获取今日总资金调整（排除休眠账户）
        cursor.execute('''
            SELECT SUM(amount) FROM capital_adjustments 
            WHERE adjust_time >= date('now', 'localtime')
              AND account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
        ''')
        today_adjustment = cursor.fetchone()[0] or 0
        
        conn.close()
        
        today_profit = current_asset - yesterday_asset - today_adjustment
        # 当日收益率 = 当日盈亏 / 当日本金基数。出金不缩小基数（白天这笔资金仍在场内），
        # 只让入金增大基数，避免大额提现把分母压到极小导致收益率爆表（如 -100%）。
        denominator = yesterday_asset + max(0, today_adjustment)
        today_profit_rate = (today_profit / denominator * 100) if denominator > 0 else 0
        
        return {
            "today_profit": today_profit,
            "today_profit_rate": today_profit_rate
        }

    # 单个用户逻辑
    # 1. 获取当前总资产
    cursor.execute('SELECT total_asset FROM assets WHERE account_id = ?', (account_id,))
    asset_row = cursor.fetchone()
    current_asset = asset_row[0] if asset_row else 0
    
    # 2. 获取昨末总资产 (今天之前的最后一条有效记录，跳过 total_asset=0 的异常推送)
    cursor.execute('''
        SELECT total_asset FROM asset_history 
        WHERE account_id = ? AND record_time < date('now', 'localtime') AND total_asset > 0
        ORDER BY record_time DESC LIMIT 1
    ''', (account_id,))
    yesterday_asset_row = cursor.fetchone()
    
    if not yesterday_asset_row:
        # 检查今天是否已有"重算-新账户初始资产"调整
        # 若有，说明 recalculate 已经把初始资产设为 capital_adjustment，
        # yesterday_asset 应为 0，让调整值充当基准，与汇总逻辑保持一致。
        cursor.execute('''
            SELECT COUNT(*) FROM capital_adjustments 
            WHERE account_id = ? AND adjust_time >= date('now', 'localtime')
              AND remark LIKE '重算-新账户初始资产%'
        ''', (account_id,))
        has_rebase = cursor.fetchone()[0] > 0

        if has_rebase:
            # 已做重算：yesterday_asset = 0，capital_adjustment 就是开户基准
            yesterday_asset = 0
        else:
            # 未做重算：取今天最早的一条有效记录作为初始基准，避免显示巨额"盈利"
            cursor.execute('''
                SELECT total_asset FROM asset_history 
                WHERE account_id = ? AND record_time >= date('now', 'localtime') AND total_asset > 0
                ORDER BY record_time ASC LIMIT 1
            ''', (account_id,))
            yesterday_asset_row = cursor.fetchone()
            yesterday_asset = yesterday_asset_row[0] if yesterday_asset_row else current_asset
    else:
        yesterday_asset = yesterday_asset_row[0]
    
    # 3. 获取今日资金调整
    cursor.execute('''
        SELECT SUM(amount) FROM capital_adjustments 
        WHERE account_id = ? AND adjust_time >= date('now', 'localtime')
    ''', (account_id,))
    adj_row = cursor.fetchone()
    today_adjustment = adj_row[0] if adj_row and adj_row[0] is not None else 0
    
    conn.close()
    
    # 对于新账户（昨末资产等于当前资产），说明是第一次推送数据
    # 此时当日盈亏应该为0，而不是 -today_adjustment
    if yesterday_asset == current_asset:
        today_profit = 0
        today_profit_rate = 0
    else:
        today_profit = current_asset - yesterday_asset - today_adjustment
        # 当日收益率 = 当日盈亏 / 当日本金基数。出金不缩小基数（白天这笔资金仍在场内），
        # 只让入金增大基数，避免大额提现把分母压到极小导致收益率爆表（如 -100%）。
        denominator = yesterday_asset + max(0, today_adjustment)
        today_profit_rate = (today_profit / denominator * 100) if denominator > 0 else 0
    
    return {
        "today_profit": today_profit,
        "today_profit_rate": today_profit_rate
    }

# 获取历史资产数据（用于资金曲线）
@app.get("/api/asset-history")
async def get_asset_history_endpoint(
    account_id: str = Query(None, description="用户账号ID"),
    hours: int = Query(24, description="获取多少小时内的数据"),
    current_user: dict = Depends(get_current_user)
):
    # 如果没有指定account_id，返回当前用户的数据
    if not account_id:
        account_id = current_user["account_id"]

    # 检查权限
    if current_user["role"] != "admin" and account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    # 1. 获取分时历史数据 (用于资产曲线)
    history = get_asset_history(account_id, hours)

    # 2. 获取每日累计收益率历史 (用于收益率曲线)
    daily_history = []
    
    if account_id == 'all':
        # 聚合所有用户的每日收益率
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 获取所有用户的每日收益率记录
        cursor.execute('''
            SELECT date, AVG(cumulative_profit_rate) as avg_cumulative_rate
            FROM (
                SELECT date, account_id, SUM(profit_rate) OVER (PARTITION BY account_id ORDER BY date ASC) as cumulative_profit_rate
                FROM daily_profits
                WHERE account_id NOT IN (SELECT account_id FROM users WHERE is_dormant = 1)
            )
            GROUP BY date
            ORDER BY date ASC
        ''')
        rows = cursor.fetchall()

        for d, avg_rate in rows:
            daily_history.append({
                "date": d,
                "cumulative_rate": round(avg_rate, 4) if avg_rate is not None else 0
            })
            
        # 加上今日实时平均收益率
        today_info = calculate_today_profit_info('all')
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        # 如果今日实时收益率已经包含在 daily_history 中（虽然不太可能，因为 daily_profits 通常只存历史数据），先移除
        if daily_history and daily_history[-1]["date"] == today_str:
            daily_history.pop()
            
        # 实时计算平均累计收益率：历史最后的平均累计值 + 今日平均收益率
        last_cumulative = daily_history[-1]["cumulative_rate"] if daily_history else 0
        daily_history.append({
            "date": today_str,
            "cumulative_rate": round(last_cumulative + today_info["today_profit_rate"], 4)
        })
        conn.close()
    else:
        # 获取指定用户的收益率
        conn = get_db_connection()
        cursor = conn.cursor()
        # 获取历史每日收益率并计算累计值
        cursor.execute('''
            SELECT date, profit_rate 
            FROM daily_profits 
            WHERE account_id = ? 
            ORDER BY date ASC
        ''', (account_id,))
        all_daily = cursor.fetchall()
        
        cumulative = 0
        for d, rate in all_daily:
            cumulative += (rate or 0)
            daily_history.append({
                "date": d,
                "cumulative_rate": round(cumulative, 4)
            })
        
        # 添加今日实时点
        today_info = calculate_today_profit_info(account_id)
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        if not daily_history or daily_history[-1]["date"] != today_str:
            daily_history.append({
                "date": today_str,
                "cumulative_rate": round(cumulative + today_info["today_profit_rate"], 4)
            })
        conn.close()

    # 只截取用户请求的时间范围内的每日数据
    # 比如请求 7 天，我们就给最近 7 个点
    requested_days = (hours // 24) if hours >= 24 else 1
    daily_history = daily_history[-(requested_days + 1):]

    conn.close()

    return {
        "history": history,
        "daily_history": daily_history
    }

# 获取交易统计信息
@app.get("/api/trade-stats")
async def get_trade_stats_endpoint(
    account_id: str = Query(None, description="用户账号ID"),
    date: str = Query(None, description="查询日期 (YYYY-MM-DD)"),
    current_user: dict = Depends(get_current_user)
):
    # 如果没有指定account_id，返回当前用户的数据
    if not account_id:
        account_id = current_user["account_id"]

    # 检查权限
    if current_user["role"] != "admin" and account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    # 获取统计信息
    stats = get_trade_stats(account_id, date)
    return stats

# T扫描：按账号扫描今日成交，估算当前可做T机会
@app.get("/api/t0-scan")
async def get_t0_scan_endpoint(
    account_id: str = Query(None, description="用户账号ID"),
    date: str = Query(None, description="查询日期 (YYYY-MM-DD)"),
    current_user: dict = Depends(get_current_user)
):
    if not account_id:
        account_id = current_user["account_id"]

    if current_user["role"] != "admin" and account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    if current_user["role"] != "admin" and account_id == "all":
        raise HTTPException(status_code=403, detail="Not enough permissions")

    trade_date = date or datetime.now().strftime("%Y-%m-%d")
    rows, summary = get_t0_scan(account_id, trade_date)
    return {
        "status": "success",
        "account_id": account_id,
        "trade_date": trade_date,
        "rows": rows,
        "summary": summary,
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


# 获取交易日期（用于日历标记）
@app.get("/api/trade-dates")
async def get_trade_dates_endpoint(
    account_id: str = Query(None, description="用户账号ID"),
    days: int = Query(30, description="获取多少天内的数据"),
    current_user: dict = Depends(get_current_user)
):
    # 如果没有指定account_id，返回当前用户的数据
    if not account_id:
        account_id = current_user["account_id"]

    # 检查权限
    if current_user["role"] != "admin" and account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    # 获取交易日期
    dates = get_trade_dates(account_id, days)

    return {"dates": dates}

# 切换持仓锁定状态
@app.post("/api/position/lock")
async def toggle_position_lock(
    lock_data: PositionLock,
    current_user: dict = Depends(get_current_user)
):
    # 检查权限：
    # 1. 管理员可以操作所有账号（包括汇总视图）
    # 2. 普通用户只能操作自己的账号
    is_admin = current_user["role"] == "admin"
    
    if lock_data.account_id == "all":
        # 汇总视图只有管理员可以操作
        if not is_admin:
            raise HTTPException(status_code=403, detail="Only admin can lock/unlock in all accounts view")
    else:
        # 单个账号：管理员可以操作任何人，普通用户只能操作自己
        if not is_admin and lock_data.account_id != current_user["account_id"]:
            raise HTTPException(status_code=403, detail="You can only lock/unlock your own positions")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 如果是汇总视图 (account_id == 'all')，为所有持有该股票的账号添加/移除锁定
    if lock_data.account_id == 'all':
        # 获取所有持有该股票的账号
        cursor.execute('''
        SELECT DISTINCT account_id FROM positions 
        WHERE stock_code = ? 
        AND update_time = (SELECT MAX(update_time) FROM positions p2 WHERE positions.account_id = p2.account_id)
        AND volume > 0
        ''', (lock_data.stock_code,))
        
        account_ids = [row[0] for row in cursor.fetchall()]
        
        # 为每个账号设置锁定状态
        for account_id in account_ids:
            cursor.execute('''
            INSERT INTO position_locks (account_id, stock_code, is_locked, update_time)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, stock_code) DO UPDATE SET
            is_locked = excluded.is_locked,
            update_time = CURRENT_TIMESTAMP
            ''', (account_id, lock_data.stock_code, 1 if lock_data.is_locked else 0))
        
        conn.commit()
        conn.close()

        # 锁定与做T互斥：锁定时自动关掉做T
        if lock_data.is_locked and lock_data.stock_code:
            for aid in account_ids:
                set_t0_status(aid, lock_data.stock_code, 'disable')

        return {
            "status": "success", 
            "is_locked": lock_data.is_locked,
            "affected_accounts": len(account_ids)
        }
    else:
        # 单个账号的锁定操作
        cursor.execute('''
        INSERT INTO position_locks (account_id, stock_code, is_locked, update_time)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(account_id, stock_code) DO UPDATE SET
        is_locked = excluded.is_locked,
        update_time = CURRENT_TIMESTAMP
        ''', (lock_data.account_id, lock_data.stock_code, 1 if lock_data.is_locked else 0))
        
        conn.commit()
        conn.close()

        # 锁定与做T互斥：锁定时自动关掉做T
        if lock_data.is_locked and lock_data.stock_code:
            set_t0_status(lock_data.account_id, lock_data.stock_code, 'disable')

        return {"status": "success", "is_locked": lock_data.is_locked}


# 卖出持仓指令模型
class SellPositionCommand(BaseModel):
    account_id: str
    stock_code: str
    percentage: int


class SellAmountCommand(BaseModel):
    account_id: str
    stock_code: str
    amount: int


# 买入持仓指令模型
class BuyPositionCommand(BaseModel):
    account_id: str
    stock_code: str
    percentage: int


# 新买入指令模型。两种下单方式二选一：
#   amount      按数量（股/份/张，单位随品种）
#   cash_amount 按金额（元），服务端用实时价换算成数量再按品种规整
class NewBuyPositionCommand(BaseModel):
    account_id: str
    stock_code: str
    stock_name: str = ""
    amount: int = 0
    cash_amount: float = 0.0
    force: bool = False


# ============================================================================
# 下单辅助：算量 + 报单
# ============================================================================
# 改造前这些接口做的事是「往内存队列塞一条记录」，等 QMT 客户端来取。现在是当场报单，
# 所以算量必须准 —— 可用数量优先问大QMT 要实时的，SQLite 里的快照最多差一个轮询周期，
# 拿它算卖出量可能报出超过可用数的单子。

def live_position(account_id, stock_code):
    """该账号该股票的实时持仓。优先问大QMT，失败退回本地快照。

    返回 {"volume", "can_use_volume", "source"}；查不到返回 None。
    """
    code = instruments.normalize_code(stock_code)
    try:
        trader = bridge_pool.get_trader(account_id)
        acc = bridge_pool.get_account_ref(account_id)
        position = trader.query_stock_position(acc, code)
        if position is not None:
            volume = int(getattr(position, "volume", 0) or 0)
            return {
                "volume": volume,
                "can_use_volume": int(getattr(position, "can_use_volume", volume) or 0),
                "source": "bridge",
            }
    except bridge_pool.BridgeUnavailable:
        pass
    except Exception as e:
        print(f"[下单] 实时持仓查询失败，退回本地快照 ({account_id} {code}): {e}")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            SELECT volume, can_use_volume FROM positions
            WHERE account_id = ? AND stock_code = ?
            AND update_time = (SELECT MAX(update_time) FROM positions p2
                               WHERE p2.account_id = ? AND p2.stock_code = ?)
        ''', (account_id, code, account_id, code))
        row = cursor.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    volume = int(row[0] or 0)
    return {"volume": volume, "can_use_volume": int(row[1] or volume), "source": "snapshot"}


def accounts_holding(stock_code):
    """持有该股票的所有账号（汇总视图下批量操作用）。"""
    code = instruments.normalize_code(stock_code)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            SELECT DISTINCT account_id FROM positions
            WHERE stock_code = ? AND volume > 0
            AND update_time = (SELECT MAX(update_time) FROM positions p2
                               WHERE positions.account_id = p2.account_id)
        ''', (code,))
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def place_dashboard_order(account_id, stock_code, side, volume, operator="",
                          sell_all=False, remark="dashboard", strategy_name="dashboard"):
    """面板下单统一入口。风控与审计在 bridge.orders 里，这里只负责传参。"""
    return bridge_orders.place_order(
        account_id=account_id, stock_code=stock_code, side=side, volume=volume,
        sell_all=sell_all, operator=operator, remark=remark,
        strategy_name=strategy_name,
    )


def sell_by_percentage(account_id, stock_code, percentage, operator=""):
    """按可用量的百分比卖出。100% 走 sell_all，零股一并卖掉。"""
    position = live_position(account_id, stock_code)
    available = (position or {}).get("can_use_volume", 0)
    if available <= 0:
        return {"ok": False, "account_id": account_id,
                "stock_code": instruments.normalize_code(stock_code),
                "side": bridge_orders.SIDE_SELL, "volume": 0, "status": "rejected",
                "message": "无可用持仓（可能是当日买入尚未解冻）"}
    sell_all = percentage >= 100
    volume = available if sell_all else int(available * percentage / 100)
    return place_dashboard_order(
        account_id, stock_code, bridge_orders.SIDE_SELL, volume,
        operator=operator, sell_all=sell_all,
        remark="sell_%d%%" % percentage,
    )


def clear_account_positions(account_id, operator=""):
    """清掉一个账号的全部可用持仓，逐笔报单。

    改造前是往 account_commands 塞一条 clear_positions 指令，客户端取走后自己遍历
    持仓下单，服务端只知道「指令已下发」，清没清干净并不知道。现在每一笔的结果都在手上。
    零股一并卖出（sell_all），否则不足一手的尾巴永远清不掉。
    """
    try:
        trader = bridge_pool.get_trader(account_id)
        acc = bridge_pool.get_account_ref(account_id)
        positions = trader.query_stock_positions(acc) or []
    except bridge_pool.BridgeUnavailable as e:
        return [{"ok": False, "account_id": account_id, "status": "unavailable",
                 "message": str(e)}]
    except Exception as e:
        return [{"ok": False, "account_id": account_id, "status": "failed",
                 "message": "查询持仓失败: %s" % e}]

    results = []
    for position in positions:
        available = int(getattr(position, "can_use_volume", 0) or 0)
        if available <= 0:
            continue
        results.append(place_dashboard_order(
            account_id, getattr(position, "stock_code", ""), bridge_orders.SIDE_SELL,
            available, operator=operator, sell_all=True, remark="clear_positions",
        ))
    if not results:
        return [{"ok": False, "account_id": account_id, "status": "noop",
                 "message": "无可卖持仓"}]
    return results


def verify_clear_password(account_id, password):
    """校验清仓密码。通过返回 None，否则返回错误信息。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT clear_password FROM users WHERE account_id = ?', (account_id,))
        row = cursor.fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return "未设置清仓密码，请先设置"
    if row[0] != password:
        return "清仓密码错误"
    return None


def cancel_open_orders(account_id, stock_code=None, side=None, operator=""):
    """撤掉该账号（可选按股票、按方向）所有可撤委托。

    改造前 /api/position/sell/cancel 做的是「把还没下发的指令从内存队列里删掉」——
    单子根本还没报出去。现在单子是立刻报的，所以「取消」只能是真撤单。
    """
    code = instruments.normalize_code(stock_code) if stock_code else None
    try:
        trader = bridge_pool.get_trader(account_id)
        acc = bridge_pool.get_account_ref(account_id)
        open_orders = trader.query_stock_orders(acc, cancelable_only=True) or []
    except bridge_pool.BridgeUnavailable as e:
        return [{"ok": False, "account_id": account_id, "status": "unavailable",
                 "message": str(e)}]
    except Exception as e:
        return [{"ok": False, "account_id": account_id, "status": "failed",
                 "message": "查询可撤委托失败: %s" % e}]

    wanted_type = None
    if side == bridge_orders.SIDE_BUY:
        wanted_type = 23
    elif side == bridge_orders.SIDE_SELL:
        wanted_type = 24

    results = []
    for order in open_orders:
        if code and instruments.normalize_code(getattr(order, "stock_code", "")) != code:
            continue
        if wanted_type is not None and int(getattr(order, "order_type", 0) or 0) != wanted_type:
            continue
        results.append(bridge_orders.cancel_order(
            account_id, order_id=getattr(order, "order_id", None), operator=operator))
    return results


def volume_from_cash(stock_code, cash_amount, price=None):
    """把「我要买 N 元」换算成可申报数量。

    返回 (volume, price, error)。价格取实时价；取不到就没法换算 —— 这时宁可报错，
    也不能拿昨收或者瞎猜的价格去决定买多少。
    """
    try:
        cash_amount = float(cash_amount or 0)
    except (TypeError, ValueError):
        return 0, None, "金额格式不对"
    if cash_amount <= 0:
        return 0, None, "金额必须大于 0"

    price = price or bridge_market.last_price(stock_code)
    if not price or price <= 0:
        return 0, None, "取不到 %s 的实时价，无法按金额换算数量" % stock_code

    spec = instruments.describe(stock_code)
    raw = int(cash_amount // price)
    volume = instruments.round_volume(stock_code, raw)
    if volume <= 0:
        need = spec["min_volume"] * price
        return 0, price, ("%.2f 元不够一手：%s 最少 %d%s，约需 %.2f 元"
                          % (cash_amount, spec["code"], spec["min_volume"],
                             spec["unit"], need))
    return volume, price, ""


def order_response(result):
    """把 bridge.orders 的结果翻成接口响应；被拒/失败一律 400 带原因。"""
    if result.get("ok"):
        return {
            "status": "success",
            "message": result.get("message") or "已报单",
            "order_sys_id": result.get("order_sys_id"),
            "stock_code": result.get("stock_code"),
            "volume": result.get("volume"),
            "unit": result.get("unit"),
            "price_type": result.get("price_type"),
        }
    raise HTTPException(status_code=400, detail=result.get("message") or "下单失败")


def batch_order_response(results, action):
    """批量下单（汇总视图、清仓）的汇总响应：成功几笔、失败几笔、每笔原因。"""
    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    return {
        "status": "success" if ok else "error",
        "message": "%s：成功 %d 笔，失败 %d 笔" % (action, len(ok), len(failed)),
        "succeeded": len(ok),
        "failed": len(failed),
        "details": [
            {"account_id": r.get("account_id"), "stock_code": r.get("stock_code"),
             "volume": r.get("volume"), "status": r.get("status"),
             "order_sys_id": r.get("order_sys_id"), "message": r.get("message")}
            for r in results
        ],
    }


# MySQL 股票数据库：配置见 config/mysql.json 或 STOCK_DB_* 环境变量。
# 未配置时 get_stock_db_connection() 抛 MySQLUnavailable，调用方
# (load_stock_basic_items / load_etf_items) 已有 try/except，退回本地缓存。
from plugins.mysql_client import (
    get_stock_db_connection,
    stock_db_available,
    MySQLUnavailable,
)


# 搜索股票/ETF 列表（下单用）
@app.get("/api/stocks/search")
async def search_stocks(
    q: str = Query("", description="股票/ETF 名称或代码关键词"),
    current_user: dict = Depends(get_current_user)
):
    """从内存缓存搜索：股票(bak_basic) + ETF(etf_type_snapshot)，按名称或代码匹配。
    两份列表均常驻内存（每小时刷新），不再每次按键都打 MySQL。ETF 单独配额，保证可被搜到。"""
    kw = (q or "").strip().lower()
    if not kw:
        return []
    digits = re.sub(r"\D", "", kw)

    def collect(items, is_etf, cap):
        scored = []
        for it in items:
            ts = str(it.get("ts_code", ""))
            name = str(it.get("name", ""))
            sym = str(it.get("symbol", "")) or ts.split(".")[0]
            # 优先用缓存预存的小写；tushare 回退路径无此字段时再实时计算
            ts_l = it.get("ts_lower") or ts.lower()
            name_l = it.get("name_lower") or name.lower()
            if not (kw in ts_l or kw in name_l or (digits and digits in sym)):
                continue
            # 相关性：精确 > 前缀 > 子串
            if name_l == kw or ts_l == kw or (digits and sym == digits):
                score = 0
            elif name_l.startswith(kw) or (digits and sym.startswith(digits)):
                score = 1
            else:
                score = 2
            row = {"ts_code": ts, "name": name, "is_etf": is_etf}
            if is_etf:
                row["etf_type"] = it.get("etf_type", "")
            scored.append((score, len(name), name, row))
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        return [r for *_, r in scored[:cap]]

    try:
        stocks = collect(load_stock_basic_items(), False, 50)
        etfs = collect(load_etf_items(), True, 30)
    except Exception as e:
        print(f"[下单搜索] MySQL 名录不可用，只用本地来源: {e}")
        stocks, etfs = [], []

    # 转债不在 bak_basic 里，永远得从这儿来；顺便在没配 MySQL 时兜住股票搜索。
    bonds = collect(load_bond_search_items(), False, 30)
    holdings = collect(load_holding_search_items(), False, 20)

    seen = set()
    merged = []
    for row in stocks + etfs + bonds + holdings:
        if row["ts_code"] in seen:
            continue
        seen.add(row["ts_code"])
        merged.append(row)
    return merged


def load_bond_search_items():
    """可转债名录，来自 cb_reference（akshare 日更）。"""
    items = []
    for row in cb_reference.load_all().values():
        code = row.get("bond_code") or ""
        name = row.get("bond_name") or ""
        if not code:
            continue
        items.append({
            "ts_code": code, "name": name, "symbol": code.split(".")[0],
            "ts_lower": code.lower(), "name_lower": name.lower(),
        })
    return items


def load_holding_search_items():
    """自己持有的标的。搜不到别的时至少能搜到手上的票。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            SELECT DISTINCT stock_code, instrument_name FROM positions
            WHERE volume > 0 AND stock_code IS NOT NULL
        ''')
        rows = cursor.fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    items = []
    for code, name in rows:
        code = (code or "").strip()
        name = (name or "").strip()
        if not code:
            continue
        items.append({
            "ts_code": code, "name": name, "symbol": code.split(".")[0],
            "ts_lower": code.lower(), "name_lower": name.lower(),
        })
    return items


# 新买入股票指令（不依赖已有持仓，直接指定股数）
@app.post("/api/position/buy_new")
async def set_buy_new_position_command(
    command: NewBuyPositionCommand,
    current_user: dict = Depends(get_current_user)
):
    """按绝对数量新开仓。

    数量校验按品种走 bridge.instruments —— 改造前这里写死 `amount % 100 != 0`，
    可转债 10 张的单子会被直接判成非法参数。
    """
    is_admin = current_user["role"] == "admin"
    if not is_admin and command.account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="只能操作自己的账号")

    spec = instruments.describe(command.stock_code)

    # 按金额下单：服务端用实时价换算，保证换算结果就是实际报单量
    volume = command.amount
    if command.cash_amount and command.cash_amount > 0:
        volume, _, error = volume_from_cash(command.stock_code, command.cash_amount)
        if error:
            raise HTTPException(status_code=400, detail=error)
    elif volume <= 0:
        raise HTTPException(status_code=400, detail="请给出买入数量或买入金额")

    if (volume < spec["min_volume"]
            or (volume - spec["min_volume"]) % spec["volume_step"] != 0):
        raise HTTPException(
            status_code=400,
            detail="%s 最少 %d%s，之后按 %d%s 递增" % (
                spec["code"], spec["min_volume"], spec["unit"],
                spec["volume_step"], spec["unit"]))

    if command.account_id == "all":
        if not is_admin:
            raise HTTPException(status_code=403, detail="只有管理员可以操作汇总视图")
        targets = bridge_config.account_ids()
        if not targets:
            raise HTTPException(status_code=400, detail="没有已配置的大QMT 账号")
    else:
        targets = [command.account_id]

    operator = current_user.get("username") or current_user.get("account_id") or ""
    # 手动新开仓沿用老行为：忽略「停止买入」开关（老客户端的 ignore_stop_buy）。
    # 绕过动作会写进 order_audit，不是悄悄放行。
    results = [
        bridge_orders.place_order(
            account_id=account_id, stock_code=command.stock_code,
            side=bridge_orders.SIDE_BUY, volume=volume,
            operator=operator, remark="buy_new", bypass=["stop_buy"],
        )
        for account_id in targets
    ]

    if command.account_id == "all":
        return batch_order_response(
            results, "买入 %s %d%s" % (spec["code"], volume, spec["unit"]))
    return order_response(results[0])


# ============================================================================
#                      历史买入列表（stock_market_data）
# ============================================================================
def _code_to_ts_code(code: str) -> str:
    """6 位股票代码补全交易所后缀，转成 tushare ts_code 格式（如 000681 -> 000681.SZ）。
    已带后缀的原样返回。6/9 开头 -> SH，0/3 -> SZ，4/8 -> BJ（北交所）。"""
    code = str(code or "").strip()
    if not code or "." in code:
        return code
    head = code[0]
    if head in ("6", "9"):
        return f"{code}.SH"
    if head in ("4", "8"):
        return f"{code}.BJ"
    return f"{code}.SZ"


# 题材/市值 富集缓存：5 分钟内复用，避免 5 秒轮询频繁打 tushare/MySQL
_MARKET_TODAY_ENRICH = {"date": "", "ts": 0.0, "data": {}}


def _enrich_market_today_records(records):
    """尽力富集 题材 + 当前总市值(亿)，按交易日缓存 5 分钟。失败不影响主数据。
    records 为 list[dict]，原地写入 topic / market_value_yi 字段。"""
    today = datetime.now().strftime("%Y%m%d")
    now_ts = time.time()
    cache = _MARKET_TODAY_ENRICH
    codes = {r["ts_code"] for r in records if r.get("ts_code")}
    # 日期变更 / 超过 5 分钟 / 出现缓存里没有的新代码 -> 重新富集
    stale = (cache["date"] != today
             or now_ts - cache["ts"] > 300
             or any(c not in cache["data"] for c in codes))
    if stale:
        try:
            metrics = fetch_metrics_batch(codes)
        except Exception as e:
            print(f"[历史买入] 富集市值失败: {e}")
            metrics = {}
        try:
            resolved = [{"code": r["ts_code"], "name": r.get("stock_name", "")} for r in records]
            topics = fetch_limit_up_topics_batch(resolved)
        except Exception as e:
            print(f"[历史买入] 富集题材失败: {e}")
            topics = {}
        data = {}
        for r in records:
            c = r["ts_code"]
            mv, _pct = (metrics.get(c) or (None, None))
            tp = topics.get(c) or topics.get(r.get("stock_name")) or {}
            data[c] = {
                "market_value_yi": mv,
                "topic": tp.get("topic") or tp.get("limit_up_reason") or tp.get("concept") or "",
            }
        cache.update(date=today, ts=now_ts, data=data)
    for r in records:
        e = cache["data"].get(r["ts_code"], {})
        r["market_value_yi"] = e.get("market_value_yi")
        r["topic"] = e.get("topic", "")


@app.get("/api/market-data/today")
async def get_market_data_today(current_user: dict = Depends(get_user_or_viewer)):
    """读取 stock_market_data 当日报出的股票，按报出时间(update_time)降序。
    附带：报出时涨跌幅、最新价、DDE、大单净流入、题材、市值、近 7 日报出次数。"""
    today = datetime.now().strftime("%Y%m%d")
    try:
        conn = get_stock_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT stock_code, stock_name, current_change, latest_price, DDE,
                   大单净流入, update_time
            FROM stock_market_data
            WHERE date_string = %s
            ORDER BY update_time DESC
            """,
            (today,),
        )
        rows = cursor.fetchall()
        # 近 7 天（含今日）报出次数：按出现的不同日期计数（每股每日仅一条）
        cutoff_7d = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
        cursor.execute(
            """
            SELECT stock_code, COUNT(DISTINCT date_string) AS cnt
            FROM stock_market_data
            WHERE date_string >= %s
            GROUP BY stock_code
            """,
            (cutoff_7d,),
        )
        report_counts = {r[0]: int(r[1]) for r in cursor.fetchall()}
        conn.close()
    except MySQLUnavailable:
        # 没配 MySQL 就没有「今日报出」这份数据源。返回空列表并说明原因，
        # 不要 500 —— 这是可选数据源，缺了不该把整个面板打挂。
        return {"status": "success", "records": [], "count": 0,
                "note": "未配置股票基础库（config/mysql.json 或 STOCK_DB_* 环境变量），"
                        "「今日报出」列表为空"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取 stock_market_data 失败: {str(e)}")

    ts_codes = [_code_to_ts_code(row[0]) for row in rows if row and row[0]]
    ensure_realtime_change_cache(ts_codes)
    rt_map = _RT_CHANGE_CACHE.get("data", {})
    rt_ts = _RT_CHANGE_CACHE.get("ts", 0.0)
    records = []
    for row in rows:
        raw_code = row[0]
        ts_code = _code_to_ts_code(raw_code)
        records.append({
            "stock_code": raw_code,
            "ts_code": ts_code,
            "stock_name": row[1] or "",
            "current_change": float(row[2]) if row[2] is not None else None,
            # 动态涨跌幅：由统一实时涨幅任务(tushare rt_k 每1min)写入缓存，qmt 不实时更新时回退
            "dynamic_change": rt_map.get(ts_code),
            "latest_price": float(row[3]) if row[3] is not None else None,
            "dde": float(row[4]) if row[4] is not None else None,
            "big_order_net": float(row[5]) if row[5] is not None else None,
            "update_time": str(row[6]) if row[6] is not None else "",
            "report_count_7d": report_counts.get(raw_code, 1),
        })

    # 尽力富集题材与市值（缓存 5 分钟，失败不影响主数据）
    try:
        _enrich_market_today_records(records)
    except Exception as e:
        print(f"[历史买入] 富集失败(忽略): {e}")

    return {"status": "success", "trade_date": today, "records": records,
            "dynamic_change_ts": rt_ts}


class MarketDataKlineRequest(BaseModel):
    stock_code: str
    stock_name: str = ""
    topic: str = ""


@app.post("/api/market-data/kline-analysis")
async def analyze_market_data_kline(
    req: MarketDataKlineRequest,
    current_user: dict = Depends(get_current_user),
):
    """对历史买入列表中的个股做大模型 K 线分析（复用记录板的分析逻辑）。"""
    ts_code = _code_to_ts_code(req.stock_code)
    if not ts_code:
        raise HTTPException(status_code=400, detail="缺少股票代码，无法获取K线")
    record = {
        "stock_name": req.stock_name or "",
        "stock_code": ts_code,
        "logic": "",
        "topic": req.topic or "",
        "concept": "",
        "limit_up_reason": req.topic or "",
        "target_market_value_yi": None,
        "current_market_value_yi": None,
    }
    result = await asyncio.to_thread(analyze_research_record_kline, record)
    result["stock_name"] = req.stock_name or ""
    result["stock_code"] = ts_code
    return result


# 设置卖出持仓指令
@app.post("/api/position/sell")
async def set_sell_position_command(
    command: SellPositionCommand,
    current_user: dict = Depends(get_current_user)
):
    """按比例卖出。

    改造前：把 {stock_code, percentage} 塞进 pending_sell_commands，等 QMT 客户端
    下次轮询取走、自己算量自己下单，服务端拿不到任何结果。
    现在：按实时可用量当场算量、当场报单，返回真实委托号，失败原因直接回前端。
    """
    is_admin = current_user["role"] == "admin"
    if not is_admin and command.account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="只能操作自己的持仓")
    if command.percentage < 10 or command.percentage > 100 or command.percentage % 10 != 0:
        raise HTTPException(status_code=400, detail="卖出比例必须是10-100之间的10的倍数")

    if command.account_id == "all":
        if not is_admin:
            raise HTTPException(status_code=403, detail="只有管理员可以在汇总视图操作")
        targets = accounts_holding(command.stock_code)
        if not targets:
            raise HTTPException(status_code=400, detail="没有账号持有 %s" % command.stock_code)
    else:
        targets = [command.account_id]

    operator = current_user.get("username") or current_user.get("account_id") or ""
    results = [
        sell_by_percentage(account_id, command.stock_code, command.percentage, operator)
        for account_id in targets
    ]

    if command.account_id == "all":
        return batch_order_response(
            results, "卖出 %s %d%%" % (command.stock_code, command.percentage))
    return order_response(results[0])


@app.post("/api/position/sell_amount")
async def set_sell_amount_command(
    command: SellAmountCommand,
    current_user: dict = Depends(get_current_user)
):
    """按绝对数量卖出。数量单位随品种：股票是股，可转债是张。"""
    is_admin = current_user["role"] == "admin"
    if not is_admin and command.account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="只能操作自己的持仓")
    if command.amount <= 0:
        raise HTTPException(status_code=400, detail="卖出数量必须大于 0")

    position = live_position(command.account_id, command.stock_code)
    available = (position or {}).get("can_use_volume", 0)
    if available <= 0:
        raise HTTPException(status_code=400, detail="无可用持仓（可能是当日买入尚未解冻）")

    volume = min(command.amount, available)
    operator = current_user.get("username") or current_user.get("account_id") or ""
    # 要卖的量已等于全部可用量时按清仓处理，零股才不会被规整掉
    result = place_dashboard_order(
        command.account_id, command.stock_code, bridge_orders.SIDE_SELL, volume,
        operator=operator, sell_all=(volume >= available), remark="sell_amount",
    )
    return order_response(result)


@app.post("/api/position/sell/cancel")
async def cancel_sell_position_command(
    command: SellPositionCommand,
    current_user: dict = Depends(get_current_user)
):
    """撤掉该股票所有可撤的卖单。

    改造前这里是把还没下发的指令从内存队列里删掉 —— 单子根本还没报出去。
    现在单子是立刻报的，「取消」只能是真撤单。
    """
    is_admin = current_user["role"] == "admin"
    if not is_admin and command.account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="只能操作自己的持仓")

    operator = current_user.get("username") or current_user.get("account_id") or ""
    targets = (accounts_holding(command.stock_code)
               if command.account_id == "all" else [command.account_id])
    results = []
    for account_id in targets:
        results.extend(cancel_open_orders(
            account_id, command.stock_code, bridge_orders.SIDE_SELL, operator))
    if not results:
        return {"status": "success", "message": "没有可撤的卖单",
                "succeeded": 0, "failed": 0, "details": []}
    return batch_order_response(results, "撤销 %s 卖单" % command.stock_code)


class T0Command(BaseModel):
    account_id: str
    stock_code: str = ""  # 为空时全局操作
    action: str  # 'enable', 'disable', 'force_rebalance'


def set_t0_status(account_id, stock_code, action):
    """记录某只股票的做T开关状态。

    改造前这里做两件事：往 account_commands 塞一条指令让 QMT 客户端取走执行，
    再顺手更新 t0_status。直连之后没有客户端可下发 —— 做T执行归服务端，
    这张表就是唯一状态来源。
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # 确保 t0_status 表存在（兼容旧库无此表的情况）
        cur.execute('''CREATE TABLE IF NOT EXISTS t0_status (
            account_id TEXT,
            stock_code TEXT,
            enabled INTEGER DEFAULT 1,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account_id, stock_code),
            FOREIGN KEY (account_id) REFERENCES users (account_id)
        )''')
        if action == 'enable':
            cur.execute('''INSERT OR REPLACE INTO t0_status
                (account_id, stock_code, enabled, update_time)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP)''',
                (account_id, stock_code))
        elif action == 'disable':
            cur.execute('''DELETE FROM t0_status
                WHERE account_id = ? AND stock_code = ?''',
                (account_id, stock_code))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[做T] 保存做T状态失败: {e}")


# 做T指令端点 (个股级别)
@app.post("/api/position/t0")
async def set_t0_command(
    command: T0Command,
    current_user: dict = Depends(get_current_user)
):
    """开关某只股票的做T。

    改造前这里既写状态又往指令队列塞一条给 QMT 客户端。直连之后做T执行在服务端，
    这个接口就只剩「改状态」一件事，引擎按 t0_status 决定盯哪些票。
    """
    is_admin = current_user["role"] == "admin"
    if not is_admin and command.account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="只能操作自己的账号")
    if command.action not in ('enable', 'disable', 'force_rebalance'):
        raise HTTPException(status_code=400, detail="无效的做T指令类型")
    if not command.stock_code:
        raise HTTPException(status_code=400, detail="请指定股票代码")

    # 做T与锁仓互斥：启用做T时自动解锁
    if command.action == 'enable':
        conn = get_db_connection()
        cursor = conn.cursor()
        if command.account_id == 'all':
            cursor.execute('DELETE FROM position_locks WHERE stock_code = ? AND is_locked = 1',
                           (command.stock_code,))
        else:
            cursor.execute(
                'DELETE FROM position_locks WHERE account_id = ? AND stock_code = ? AND is_locked = 1',
                (command.account_id, command.stock_code))
        conn.commit()
        conn.close()

    if command.account_id == "all":
        if not is_admin:
            raise HTTPException(status_code=403, detail="只有管理员可以在汇总视图操作")
        targets = accounts_holding(command.stock_code)
    else:
        targets = [command.account_id]

    for account_id in targets:
        set_t0_status(account_id, command.stock_code, command.action)

    return {
        "status": "success",
        "message": "已为 %d 个账号设置 %s 做T: %s" % (
            len(targets), command.stock_code, command.action),
        "affected_accounts": len(targets),
    }


class DeletePositionRequest(BaseModel):
    account_id: str
    stock_code: str


@app.delete("/api/position/{stock_code}")
async def delete_position(
    stock_code: str,
    account_id: str = Query(..., description="用户账号ID"),
    current_user: dict = Depends(get_current_user)
):
    """删除指定持仓记录（仅删除 dashboard 显示，不影响 QMT 实际持仓）"""
    is_admin = current_user["role"] == "admin"
    if not is_admin and account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="只能操作自己的账号")
    if account_id == "all":
        raise HTTPException(status_code=400, detail="汇总视图不支持删除持仓，请切换到具体账号操作")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        DELETE FROM positions WHERE account_id = ? AND stock_code = ?
    ''', (account_id, stock_code))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted == 0:
        raise HTTPException(status_code=404, detail="未找到该持仓记录")

    return {"status": "success", "message": f"已删除 {stock_code} 的持仓记录", "deleted": deleted}


# 设置买入持仓指令
@app.post("/api/position/buy")
async def set_buy_position_command(
    command: BuyPositionCommand,
    current_user: dict = Depends(get_current_user)
):
    """按现有持仓的百分比加仓。无持仓则拒绝（新开仓走 /api/position/buy_new）。"""
    is_admin = current_user["role"] == "admin"
    if not is_admin and command.account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="只能操作自己的持仓")
    if command.percentage <= 0 or command.percentage > 100 or command.percentage % 10 != 0:
        raise HTTPException(status_code=400, detail="加仓比例必须是10-100之间的10的倍数")

    operator = current_user.get("username") or current_user.get("account_id") or ""
    targets = (accounts_holding(command.stock_code)
               if command.account_id == "all" else [command.account_id])

    results = []
    for account_id in targets:
        position = live_position(account_id, command.stock_code)
        held = (position or {}).get("volume", 0)
        if held <= 0:
            results.append({"ok": False, "account_id": account_id,
                            "stock_code": instruments.normalize_code(command.stock_code),
                            "side": bridge_orders.SIDE_BUY, "volume": 0,
                            "status": "rejected", "message": "无持仓，加仓比例无从计算"})
            continue
        # 仓位系数：改造前是下发给客户端让它自己乘，现在服务端算完再报单
        factor = position_factor_of(account_id)
        volume = int(held * command.percentage / 100 * factor)
        results.append(place_dashboard_order(
            account_id, command.stock_code, bridge_orders.SIDE_BUY, volume,
            operator=operator, remark="buy_%d%%" % command.percentage))

    if command.account_id == "all":
        return batch_order_response(
            results, "加仓 %s %d%%" % (command.stock_code, command.percentage))
    return order_response(results[0])


@app.post("/api/position/buy/cancel")
async def cancel_buy_position_command(
    command: BuyPositionCommand,
    current_user: dict = Depends(get_current_user)
):
    """撤掉该股票所有可撤的买单。"""
    is_admin = current_user["role"] == "admin"
    if not is_admin and command.account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="只能操作自己的持仓")

    operator = current_user.get("username") or current_user.get("account_id") or ""
    targets = (bridge_config.account_ids()
               if command.account_id == "all" else [command.account_id])
    results = []
    for account_id in targets:
        results.extend(cancel_open_orders(
            account_id, command.stock_code, bridge_orders.SIDE_BUY, operator))
    if not results:
        return {"status": "success", "message": "没有可撤的买单",
                "succeeded": 0, "failed": 0, "details": []}
    return batch_order_response(results, "撤销 %s 买单" % command.stock_code)


def backup_database():
    """备份数据库到 backup 目录"""
    try:
        # 创建备份目录
        backup_dir = "backup"
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        
        # 生成备份文件名：dashboard_YYYYMMDD_HHMMSS.db
        now = datetime.now()
        backup_filename = f"dashboard_{now.strftime('%Y%m%d_%H%M%S')}.db"
        backup_path = os.path.join(backup_dir, backup_filename)
        
        # 复制数据库文件
        shutil.copy2(DB_PATH, backup_path)
        
        # 清理旧备份（保留最近7天的备份）
        cleanup_old_backups(backup_dir, keep_days=7)
        
        print(f"[数据库备份] 成功备份到: {backup_path}")
    except Exception as e:
        print(f"[数据库备份] 备份失败: {e}")

def cleanup_old_backups(backup_dir, keep_days=30):
    """清理旧的备份文件，只保留最近 N 天的"""
    try:
        now = datetime.now()
        for filename in os.listdir(backup_dir):
            if filename.startswith("dashboard_") and filename.endswith(".db"):
                file_path = os.path.join(backup_dir, filename)
                # 获取文件修改时间
                file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                # 如果文件超过 keep_days 天，则删除
                if (now - file_mtime).days > keep_days:
                    os.remove(file_path)
                    print(f"[数据库备份] 清理旧备份: {filename}")
    except Exception as e:
        print(f"[数据库备份] 清理旧备份失败: {e}")


# 手动触发分钟K线回补指令（管理员）
@app.post("/api/admin/backfill-kline")
async def admin_backfill_kline(data: dict, current_user: dict = Depends(get_current_user)):
    """手动补齐当前持仓的分钟线。

    改造前是给每个账号下发 backfill_kline 指令，等客户端拉了再推回来，接口只能回
    「已下发」。现在当场从大QMT 拉完再返回，补了多少只是确定的。
    """
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")

    account_id = data.get("account_id", "all")
    codes = data.get("codes") or get_current_holding_codes(
        None if account_id == "all" else account_id)
    if not codes:
        return {"status": "success", "message": "当前没有持仓，无需回补", "filled": 0}

    filled = backfill_minute_bars(codes)
    return {
        "status": "success",
        "message": "已补齐 %d/%d 只标的的分钟线" % (filled, len(codes)),
        "filled": filled,
        "codes": codes,
    }


@app.post("/api/admin/refresh-kline")
async def admin_refresh_kline(current_user: dict = Depends(get_current_user)):
    """清空走势图缓存并从大QMT 重新拉一遍当前持仓的分钟线。"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")

    global GLOBAL_MARKET_MIN_DATA, GLOBAL_MARKET_MIN_DATA_RAW, _BACKFILL_KLINE_ISSUED

    codes = get_current_holding_codes()
    GLOBAL_MARKET_MIN_DATA.clear()
    GLOBAL_MARKET_MIN_DATA_RAW.clear()
    _BACKFILL_KLINE_ISSUED.clear()

    filled = backfill_minute_bars(codes) if codes else 0
    return {
        "status": "success",
        "message": "已清空缓存，当前持仓 %d 只，从大QMT 补回 %d 只" % (len(codes), filled),
        "codes_count": len(codes),
        "cached_count": len(GLOBAL_MARKET_MIN_DATA),
        "filled": filled,
    }


# ====== Strategy.ini 远程管理 (通过指令下发到 QMT 端写入) ======
_LAST_STRATEGY_INI_CONTENT = {}  # {account_id: content}
_STOCK_BASIC_CACHE = {"loaded_at": None, "items": []}
_ETF_BASIC_CACHE = {"loaded_at": None, "items": []}  # 下单搜索用的 ETF 列表（etf_type_snapshot）
_RESEARCH_BOARD_TASKS = {}
_RESEARCH_BOARD_TASK_LOCK = threading.Lock()


def create_research_board_task(content: str, created_by: str):
    task_id = uuid.uuid4().hex
    task = {
        "task_id": task_id,
        "status": "processing",
        "content": content,
        "created_by": created_by,
        "count": 0,
        "records": [],
        "error": "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
    }
    with _RESEARCH_BOARD_TASK_LOCK:
        _RESEARCH_BOARD_TASKS[task_id] = task
    return task_id


def get_research_board_task(task_id: str):
    with _RESEARCH_BOARD_TASK_LOCK:
        task = _RESEARCH_BOARD_TASKS.get(task_id)
        return dict(task) if task else None


def update_research_board_task(task_id: str, **updates):
    with _RESEARCH_BOARD_TASK_LOCK:
        if task_id not in _RESEARCH_BOARD_TASKS:
            return None
        _RESEARCH_BOARD_TASKS[task_id].update(updates)
        return dict(_RESEARCH_BOARD_TASKS[task_id])


def create_research_board_input(task_id: str, content: str, created_by: str):
    """归档每次记录板输入的原始消息，返回归档行 id，便于后续复核解析结果。"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO research_board_inputs (task_id, content, created_by, status)
            VALUES (?, ?, ?, 'processing')
        ''', (task_id, content, created_by))
        input_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return input_id
    except Exception as e:
        print(f"[记录板] 归档输入失败: {e}")
        return None


def update_research_board_input(input_id, **fields):
    """回写归档行：解析来源、大模型原始返回、解析结果、生成的记录 id、状态等。"""
    if not input_id or not fields:
        return
    allowed = {"parse_source", "llm_raw_response", "parsed_records_json",
               "record_ids", "error", "status", "finished_at"}
    sets, values = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            values.append(value)
    if not sets:
        return
    values.append(input_id)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE research_board_inputs SET {', '.join(sets)} WHERE id = ?", values)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[记录板] 更新归档输入失败: {e}")


def extract_json_payload(text: str):
    """从大模型回复中抽取 JSON 数组或对象。"""
    if not text:
        return []

    cleaned = text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else [data]
    except Exception:
        pass

    start_positions = [pos for pos in (cleaned.find("["), cleaned.find("{")) if pos >= 0]
    if not start_positions:
        return []
    start = min(start_positions)
    end = cleaned.rfind("]") if cleaned[start] == "[" else cleaned.rfind("}")
    if end <= start:
        return []
    data = json.loads(cleaned[start:end + 1])
    return data if isinstance(data, list) else [data]


def parse_market_value_yi(value):
    """把 '300亿以上'、300、'3,000' 等目标市值归一成亿元。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(value).replace(",", ""))
    return float(match.group(1)) if match else None


def normalize_research_records(records):
    """归一化模型返回字段，避免不同模型 key 命名不一致。"""
    normalized = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        stock_name = (
            item.get("stock_name") or item.get("name") or item.get("股票名称") or
            item.get("股票") or item.get("display_name") or ""
        )
        stock_code = (
            item.get("stock_code") or item.get("ts_code") or item.get("code") or
            item.get("symbol") or item.get("股票代码") or item.get("代码") or ""
        )
        logic = (
            item.get("logic") or item.get("reason") or item.get("逻辑") or
            item.get("investment_logic") or item.get("summary") or ""
        )
        target_mv = (
            item.get("target_market_value_yi") or item.get("target_market_value") or
            item.get("目标市值") or item.get("target_mv")
        )
        stock_name = str(stock_name).strip()
        stock_code = str(stock_code).strip()
        logic = str(logic).strip()
        if not stock_name and not stock_code and not logic:
            continue
        normalized.append({
            "stock_name": stock_name,
            "stock_code": stock_code,
            "logic": logic,
            "target_market_value_yi": parse_market_value_yi(target_mv),
        })
    return normalized


def fallback_parse_research_text(content: str):
    records = []
    for part in re.split(r"[；;\n]+", content or ""):
        text = part.strip(" ，,。")
        if not text:
            continue
        target = None
        target_match = re.search(r"(?:市值)?看(?:到)?\s*([0-9]+(?:\.[0-9]+)?)\s*亿", text)
        if target_match:
            target = float(target_match.group(1))
        # 6-digit code: regex word-boundary is unreliable around CJK; drop it and take group 1
        code_match = re.search(r"(\d{6})(?:\.(?:SZ|SH|BJ|sz|sh|bj))?", text)
        stock_code = code_match.group(1) if code_match else ""
        name_match = re.match(r"^([\u4e00-\u9fa5A-Za-z]{2,8})", text)
        stock_name = name_match.group(1) if name_match else ""
        records.append({
            "stock_name": stock_name,
            "stock_code": stock_code,
            "logic": text,
            "target_market_value_yi": target,
        })
    return records


def get_setting(setting_key: str, default_value: str = ""):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT setting_value FROM app_settings WHERE setting_key = ?", (setting_key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default_value


def save_setting(setting_key: str, setting_value: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO app_settings (setting_key, setting_value, update_time)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(setting_key) DO UPDATE SET
            setting_value = excluded.setting_value,
            update_time = CURRENT_TIMESTAMP
    ''', (setting_key, setting_value))
    conn.commit()
    conn.close()


# 展示模式（服务端全局开关，仅管理员可改）：开启后 POST /api/data 不再下发任何
# 买入/卖出/做T/锁仓/清仓指令，整套部署变为只读展示（客户端仍可推送持仓、行情照常更新）。
# 状态持久化在 app_settings 表；缓存加 2 秒 TTL，保证重启后恢复、且多 worker 间最多 2 秒内同步一致。
_DISPLAY_MODE_CACHE = {"value": None, "ts": 0.0}


def is_display_mode():
    nowt = time.time()
    if _DISPLAY_MODE_CACHE["value"] is None or (nowt - _DISPLAY_MODE_CACHE["ts"]) > 2:
        _DISPLAY_MODE_CACHE["value"] = (get_setting("display_mode", "0") == "1")
        _DISPLAY_MODE_CACHE["ts"] = nowt
    return _DISPLAY_MODE_CACHE["value"]


def set_display_mode(enabled: bool):
    save_setting("display_mode", "1" if enabled else "0")
    _DISPLAY_MODE_CACHE["value"] = bool(enabled)
    _DISPLAY_MODE_CACHE["ts"] = time.time()


@app.get("/api/display-mode")
async def read_display_mode(current_user: dict = Depends(get_current_user)):
    return {"status": "success", "enabled": is_display_mode()}


@app.post("/api/display-mode")
async def write_display_mode(data: dict, current_user: dict = Depends(get_current_admin)):
    enabled = bool(data.get("enabled"))
    set_display_mode(enabled)
    return {
        "status": "success",
        "enabled": enabled,
        "message": "展示模式已开启（已停止下发买卖/锁仓/清仓指令）" if enabled else "展示模式已关闭",
    }


def get_llm_config_dict(mask_key: bool = False):
    raw = get_setting("llm_config", "{}")
    try:
        config = json.loads(raw or "{}")
    except Exception:
        config = {}
    config = {
        "api_url": config.get("api_url", ""),
        "api_key": config.get("api_key", ""),
        "model": config.get("model", ""),
    }
    if mask_key and config["api_key"]:
        config["api_key"] = "********" + config["api_key"][-4:]
    return config


def call_llm_for_research_records(content: str, config: dict):
    api_url = (config.get("api_url") or "").strip()
    api_key = (config.get("api_key") or "").strip()
    model = (config.get("model") or "").strip()
    if not api_url or not api_key or not model:
        return [], ""

    if not api_url.rstrip("/").endswith("/chat/completions"):
        api_url = api_url.rstrip("/") + "/chat/completions"

    prompt = (
        "请把下面的股票研究记录逐条解析为 JSON 数组，每条对象包含字段：\n"
        "- stock_name：股票简称，只取简称本身，不要带后面的逻辑描述"
        "（例如“胜宏科技继续看好”取“胜宏科技”，“鸿仕达，继续看好”取“鸿仕达”）；"
        "若一句以“继续看好”等词开头或同时提到多只股票，取该句主要论述对象/给出市值目标的那只"
        "（如“继续看好锐翔智能和鸿仕达…鸿仕达市值看到200亿”取“鸿仕达”）；\n"
        "- stock_code：6位股票代码字符串；若文中出现（如“920178（锐翔智能）”“春光集团（301531）”）则填写，否则填\"\"；\n"
        "- logic：该条完整逻辑原文；\n"
        "- target_market_value_yi：目标市值（亿元，数字），无法判断填 null。\n"
        "不同记录通常以“；”分隔。严格只输出 JSON 数组，不要 markdown、不要解释。\n\n"
        "用户输入：\n" + content
    )
    resp = requests.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "你是股票研究记录结构化助手，只返回合法 JSON 数组。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    return normalize_research_records(extract_json_payload(text)), text


def load_stock_basic_items():
    now = datetime.now()
    cached_at = _STOCK_BASIC_CACHE.get("loaded_at")
    if cached_at and (now - cached_at).total_seconds() < 3600 and _STOCK_BASIC_CACHE.get("items"):
        return _STOCK_BASIC_CACHE["items"]
    # 优先从 MySQL 的 bak_basic 取最新快照（名称/代码/行业）
    try:
        conn = get_stock_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ts_code, name, industry FROM bak_basic
            WHERE trade_date = (SELECT MAX(trade_date) FROM bak_basic)
        ''')
        rows = cursor.fetchall()
        conn.close()
        items = []
        for ts_code, name, industry in rows:
            ts_code = (ts_code or "").strip()
            name = (name or "").strip()
            items.append({
                "ts_code": ts_code,
                "name": name,
                "symbol": ts_code.split(".")[0],
                "industry": (industry or "").strip(),
                "ts_lower": ts_code.lower(),     # 预存小写，搜索时免去每次按键重复 .lower()
                "name_lower": name.lower(),
            })
        if items:
            _STOCK_BASIC_CACHE.update({"loaded_at": now, "items": items})
            return items
    except Exception as e:
        print(f"[记录板] 从 bak_basic 获取股票基础信息失败，回退 tushare: {e}")
    # 回退：tushare stock_basic
    try:
        df = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,symbol')
        items = df.to_dict("records") if df is not None else []
        _STOCK_BASIC_CACHE.update({"loaded_at": now, "items": items})
        return items
    except Exception as e:
        print(f"[记录板] 获取股票基础信息失败: {e}")
        return _STOCK_BASIC_CACHE.get("items", [])


def load_etf_items():
    """ETF 列表（来自 stock_db_daily.etf_type_snapshot 最新交易日快照），带 1 小时内存缓存。
    返回 [{ts_code, name, symbol, etf_type, is_etf:True}]，供下单搜索框使用。
    code 字段本身已是带后缀代码（513120.SH / 159338.SZ）。"""
    now = datetime.now()
    cached_at = _ETF_BASIC_CACHE.get("loaded_at")
    if cached_at and (now - cached_at).total_seconds() < 3600 and _ETF_BASIC_CACHE.get("items"):
        return _ETF_BASIC_CACHE["items"]
    try:
        conn = get_stock_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT code, name, etf_type FROM etf_type_snapshot
            WHERE trade_date = (SELECT MAX(trade_date) FROM etf_type_snapshot)
        ''')
        rows = cursor.fetchall()
        conn.close()
        items = []
        seen = set()
        for code, name, etf_type in rows:
            code = (code or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            name = (name or "").strip()
            items.append({
                "ts_code": code,
                "name": name,
                "symbol": code.split(".")[0],
                "etf_type": (etf_type or "").strip(),
                "is_etf": True,
                "ts_lower": code.lower(),        # 预存小写，搜索时免去每次按键重复 .lower()
                "name_lower": name.lower(),
            })
        if items:
            _ETF_BASIC_CACHE.update({"loaded_at": now, "items": items})
            return items
        return _ETF_BASIC_CACHE.get("items", [])
    except Exception as e:
        print(f"[下单搜索] 加载 ETF 列表失败: {e}")
        return _ETF_BASIC_CACHE.get("items", [])


def find_stock_by_name(stock_name: str):
    if not stock_name:
        return None
    for item in load_stock_basic_items():
        if item.get("name") == stock_name or stock_name in item.get("name", ""):
            return item
    return None


def resolve_stock(name_hint: str = "", code_hint: str = ""):
    """用 tushare 权威股票列表，从（可能不干净的）名称/代码线索解析出 {ts_code, name}。
    优先级：6位代码精确 > 名称完全相等 > 名称最长前缀（处理“胜宏科技继续看好”这类名字与逻辑粘连）。"""
    items = load_stock_basic_items()
    name_hint = (name_hint or "").strip()
    digits = re.sub(r"\D", "", code_hint or "")[:6]

    if digits:
        for item in items:
            ts = str(item.get("ts_code", ""))
            if ts.split(".")[0] == digits or str(item.get("symbol", "")) == digits:
                return item

    if name_hint:
        for item in items:
            if item.get("name") == name_hint:
                return item
        best = None
        for item in items:
            nm = item.get("name", "")
            if nm and name_hint.startswith(nm):
                if best is None or len(nm) > len(best.get("name", "")):
                    best = item
        if best:
            return best
    return None


def find_dominant_stock_in_text(text: str):
    """在整段文本里扫描所有出现的已知股票名（bak_basic），返回出现次数最多者（并列取最早出现）。
    用于「继续看好X和Y……」这类不以股名开头、且一句提到多只股票的情况。"""
    text = (text or "").strip()
    if not text:
        return None
    items = load_stock_basic_items()
    by_name = {}
    for it in items:
        nm = (it.get("name") or "").strip()
        if nm:
            by_name[nm] = it
    if not by_name:
        return None
    counts = {}  # name -> [出现次数, 首次位置]
    i, n = 0, len(text)
    while i < n:
        if "一" <= text[i] <= "龥":
            matched = None
            for length in range(6, 1, -1):
                cand = text[i:i + length]
                if len(cand) == length and cand in by_name:
                    matched = cand
                    break
            if matched:
                if matched not in counts:
                    counts[matched] = [0, i]
                counts[matched][0] += 1
                i += len(matched)
                continue
        i += 1
    if not counts:
        return None
    best_name = min(counts.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[0]
    return by_name.get(best_name)


def resolve_stock_smart(name_hint: str = "", code_hint: str = "", text: str = ""):
    """先按名称/代码线索解析；都失败时扫描全文找出现最多的已知股票兜底。"""
    stock = resolve_stock(name_hint=name_hint, code_hint=code_hint)
    if not stock and text:
        stock = find_dominant_stock_in_text(text)
    return stock


def fetch_stock_metrics(ts_code: str):
    if not ts_code:
        return None, None
    current_market_value_yi = None
    current_change = None
    try:
        df_basic = pro.daily_basic(ts_code=ts_code, fields='ts_code,trade_date,total_mv')
        if df_basic is not None and not df_basic.empty:
            latest = df_basic.sort_values("trade_date", ascending=False).iloc[0]
            total_mv = latest.get("total_mv")
            if pd.notna(total_mv):
                current_market_value_yi = float(total_mv) / 10000
    except Exception as e:
        print(f"[记录板] 获取 {ts_code} 市值失败: {e}")
    try:
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
        df_daily = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df_daily is not None and not df_daily.empty:
            latest = df_daily.sort_values("trade_date", ascending=False).iloc[0]
            pct_chg = latest.get("pct_chg")
            if pd.notna(pct_chg):
                current_change = float(pct_chg)
    except Exception as e:
        print(f"[记录板] 获取 {ts_code} 涨跌幅失败: {e}")
    return current_market_value_yi, current_change


def enrich_research_record(record: dict):
    name_hint = record.get("stock_name", "")
    code_hint = record.get("stock_code", "")
    # 用权威列表校正：代码优先，其次名称最长前缀；都失败再扫描全文找主角股票
    stock = resolve_stock_smart(name_hint=name_hint, code_hint=code_hint, text=record.get("logic", ""))
    if stock:
        stock_code = stock.get("ts_code", "") or code_hint
        display_name = stock.get("name", "") or name_hint
    else:
        stock_code = code_hint
        display_name = name_hint
    current_market_value_yi, current_change = fetch_stock_metrics(stock_code)
    limit_up_topic = fetch_latest_limit_up_topic(display_name, stock_code)
    result = {
        **record,
        "stock_name": display_name,
        "stock_code": stock_code,
        "current_market_value_yi": current_market_value_yi,
        "current_change": current_change,
        **limit_up_topic,
    }
    # 题材表没有行业时，用 bak_basic 的行业兜底
    if not result.get("industry") and stock:
        result["industry"] = stock.get("industry", "")
    return result


def normalize_limit_up_topic_row(row):
    if not row:
        return {
            "topic": "",
            "industry": "",
            "concept": "",
            "limit_up_reason": "",
            "limit_up_trade_date": "",
        }
    if not isinstance(row, dict):
        row = {
            "industry": row[0] if len(row) > 0 else "",
            "concept": row[1] if len(row) > 1 else "",
            "limit_up_reason": row[2] if len(row) > 2 else "",
            "trade_date": row[3] if len(row) > 3 else "",
        }
    industry = str(row.get("industry") or "").strip()
    concept = str(row.get("concept") or "").strip()
    reason = str(row.get("limit_up_reason") or "").strip()
    trade_date = str(row.get("trade_date") or row.get("limit_up_trade_date") or "").strip()
    topic_parts = [part for part in [concept, reason] if part]
    if not topic_parts and industry:
        topic_parts = [industry]
    return {
        "topic": " / ".join(topic_parts),
        "industry": industry,
        "concept": concept,
        "limit_up_reason": reason,
        "limit_up_trade_date": trade_date,
    }


def fetch_latest_limit_up_topic(stock_name: str = "", stock_code: str = ""):
    name = (stock_name or "").strip()
    code = (stock_code or "").strip()
    if not name and not code:
        return normalize_limit_up_topic_row(None)
    try:
        conn = get_stock_db_connection()
        cursor = conn.cursor()
        if code and name:
            cursor.execute('''
                SELECT industry, concept, limit_up_reason, trade_date
                FROM limit_up_data
                WHERE ts_code = %s OR name = %s
                ORDER BY trade_date DESC
                LIMIT 1
            ''', (code, name))
        elif code:
            cursor.execute('''
                SELECT industry, concept, limit_up_reason, trade_date
                FROM limit_up_data
                WHERE ts_code = %s
                ORDER BY trade_date DESC
                LIMIT 1
            ''', (code,))
        else:
            cursor.execute('''
                SELECT industry, concept, limit_up_reason, trade_date
                FROM limit_up_data
                WHERE name = %s
                ORDER BY trade_date DESC
                LIMIT 1
            ''', (name,))
        row = cursor.fetchone()
        conn.close()
        return normalize_limit_up_topic_row(row)
    except Exception as e:
        print(f"[记录板] 查询 {name or code} 涨停原因失败: {e}")
        return normalize_limit_up_topic_row(None)


def _safe_float(value):
    """把 tushare/pandas 的数值安全转成 float，缺失返回 None。"""
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def fetch_kline_60d(ts_code: str, limit: int = 60):
    """获取最近 limit 个交易日的日 K 线（按日期升序）。"""
    code = (ts_code or "").strip()
    if not code:
        return []
    try:
        end_date = datetime.now().strftime("%Y%m%d")
        # 多取一些自然日，确保覆盖到 limit 个交易日（含停牌/节假日）
        start_date = (datetime.now() - timedelta(days=limit * 2 + 40)).strftime("%Y%m%d")
        df = pro.daily(
            ts_code=code,
            start_date=start_date,
            end_date=end_date,
            fields="trade_date,open,high,low,close,vol,amount,pct_chg",
        )
        if df is None or df.empty:
            return []
        df = df.sort_values("trade_date").tail(limit)
        bars = []
        for _, r in df.iterrows():
            bars.append({
                "date": str(r.get("trade_date") or ""),
                "open": _safe_float(r.get("open")),
                "high": _safe_float(r.get("high")),
                "low": _safe_float(r.get("low")),
                "close": _safe_float(r.get("close")),
                "vol": _safe_float(r.get("vol")),
                "amount": _safe_float(r.get("amount")),
                "pct_chg": _safe_float(r.get("pct_chg")),
            })
        return bars
    except Exception as e:
        print(f"[记录板] 获取 {code} {limit}日K线失败: {e}")
        return []


def summarize_kline_stats(bars):
    """对日 K 线计算区间涨跌、均线、回撤、量能等统计，供前端展示与喂给大模型。"""
    closes = [b["close"] for b in bars if b.get("close") is not None]
    if not closes:
        return {}
    first_close, last_close = closes[0], closes[-1]
    peak, max_dd = closes[0], 0.0
    for c in closes:
        if c > peak:
            peak = c
        if peak > 0:
            max_dd = min(max_dd, (c - peak) / peak * 100)

    def ma(n):
        return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

    period_change = (last_close - first_close) / first_close * 100 if first_close else None
    up_days = sum(1 for b in bars if (b.get("pct_chg") or 0) > 0)
    down_days = sum(1 for b in bars if (b.get("pct_chg") or 0) < 0)

    vols = [b["vol"] for b in bars if b.get("vol") is not None]
    recent_vol = sum(vols[-5:]) / len(vols[-5:]) if len(vols) >= 5 else None
    earlier_vol = sum(vols[:-5]) / len(vols[:-5]) if len(vols) > 5 else None
    vol_change = ((recent_vol - earlier_vol) / earlier_vol * 100) if (recent_vol and earlier_vol) else None

    return {
        "days": len(bars),
        "start_date": bars[0]["date"],
        "end_date": bars[-1]["date"],
        "first_close": round(first_close, 2),
        "last_close": round(last_close, 2),
        "period_change_pct": round(period_change, 2) if period_change is not None else None,
        "highest": round(max(closes), 2),
        "lowest": round(min(closes), 2),
        "max_drawdown_pct": round(max_dd, 2),
        "ma5": ma(5),
        "ma10": ma(10),
        "ma20": ma(20),
        "ma60": ma(60),
        "up_days": up_days,
        "down_days": down_days,
        "vol_change_pct": round(vol_change, 2) if vol_change is not None else None,
    }


def build_kline_prompt(record: dict, bars: list, stats: dict):
    """把研究记录 + K线统计 + 每日明细整理成给大模型的文本。"""
    lines = [f"股票：{record.get('stock_name') or ''}（{record.get('stock_code') or ''}）"]
    if record.get("logic"):
        lines.append(f"我的逻辑：{record.get('logic')}")
    topic = record.get("topic") or record.get("limit_up_reason") or record.get("concept")
    if topic:
        lines.append(f"题材：{topic}")
    if record.get("target_market_value_yi"):
        lines.append(f"目标市值：{record.get('target_market_value_yi')}亿")
    if record.get("current_market_value_yi"):
        lines.append(f"当前市值：{record.get('current_market_value_yi')}亿")
    lines.append("")
    lines.append(f"近{stats.get('days')}个交易日（{stats.get('start_date')}~{stats.get('end_date')}）行情概况：")
    lines.append(f"区间涨跌 {stats.get('period_change_pct')}%，最高 {stats.get('highest')}，最低 {stats.get('lowest')}，最大回撤 {stats.get('max_drawdown_pct')}%")
    lines.append(f"均线 MA5={stats.get('ma5')} MA10={stats.get('ma10')} MA20={stats.get('ma20')} MA60={stats.get('ma60')}，最新收盘 {stats.get('last_close')}")
    lines.append(f"阳线 {stats.get('up_days')} 天 / 阴线 {stats.get('down_days')} 天，近5日量能较前期变化 {stats.get('vol_change_pct')}%")
    lines.append("")
    lines.append("每日(日期/收盘/涨跌幅%)：")
    lines.append("; ".join(f"{b['date']} {b['close']} {b['pct_chg']}%" for b in bars))
    return "\n".join(lines)


def call_llm_for_kline_analysis(record: dict, bars: list, stats: dict, config: dict):
    """调用 OpenAI 兼容接口，让大模型分析最近 60 天 K 线走势。"""
    api_url = (config.get("api_url") or "").strip()
    api_key = (config.get("api_key") or "").strip()
    model = (config.get("model") or "").strip()
    if not api_url or not api_key or not model:
        return ""
    if not api_url.rstrip("/").endswith("/chat/completions"):
        api_url = api_url.rstrip("/") + "/chat/completions"

    user_content = (
        "请根据下面个股最近约60个交易日的日K数据，分析其走势，并结合我的投资逻辑给出研判。要点：\n"
        "1) 当前趋势（上升/下降/震荡）及所处阶段；2) 量价关系；"
        "3) 关键支撑位与压力位（给出价格）；4) 当前相对均线与区间高低点的位置；"
        "5) 结合逻辑/题材的看法与需要关注的信号；6) 风险提示。\n"
        "用简洁中文分点输出，控制在400字以内，不要使用markdown表格。\n\n"
        + build_kline_prompt(record, bars, stats)
    )
    resp = requests.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "你是资深A股技术分析师，擅长结合基本面逻辑解读个股K线走势，给出客观、可执行的研判，并始终提示风险。"},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.4,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()


def fallback_kline_analysis(record: dict, bars: list, stats: dict):
    """未配置/调用大模型失败时，用规则生成一段走势摘要。"""
    if not stats:
        return "未获取到足够的K线数据，无法分析。"
    last = stats.get("last_close")
    chg = stats.get("period_change_pct") or 0
    trend = "上升" if chg > 8 else ("下降" if chg < -8 else "震荡")
    pos = []
    if stats.get("ma20") and last is not None:
        pos.append("站上20日均线" if last >= stats["ma20"] else "处于20日均线下方")
    if stats.get("ma60") and last is not None:
        pos.append("站上60日均线" if last >= stats["ma60"] else "处于60日均线下方")
    parts = [
        f"近{stats.get('days')}个交易日（{stats.get('start_date')}~{stats.get('end_date')}）整体呈{trend}态势，区间涨跌{stats.get('period_change_pct')}%。",
        f"期间最高{stats.get('highest')}、最低{stats.get('lowest')}，最大回撤{stats.get('max_drawdown_pct')}%，最新收盘{last}。",
    ]
    if pos:
        parts.append("当前" + "、".join(pos) + "。")
    if stats.get("vol_change_pct") is not None:
        vc = stats["vol_change_pct"]
        parts.append(f"近5日量能较前期{'放大' if vc >= 0 else '萎缩'}{abs(vc)}%。")
    parts.append("（未配置大模型，以上为规则统计摘要，仅供参考，注意风险。）")
    return "".join(parts)


def analyze_research_record_kline(record: dict):
    """阻塞式：取K线 -> 统计 -> 大模型分析（失败回退规则摘要）。在线程池中执行。"""
    bars = fetch_kline_60d(record.get("stock_code"), 60)
    if not bars:
        return {"status": "success", "source": "none",
                "analysis": "未获取到该股票的K线数据，无法分析。", "stats": {}, "bars": []}
    stats = summarize_kline_stats(bars)
    source, analysis = "fallback", ""
    try:
        analysis = call_llm_for_kline_analysis(record, bars, stats, get_llm_config_dict(mask_key=False))
        if analysis:
            source = "llm"
    except Exception as e:
        print(f"[记录板] K线大模型分析失败，改用规则摘要: {e}")
        analysis = ""
    if not analysis:
        analysis = fallback_kline_analysis(record, bars, stats)
    return {"status": "success", "source": source, "analysis": analysis, "stats": stats, "bars": bars}


@app.get("/api/llm-config")
async def read_llm_config(current_user: dict = Depends(get_current_admin)):
    return {"status": "success", "config": get_llm_config_dict(mask_key=True)}


@app.post("/api/llm-config")
async def write_llm_config(config: LLMConfig, current_user: dict = Depends(get_current_admin)):
    old_config = get_llm_config_dict(mask_key=False)
    api_key = config.api_key.strip()
    if api_key.startswith("********"):
        api_key = old_config.get("api_key", "")
    save_setting("llm_config", json.dumps({
        "api_url": config.api_url.strip(),
        "api_key": api_key,
        "model": config.model.strip(),
    }, ensure_ascii=False))
    return {"status": "success", "message": "大模型配置已保存"}


@app.get("/api/research-board")
async def read_research_board(current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, stock_name, stock_code, logic, topic, industry, concept,
               limit_up_reason, limit_up_trade_date, target_market_value_yi,
               current_market_value_yi, current_change, raw_text, created_by,
               created_at, update_time
        FROM research_board_records
        ORDER BY id DESC
        LIMIT 200
    ''')
    rows = cursor.fetchall()
    conn.close()
    records = []
    for row in rows:
        records.append({
            "id": row[0],
            "stock_name": row[1],
            "stock_code": row[2],
            "logic": row[3],
            "topic": row[4],
            "industry": row[5],
            "concept": row[6],
            "limit_up_reason": row[7],
            "limit_up_trade_date": row[8],
            "target_market_value_yi": row[9],
            "current_market_value_yi": row[10],
            "current_change": row[11],
            "raw_text": row[12],
            "created_by": row[13],
            "created_at": row[14],
            "update_time": row[15],
        })
    return {"status": "success", "records": records}


@app.post("/api/research-board/parse")
async def parse_research_board(req: ResearchBoardParseRequest, current_user: dict = Depends(get_current_user)):
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="请输入记录内容")

    task_id = create_research_board_task(content, current_user.get("username", ""))
    threading.Thread(
        target=run_research_board_parse_task,
        args=(task_id, content, current_user.get("username", "")),
        daemon=True
    ).start()
    return {
        "status": "processing",
        "task_id": task_id,
        "message": "记录板解析已提交，完成后会自动更新表格"
    }


@app.post("/api/research-board/refresh-change")
async def refresh_research_board_change(current_user: dict = Depends(get_current_user)):
    """立即刷新记录板个股当日涨跌幅（点「刷新」时调用，不必等后台定时任务）。
    数据源 = fetch_realtime_change_map（tushare pro.daily 最新交易日，按交易日缓存）。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT stock_code FROM research_board_records "
                   "WHERE stock_code IS NOT NULL AND stock_code != ''")
    codes = [row[0] for row in cursor.fetchall() if row[0]]
    conn.close()
    updated = 0
    if codes:
        changes = fetch_realtime_change_map(set(codes))
        if changes:
            conn2 = get_db_connection()
            cursor2 = conn2.cursor()
            for code, pct in changes.items():
                cursor2.execute("UPDATE research_board_records SET current_change = ? WHERE stock_code = ?",
                                (pct, code))
                updated += 1
            conn2.commit()
            conn2.close()
            try:
                sync_local_research_records()
            except Exception as e:
                print(f"[记录板] 刷新涨跌幅同步 MySQL 失败: {e}")
    return {"status": "success", "updated": updated}


@app.post("/api/research-board/repair")
async def repair_research_board(current_user: dict = Depends(get_current_user)):
    """一键修复：用 bak_basic 重新校正所有记录的名称/代码，并刷新市值与题材。"""
    task_id = create_research_board_task("[一键修复]", current_user.get("username", ""))
    threading.Thread(target=run_research_board_repair_task, args=(task_id,), daemon=True).start()
    return {
        "status": "processing",
        "task_id": task_id,
        "message": "一键修复已提交，正在用最新基础信息重新整理记录..."
    }


def run_research_board_repair_task(task_id: str):
    try:
        result = repair_all_research_board_records(task_id)
        update_research_board_task(
            task_id,
            status="completed",
            total=result.get("total", 0),
            fixed=result.get("fixed", 0),
            count=result.get("fixed", 0),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception as e:
        update_research_board_task(
            task_id,
            status="failed",
            error=str(e),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )


def recent_trade_dates(n: int = 5):
    """返回最近 n 个交易日（YYYYMMDD，倒序）。"""
    try:
        end = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=40)).strftime('%Y%m%d')
        df = pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
        if df is not None and not df.empty:
            dates = sorted([str(d) for d in df['cal_date'].tolist()], reverse=True)
            return dates[:n]
    except Exception as e:
        print(f"[记录板] 获取交易日历失败: {e}")
    return [datetime.now().strftime('%Y%m%d')]


def fetch_metrics_batch(ts_codes):
    """一次性拉取一批股票的当前总市值(亿)与当日涨跌幅。返回 {ts_code: (mv_yi, pct_chg)}。
    用 trade_date 全市场拉取（2 次 tushare 调用），自动回退到更早的交易日直到拿到数据。"""
    codes = {c for c in (ts_codes or []) if c}
    result = {c: (None, None) for c in codes}
    if not codes:
        return result
    mv_map, pct_map = {}, {}
    for td in recent_trade_dates(5):
        if not mv_map:
            try:
                df = pro.daily_basic(trade_date=td, fields='ts_code,total_mv')
                if df is not None and not df.empty:
                    mv_map = {r['ts_code']: float(r['total_mv']) / 10000
                              for _, r in df.iterrows() if pd.notna(r.get('total_mv'))}
            except Exception as e:
                print(f"[记录板] 批量拉取市值失败({td}): {e}")
        if not pct_map:
            try:
                df2 = pro.daily(trade_date=td, fields='ts_code,pct_chg')
                if df2 is not None and not df2.empty:
                    pct_map = {r['ts_code']: float(r['pct_chg'])
                               for _, r in df2.iterrows() if pd.notna(r.get('pct_chg'))}
            except Exception as e:
                print(f"[记录板] 批量拉取涨跌幅失败({td}): {e}")
        if mv_map and pct_map:
            break
    for c in codes:
        result[c] = (mv_map.get(c), pct_map.get(c))
    return result


def fetch_limit_up_topics_batch(resolved):
    """一次性查询 limit_up_data，返回 {ts_code: topic_dict, name: topic_dict}（每只取最新一条）。"""
    result = {}
    codes = sorted({r.get("code") for r in resolved if r.get("code")})
    names = sorted({r.get("name") for r in resolved if r.get("name")})
    if not codes and not names:
        return result
    try:
        conn = get_stock_db_connection()
        cursor = conn.cursor()
        conds, params = [], []
        if codes:
            placeholders = ",".join(["%s"] * len(codes))
            conds.append(f"ts_code IN ({placeholders})")
            params.extend(codes)
        if names:
            placeholders = ",".join(["%s"] * len(names))
            conds.append(f"name IN ({placeholders})")
            params.extend(names)
        sql = ("SELECT ts_code, name, industry, concept, limit_up_reason, trade_date "
               "FROM limit_up_data WHERE " + " OR ".join(conds) + " ORDER BY trade_date DESC")
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        for ts_code, name, industry, concept, reason, trade_date in rows:
            topic = normalize_limit_up_topic_row({
                "industry": industry, "concept": concept,
                "limit_up_reason": reason, "trade_date": trade_date,
            })
            if ts_code and ts_code not in result:
                result[ts_code] = topic
            if name and name not in result:
                result[name] = topic
    except Exception as e:
        print(f"[记录板] 批量查询题材失败: {e}")
    return result


def repair_all_research_board_records(task_id: str = ""):
    """遍历所有记录，用改进后的规则 + bak_basic 重新解析名称/代码，并批量补全市值与题材。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, stock_name, stock_code, logic, target_market_value_yi
        FROM research_board_records ORDER BY id DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    total = len(rows)
    if task_id:
        update_research_board_task(task_id, processed=0, total=total, fixed=0)

    # 第一步：内存里用 bak_basic 解析每条的权威名称/代码（resolve 内部已缓存，零额外网络）
    resolved = []
    for (rid, name, code, logic, target) in rows:
        parsed = fallback_parse_research_text(logic or "")
        hint = parsed[0] if parsed else {}
        name_hint = (name or "").strip() or hint.get("stock_name", "")
        code_hint = (code or "").strip() or hint.get("stock_code", "")
        stock = resolve_stock_smart(name_hint=name_hint, code_hint=code_hint, text=logic or "")
        if stock:
            new_code = stock.get("ts_code", "") or code_hint
            new_name = stock.get("name", "") or name_hint
            industry = stock.get("industry", "")
        else:
            new_code, new_name, industry = code_hint, name_hint, ""
        resolved.append({
            "id": rid, "old_name": name or "", "old_code": code or "",
            "name": new_name, "code": new_code, "industry": industry,
        })

    # 第二步：批量拉取行情（2 次 tushare）与题材（1 次 MySQL）
    metrics = fetch_metrics_batch({r["code"] for r in resolved})
    topics = fetch_limit_up_topics_batch(resolved)

    # 第三步：逐条写回（本地 sqlite，很快）
    fixed = 0
    empty_topic = {"topic": "", "concept": "", "limit_up_reason": "", "limit_up_trade_date": ""}
    conn2 = get_db_connection()
    cursor2 = conn2.cursor()
    for idx, item in enumerate(resolved, start=1):
        try:
            mv, pct = metrics.get(item["code"], (None, None))
            topic = topics.get(item["code"]) or topics.get(item["name"]) or empty_topic
            industry = topic.get("industry") or item["industry"]
            cursor2.execute('''
                UPDATE research_board_records
                SET stock_name = ?, stock_code = ?, current_market_value_yi = ?,
                    current_change = ?, topic = ?, industry = ?, concept = ?,
                    limit_up_reason = ?, limit_up_trade_date = ?, update_time = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (
                item["name"], item["code"], mv, pct,
                topic.get("topic", ""), industry, topic.get("concept", ""),
                topic.get("limit_up_reason", ""), topic.get("limit_up_trade_date", ""),
                item["id"],
            ))
            if item["name"] != item["old_name"] or item["code"] != item["old_code"]:
                fixed += 1
        except Exception as e:
            print(f"[记录板] 修复记录 {item['id']} 失败: {e}")
        if task_id and idx % 20 == 0:
            update_research_board_task(task_id, processed=idx, total=total, fixed=fixed)
    conn2.commit()
    conn2.close()
    # 一键修复后整表镜像同步到 MySQL
    sync_local_research_records()
    if task_id:
        update_research_board_task(task_id, processed=total, total=total, fixed=fixed)
    return {"total": total, "fixed": fixed}


# 记录板 MySQL 同步：配置读取统一收进 plugins.mysql_client
# （环境变量 MYSQL_* > config/mysql_sync.json > 根目录旧版 mysql_sync_config.json）。
from plugins.mysql_client import (
    get_sync_config as get_mysql_sync_config,
    sync_enabled as mysql_sync_enabled,
    get_sync_connection as get_sync_mysql_connection,
)


def _ensure_research_sync_table(cursor, table):
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS {table} (
            id INT NOT NULL,
            stock_code VARCHAR(16),
            stock_name VARCHAR(32),
            logic TEXT,
            topic VARCHAR(255),
            concept VARCHAR(255),
            industry VARCHAR(64),
            limit_up_reason VARCHAR(255),
            limit_up_trade_date VARCHAR(16),
            target_market_value_yi DOUBLE,
            current_market_value_yi DOUBLE,
            current_change DOUBLE,
            created_by VARCHAR(64),
            update_time DATETIME,
            sync_time DATETIME,
            PRIMARY KEY (id),
            KEY idx_code (stock_code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记录板研究记录'
    ''')


def sync_research_records_to_mysql(records):
    """把若干条记录板记录幂等 upsert 到 MySQL（按 id）。失败仅记录日志，不影响本地。"""
    if not mysql_sync_enabled() or not records:
        return 0
    cfg = get_mysql_sync_config()
    table = cfg["table"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for r in records:
        rid = r.get("id")
        if rid is None:
            continue
        rows.append((
            rid, r.get("stock_code") or "", r.get("stock_name") or "",
            r.get("logic") or "", r.get("topic") or "", r.get("concept") or "",
            r.get("industry") or "", r.get("limit_up_reason") or "",
            r.get("limit_up_trade_date") or "",
            r.get("target_market_value_yi"), r.get("current_market_value_yi"),
            r.get("current_change"), r.get("created_by") or "", now, now,
        ))
    if not rows:
        return 0
    sql = f'''
        INSERT INTO {table}
          (id, stock_code, stock_name, logic, topic, concept, industry,
           limit_up_reason, limit_up_trade_date, target_market_value_yi,
           current_market_value_yi, current_change, created_by, update_time, sync_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          stock_code=VALUES(stock_code), stock_name=VALUES(stock_name), logic=VALUES(logic),
          topic=VALUES(topic), concept=VALUES(concept), industry=VALUES(industry),
          limit_up_reason=VALUES(limit_up_reason), limit_up_trade_date=VALUES(limit_up_trade_date),
          target_market_value_yi=VALUES(target_market_value_yi),
          current_market_value_yi=VALUES(current_market_value_yi),
          current_change=VALUES(current_change), created_by=VALUES(created_by),
          update_time=VALUES(update_time), sync_time=VALUES(sync_time)
    '''
    try:
        conn = get_sync_mysql_connection()
        with conn.cursor() as cursor:
            _ensure_research_sync_table(cursor, table)
            cursor.executemany(sql, rows)
        conn.commit()
        conn.close()
        return len(rows)
    except Exception as e:
        print(f"[记录板同步] 写入 MySQL 失败: {e}")
        return 0


def delete_research_records_from_mysql(record_ids):
    """从 MySQL 删除指定 id 的记录（本地删除时同步）。失败仅记录日志。"""
    if not mysql_sync_enabled():
        return 0
    ids = [rid for rid in (record_ids or []) if rid is not None]
    if not ids:
        return 0
    cfg = get_mysql_sync_config()
    table = cfg["table"]
    placeholders = ",".join(["%s"] * len(ids))
    try:
        conn = get_sync_mysql_connection()
        with conn.cursor() as cursor:
            _ensure_research_sync_table(cursor, table)
            cursor.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", ids)
        conn.commit()
        conn.close()
        return len(ids)
    except Exception as e:
        print(f"[记录板同步] 删除 MySQL 记录失败: {e}")
        return 0


def sync_local_research_records(record_ids=None):
    """从本地 sqlite 读取记录（record_ids=None 表示全部）并 upsert 到 MySQL。"""
    if not mysql_sync_enabled():
        return 0
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        base = ('SELECT id, stock_name, stock_code, logic, topic, industry, concept, '
                'limit_up_reason, limit_up_trade_date, target_market_value_yi, '
                'current_market_value_yi, current_change, created_by '
                'FROM research_board_records')
        if record_ids is not None:
            ids = [rid for rid in record_ids if rid is not None]
            if not ids:
                conn.close()
                return 0
            placeholders = ",".join(["?"] * len(ids))
            cursor.execute(base + f" WHERE id IN ({placeholders})", ids)
        else:
            cursor.execute(base)
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"[记录板同步] 读取本地记录失败: {e}")
        return 0
    records = [{
        "id": r[0], "stock_name": r[1], "stock_code": r[2], "logic": r[3], "topic": r[4],
        "industry": r[5], "concept": r[6], "limit_up_reason": r[7], "limit_up_trade_date": r[8],
        "target_market_value_yi": r[9], "current_market_value_yi": r[10],
        "current_change": r[11], "created_by": r[12],
    } for r in rows]
    return sync_research_records_to_mysql(records)


def merge_logic_text(old_logic: str, new_logic: str):
    """合并同一只股票的逻辑：追加而不是覆盖；新内容已包含在旧内容中则跳过。"""
    old = (old_logic or "").strip()
    new = (new_logic or "").strip()
    if not new:
        return old
    if not old:
        return new
    if new in old:
        return old
    return old + "；" + new


def parse_and_save_research_board(content: str, username: str):
    parse_source = "fallback"
    llm_raw = ""
    try:
        records, llm_raw = call_llm_for_research_records(content, get_llm_config_dict(mask_key=False))
        if records:
            parse_source = "llm"
    except Exception as e:
        print(f"[记录板] 大模型解析失败，改用规则解析: {e}")
        records = []

    if not records:
        records = fallback_parse_research_text(content)

    enriched = [enrich_research_record(record) for record in normalize_research_records(records)]
    if not enriched:
        raise HTTPException(status_code=400, detail="未解析出有效记录")

    def pick(new_val, old_val):
        return new_val if new_val not in (None, "") else old_val

    sel_cols = ("id, stock_name, stock_code, logic, topic, industry, concept, "
                "limit_up_reason, limit_up_trade_date, target_market_value_yi, "
                "current_market_value_yi, current_change, raw_text")

    conn = get_db_connection()
    cursor = conn.cursor()
    saved = []
    added = 0
    merged = 0
    for item in enriched:
        stock_code = (item.get("stock_code") or "").strip()
        stock_name = (item.get("stock_name") or "").strip()
        # 找记录板里已有的同一只股票：代码优先，其次名称；存在则合并而非新增/覆盖
        existing = None
        if stock_code:
            cursor.execute(f"SELECT {sel_cols} FROM research_board_records "
                           "WHERE stock_code = ? ORDER BY id DESC LIMIT 1", (stock_code,))
            existing = cursor.fetchone()
        if not existing and stock_name:
            cursor.execute(f"SELECT {sel_cols} FROM research_board_records "
                           "WHERE stock_name = ? ORDER BY id DESC LIMIT 1", (stock_name,))
            existing = cursor.fetchone()

        if existing:
            (eid, e_name, e_code, e_logic, e_topic, e_industry, e_concept,
             e_reason, e_lutd, e_target, e_cmv, e_cc, e_raw) = existing
            merged_logic = merge_logic_text(e_logic, item.get("logic", ""))
            merged_raw = ((e_raw or "") + ("\n" + content if content else "")).strip()
            cursor.execute('''
                UPDATE research_board_records SET
                    stock_name = ?, stock_code = ?, logic = ?, topic = ?, industry = ?,
                    concept = ?, limit_up_reason = ?, limit_up_trade_date = ?,
                    target_market_value_yi = ?, current_market_value_yi = ?,
                    current_change = ?, raw_text = ?, update_time = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (
                pick(stock_name, e_name),
                pick(stock_code, e_code),
                merged_logic,
                pick(item.get("topic", ""), e_topic),
                pick(item.get("industry", ""), e_industry),
                pick(item.get("concept", ""), e_concept),
                pick(item.get("limit_up_reason", ""), e_reason),
                pick(item.get("limit_up_trade_date", ""), e_lutd),
                pick(item.get("target_market_value_yi"), e_target),
                pick(item.get("current_market_value_yi"), e_cmv),
                pick(item.get("current_change"), e_cc),
                merged_raw,
                eid,
            ))
            saved.append({**item, "id": eid, "logic": merged_logic, "merged": True})
            merged += 1
        else:
            cursor.execute('''
                INSERT INTO research_board_records (
                    stock_name, stock_code, logic, topic, industry, concept,
                    limit_up_reason, limit_up_trade_date, target_market_value_yi,
                    current_market_value_yi, current_change, raw_text, created_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                item.get("stock_name", ""),
                item.get("stock_code", ""),
                item.get("logic", ""),
                item.get("topic", ""),
                item.get("industry", ""),
                item.get("concept", ""),
                item.get("limit_up_reason", ""),
                item.get("limit_up_trade_date", ""),
                item.get("target_market_value_yi"),
                item.get("current_market_value_yi"),
                item.get("current_change"),
                content,
                username,
            ))
            saved.append({**item, "id": cursor.lastrowid, "merged": False})
            added += 1
    conn.commit()
    conn.close()
    return {"status": "success", "source": parse_source, "records": saved,
            "count": len(saved), "added": added, "merged": merged, "llm_raw": llm_raw}


def run_research_board_parse_task(task_id: str, content: str, username: str):
    input_id = create_research_board_input(task_id, content, username)
    try:
        result = parse_and_save_research_board(content, username)
        records = result.get("records", [])
        update_research_board_task(
            task_id,
            status="completed",
            source=result.get("source", ""),
            records=records,
            count=result.get("count", 0),
            added=result.get("added", 0),
            merged=result.get("merged", 0),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        update_research_board_input(
            input_id,
            status="completed",
            parse_source=result.get("source", ""),
            llm_raw_response=result.get("llm_raw", ""),
            parsed_records_json=json.dumps(records, ensure_ascii=False, default=str),
            record_ids=",".join(str(r.get("id")) for r in records if r.get("id")),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        # 同步解析/合并后的记录到 MySQL
        sync_local_research_records([r.get("id") for r in records])
    except Exception as e:
        update_research_board_task(
            task_id,
            status="failed",
            error=str(e),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        update_research_board_input(
            input_id,
            status="failed",
            error=str(e),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )


@app.get("/api/research-board/tasks/{task_id}")
async def get_research_board_task_status(task_id: str, current_user: dict = Depends(get_current_user)):
    task = get_research_board_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.get("/api/research-board/inputs")
async def list_research_board_inputs(limit: int = 50, current_user: dict = Depends(get_current_user)):
    """查看最近归档的记录板原始输入及解析详情，便于复核解析效果。"""
    limit = max(1, min(int(limit or 50), 200))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, task_id, content, created_by, parse_source, llm_raw_response,
               parsed_records_json, record_ids, error, status, created_at, finished_at
        FROM research_board_inputs
        ORDER BY id DESC
        LIMIT ?
    ''', (limit,))
    rows = cursor.fetchall()
    conn.close()
    inputs = []
    for row in rows:
        inputs.append({
            "id": row[0],
            "task_id": row[1],
            "content": row[2],
            "created_by": row[3],
            "parse_source": row[4],
            "llm_raw_response": row[5],
            "parsed_records_json": row[6],
            "record_ids": row[7],
            "error": row[8],
            "status": row[9],
            "created_at": row[10],
            "finished_at": row[11],
        })
    return {"status": "success", "inputs": inputs}


@app.put("/api/research-board/{record_id}")
async def update_research_board_record(record_id: int, data: ResearchBoardRecordEdit,
                                       current_user: dict = Depends(get_current_user)):
    topic = (data.topic or "").strip()
    if not topic:
        topic_parts = [part.strip() for part in [data.concept, data.limit_up_reason] if part and part.strip()]
        topic = " / ".join(topic_parts)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE research_board_records
        SET stock_name = ?,
            stock_code = ?,
            logic = ?,
            target_market_value_yi = ?,
            topic = ?,
            industry = ?,
            concept = ?,
            limit_up_reason = ?,
            update_time = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (
        data.stock_name.strip(),
        data.stock_code.strip(),
        data.logic.strip(),
        data.target_market_value_yi,
        topic,
        data.industry.strip(),
        data.concept.strip(),
        data.limit_up_reason.strip(),
        record_id,
    ))
    updated = cursor.rowcount
    conn.commit()
    conn.close()
    if updated == 0:
        raise HTTPException(status_code=404, detail="记录不存在")
    sync_local_research_records([record_id])
    return {"status": "success", "message": "记录已更新"}


@app.delete("/api/research-board/{record_id}")
async def delete_research_board_record(record_id: int, current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM research_board_records WHERE id = ?", (record_id,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="记录不存在")
    delete_research_records_from_mysql([record_id])
    return {"status": "success", "message": "记录已删除"}


@app.post("/api/research-board/{record_id}/kline-analysis")
async def analyze_research_board_kline(record_id: int, current_user: dict = Depends(get_current_user)):
    """让大模型分析该记录对应股票最近 60 个交易日的 K 线走势。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, stock_name, stock_code, logic, topic, concept, limit_up_reason,
               target_market_value_yi, current_market_value_yi
        FROM research_board_records WHERE id = ?
    ''', (record_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    record = {
        "id": row[0], "stock_name": row[1], "stock_code": row[2], "logic": row[3],
        "topic": row[4], "concept": row[5], "limit_up_reason": row[6],
        "target_market_value_yi": row[7], "current_market_value_yi": row[8],
    }
    if not (record.get("stock_code") or "").strip():
        raise HTTPException(status_code=400, detail="该记录缺少股票代码，无法获取K线")
    # 取数与大模型调用是阻塞操作，放到线程池里跑，避免阻塞事件循环
    result = await asyncio.to_thread(analyze_research_record_kline, record)
    result["stock_name"] = record.get("stock_name")
    result["stock_code"] = record.get("stock_code")
    return result


class StrategyConfigUpdate(BaseModel):
    account_id: str = ""
    content: str


@app.get("/api/config/strategy")
async def read_strategy_ini(
    account_id: str = Query("", description="用户账号ID"),
    current_user: dict = Depends(get_current_admin)
):
    """读取某账号最近一次下发的 strategy.ini 内容"""
    global _LAST_STRATEGY_INI_CONTENT
    conn = get_db_connection()
    cursor = conn.cursor()

    if account_id:
        cursor.execute('''
            SELECT config_content FROM strategy_configs
            WHERE account_id = ?
        ''', (account_id,))
    else:
        cursor.execute('''
            SELECT account_id, config_content FROM strategy_configs
            ORDER BY update_time DESC LIMIT 1
        ''')

    row = cursor.fetchone()
    conn.close()

    if row:
        if account_id:
            _LAST_STRATEGY_INI_CONTENT[account_id] = row[0]
            return {"status": "success", "content": row[0], "account_id": account_id}
        else:
            # 返回最新一条 (任意账号)
            _LAST_STRATEGY_INI_CONTENT[row[0]] = row[1]
            return {"status": "success", "content": row[1], "account_id": row[0]}

    cached = _LAST_STRATEGY_INI_CONTENT.get(account_id) if account_id else None
    if cached:
        return {"status": "success", "content": cached, "account_id": account_id}

    return {"status": "empty", "content": "", "account_id": account_id,
            "note": "尚无配置记录, 等待 QMT 启动后自动推送, 或粘贴 strategy.ini 内容后保存下发"}


class StrategyConfigWrite(BaseModel):
    account_id: str
    content: str


@app.post("/api/config/strategy")
async def write_strategy_ini(update: StrategyConfigWrite,
                             current_user: dict = Depends(get_current_admin)):
    """保存某账号的策略参数（strategy.ini）。

    改造前这是「远程管理」：把内容塞进 account_commands，等 QMT 客户端取走写到本地
    再热加载 30 秒生效。策略搬到服务端之后没有远端可下发了 —— 这就是一份服务端配置，
    存进 strategy_configs 表，读写都在本地。INI 校验保留。
    """
    global _LAST_STRATEGY_INI_CONTENT

    if not update.account_id:
        return {"status": "error", "message": "请指定目标账号"}

    try:
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read_string(update.content)
        if not cfg.sections():
            return {"status": "error", "message": "INI 格式错误: 缺少有效的配置节 (如 [交易限制])"}
    except configparser.Error as e:
        return {"status": "error", "message": f"INI 语法错误: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"配置解析异常: {e}"}

    if update.content == _LAST_STRATEGY_INI_CONTENT.get(update.account_id):
        return {"status": "skipped", "message": f"账号 {update.account_id} 内容未变更"}

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO strategy_configs (account_id, config_content)
        VALUES (?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            config_content = excluded.config_content,
            update_time = CURRENT_TIMESTAMP
    ''', (update.account_id, update.content))
    conn.commit()
    conn.close()

    _LAST_STRATEGY_INI_CONTENT[update.account_id] = update.content
    return {"status": "success", "message": f"账号 {update.account_id} 的策略配置已保存"}


# ============================================================================
# 直连之后才有的视图：活动委托、下单审计、连接状态、品种规则
# ============================================================================

@app.get("/api/orders")
async def get_orders_endpoint(
    account_id: str = Query("", description="账号ID，留空或 all 表示全部"),
    active_only: bool = Query(True, description="只看未成交/部成的挂单"),
    current_user: dict = Depends(get_current_user)
):
    """活动委托列表。

    改造前服务端只看得到成交，「挂着没成的单」是盲区 —— 卖出指令下发后到底报没报进去、
    有没有被废单，页面上完全看不出来。直连之后这些都在 orders 表里。
    """
    if current_user["role"] != "admin":
        account_id = current_user["account_id"]

    # 未成交状态：48未报 49待报 50已报 51已报待撤 52部成待撤 55部成
    active_states = (48, 49, 50, 51, 52, 55)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        sql = '''SELECT account_id, order_id, order_sysid, stock_code, instrument_name,
                        order_type, order_status, order_volume, traded_volume, price,
                        traded_price, order_time, strategy_name, order_remark, status_msg
                 FROM orders WHERE 1 = 1'''
        params = []
        if account_id and account_id != "all":
            sql += " AND account_id = ?"
            params.append(account_id)
        if active_only:
            sql += " AND order_status IN (%s)" % ",".join("?" * len(active_states))
            params.extend(active_states)
        sql += " ORDER BY order_time DESC LIMIT 500"
        cursor.execute(sql, params)
        columns = ["account_id", "order_id", "order_sysid", "stock_code", "instrument_name",
                   "order_type", "order_status", "order_volume", "traded_volume", "price",
                   "traded_price", "order_time", "strategy_name", "order_remark", "status_msg"]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()

    for row in rows:
        row["side"] = "buy" if row["order_type"] == 23 else "sell"
        row["unit"] = instruments.unit_name(row["stock_code"])
    return {"status": "success", "orders": rows, "count": len(rows)}


class CancelOrderRequest(BaseModel):
    account_id: str
    order_id: str = ""
    stock_code: str = ""


@app.post("/api/orders/cancel")
async def cancel_order_endpoint(
    payload: CancelOrderRequest,
    current_user: dict = Depends(get_current_user)
):
    """撤单：给 order_id 就撤那一笔，只给 stock_code 就撤该票所有可撤单。"""
    is_admin = current_user["role"] == "admin"
    if not is_admin and payload.account_id != current_user["account_id"]:
        raise HTTPException(status_code=403, detail="只能操作自己的账号")

    operator = current_user.get("username") or current_user.get("account_id") or ""
    if payload.order_id:
        result = bridge_orders.cancel_order(
            payload.account_id, order_id=payload.order_id, operator=operator)
        if result.get("ok"):
            return {"status": "success", "message": result["message"]}
        raise HTTPException(status_code=400, detail=result.get("message") or "撤单失败")

    results = cancel_open_orders(payload.account_id, payload.stock_code or None,
                                None, operator)
    if not results:
        return {"status": "success", "message": "没有可撤的委托",
                "succeeded": 0, "failed": 0, "details": []}
    return batch_order_response(results, "撤单")


@app.get("/api/order-audit")
async def get_order_audit_endpoint(
    account_id: str = Query("", description="账号ID，留空表示全部"),
    limit: int = Query(200, ge=1, le=1000),
    current_user: dict = Depends(get_current_user)
):
    """下单审计流水：谁、什么时候、下了什么、成没成、失败原因。

    改造前下单失败发生在 QMT 客户端里，服务端根本不知道。
    """
    if current_user["role"] != "admin":
        account_id = current_user["account_id"]
    return {"status": "success", "records": get_order_audit(account_id or None, limit)}


@app.get("/api/accounts/health")
async def get_accounts_health(current_user: dict = Depends(get_current_admin)):
    """各账号的大QMT 连接状态：在线、往返延迟、最后一次同步结果。"""
    accounts = []
    for cfg in bridge_config.list_accounts(enabled_only=False):
        info = cfg.describe()
        info.update(account_liveness(cfg.account_id))
        info["sync"] = sync_poller.last_state(cfg.account_id) or {}
        accounts.append(info)
    return {
        "status": "success",
        "bridge_installed": bridge_pool.compat_available(),
        "quote_account_id": bridge_config.quote_account_id(),
        "accounts": accounts,
        "callback_stats": sync_callbacks.stats(),
    }


@app.get("/api/instrument/{stock_code}")
async def get_instrument_spec(
    stock_code: str,
    cash_amount: float = Query(0, ge=0, description="给定金额时，换算出可申报数量"),
    volume: int = Query(0, ge=0, description="给定数量时，估算下单金额"),
    current_user: dict = Depends(get_current_user)
):
    """品种规则 + 实时价 + 数量/金额互算。

    前端下单弹窗按这个切换步进和小数位（股票 100 股 0.01，可转债 10 张 0.001），
    并显示「预计下单金额」。换算放服务端做，保证和真正报单时的规整结果一致。
    """
    spec = instruments.describe(stock_code)
    spec["kind_name"] = {
        instruments.KIND_STOCK: "股票", instruments.KIND_STAR: "科创板",
        instruments.KIND_BJ: "北交所", instruments.KIND_ETF: "ETF/LOF",
        instruments.KIND_BOND: "可转债",
    }.get(spec["kind"], "股票")

    price = bridge_market.last_price(stock_code)
    spec["last_price"] = price
    spec["price_source"] = "bridge" if price else ""

    # 数量 -> 预计金额
    if volume > 0:
        adjusted = instruments.round_volume(stock_code, volume)
        spec["volume"] = adjusted
        spec["volume_adjusted"] = adjusted != volume
        spec["estimated_cash"] = round(adjusted * price, 2) if price else None

    # 金额 -> 可申报数量
    if cash_amount > 0:
        resolved, used_price, error = volume_from_cash(stock_code, cash_amount, price)
        spec["cash_amount"] = cash_amount
        spec["volume_for_cash"] = resolved
        spec["estimated_cash"] = round(resolved * used_price, 2) if resolved and used_price else None
        spec["cash_error"] = error

    return {"status": "success", "instrument": spec}


# ============================================================================
# 可转债
# ============================================================================

@app.get("/api/cb/{bond_code}")
async def get_bond_detail(bond_code: str,
                          current_user: dict = Depends(get_current_user)):
    """单只转债：转股价、转股价值、溢价率、双低、强赎进度。

    算不出来的项返回 null 并在 data_gap 里说明原因 —— 宁可空着，也不要拿缺失的
    转股价凑一个看起来像模像样的溢价率出来。
    """
    if not instruments.is_convertible_bond(bond_code):
        raise HTTPException(status_code=400, detail="%s 不是可转债" % bond_code)
    return {"status": "success", "bond": cb_service.bond_view(bond_code)}


@app.get("/api/cb")
async def list_bond_reference(
    q: str = Query("", description="按代码或名称过滤"),
    limit: int = Query(200, ge=1, le=2000),
    current_user: dict = Depends(get_current_user)
):
    """转债参考数据列表（转股价、正股、发行规模、评级）。"""
    keyword = (q or "").strip().lower()
    rows = []
    for row in cb_reference.load_all().values():
        if keyword and keyword not in row["bond_code"].lower() \
                and keyword not in (row.get("bond_name") or "").lower() \
                and keyword not in (row.get("stock_name") or "").lower():
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return {
        "status": "success",
        "bonds": rows,
        "coverage": cb_reference.coverage(),
    }


@app.post("/api/cb/refresh")
async def refresh_bond_reference(current_user: dict = Depends(get_current_admin)):
    """手动刷新转债参考数据（一览表 + 转股价补缺）。"""
    result = cb_reference.refresh_all()
    result["coverage"] = cb_reference.coverage()
    return {"status": "success", **result}


@app.get("/api/cb-ipo/pending")
async def get_bond_ipo_pending(current_user: dict = Depends(get_current_user)):
    """今日可申购的新债。"""
    return {"status": "success", "candidates": cb_reference.pending_applications()}


@app.post("/api/cb-ipo/subscribe")
async def subscribe_bond_ipo(data: dict, current_user: dict = Depends(get_current_admin)):
    """手动触发打新债。默认 dry-run，要真申购必须显式传 dry_run=false。"""
    account_id = (data or {}).get("account_id") or ""
    dry_run = (data or {}).get("dry_run")
    dry_run = True if dry_run is None else bool(dry_run)
    if account_id:
        return {"status": "success", "results": [cb_ipo.subscribe(account_id, dry_run=dry_run)]}
    return {"status": "success", "results": cb_ipo.run_once(force=True)}


# ============================================================================
# 大QMT 直连装配：风控闸门 + 同步落库钩子
# ============================================================================
# 改造前 stop_buy / stop_sell / 锁仓 / 仓位系数都只是「下发给 QMT 客户端的建议值」，
# 客户端听不听服务端管不着。直连之后所有单子都从服务端发出，这些开关第一次真正成了
# 闸门 —— 判定不通过就根本不会有报单。

def is_trading_session():
    """当前是否处于交易时段（交易日 09:15-15:05）。轮询频率据此切换。"""
    now = datetime.now()
    if not is_trade_date(now.strftime('%Y-%m-%d')):
        return False
    hm = now.hour * 100 + now.minute
    return 915 <= hm <= 1505


def order_risk_gate(request):
    """下单前的服务端风控。不放行就抛 OrderRejected。

    request 来自 bridge.orders.place_order，含 account_id / stock_code / side /
    volume / price 等已规整好的字段。
    """
    account_id = request.get("account_id")
    side = request.get("side")
    stock_code = request.get("stock_code")
    bypass = set(request.get("bypass") or ())

    # 展示模式：整个部署只读，不许有任何报单。这一条不接受 bypass。
    if is_display_mode():
        raise bridge_orders.OrderRejected("当前为展示模式，服务端不下发任何交易指令")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT is_stopped, buy_stopped, sell_stopped FROM trading_status WHERE account_id = ?',
            (account_id,))
        row = cursor.fetchone()
        if row:
            if row[0]:
                raise bridge_orders.OrderRejected("账号 %s 已停止交易" % account_id)
            # 手动买入历史上就是忽略「停止买入」的（老客户端的 ignore_stop_buy），
            # 保持这个行为，但绕过动作会写进审计流水。
            if side == bridge_orders.SIDE_BUY and row[1] and "stop_buy" not in bypass:
                raise bridge_orders.OrderRejected("账号 %s 已停止买入" % account_id)
            if side == bridge_orders.SIDE_SELL and row[2] and "stop_sell" not in bypass:
                raise bridge_orders.OrderRejected("账号 %s 已停止卖出" % account_id)

        # 已触发强赎的转债不许再新买：强赎公告后会按 100 元附近赎回，
        # 此时带着溢价买进去是确定性亏损。数据不足时不拦（redeem_blocked 返回 False）。
        if side == bridge_orders.SIDE_BUY and "redeem" not in bypass:
            try:
                if cb_service.redeem_blocked(stock_code):
                    raise bridge_orders.OrderRejected(
                        "%s 已触发强赎条件，禁止新开仓（如确需买入请走强制买入）" % stock_code)
            except bridge_orders.OrderRejected:
                raise
            except Exception as e:
                print(f"[风控] 强赎判定失败，放行 {stock_code}: {e}")

        # 锁仓是绝对的：要卖必须先在面板上解锁。改造前这只是给客户端的一个列表。
        if side == bridge_orders.SIDE_SELL:
            cursor.execute(
                'SELECT 1 FROM position_locks WHERE account_id = ? AND stock_code = ? AND is_locked = 1',
                (account_id, stock_code))
            if cursor.fetchone():
                raise bridge_orders.OrderRejected(
                    "%s 已锁仓，需先解锁才能卖出" % stock_code)
    finally:
        conn.close()


def position_factor_of(account_id):
    """账号的仓位系数，下单算量时用。读不到按 1.0。"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COALESCE(position_factor, 1.0) FROM users WHERE account_id = ?',
                       (account_id,))
        row = cursor.fetchone()
        conn.close()
        return _safe_db_trade_factor(row[0]) if row else 1.0
    except Exception:
        return 1.0


dbaccess.register(get_db_connection)
bridge_orders.register_risk_gate(order_risk_gate)
bridge_orders.register_audit_sink(save_order_audit)
sync_sinks.register(
    save_positions=save_positions,
    save_asset=save_asset,
    save_trades=save_trades,
    save_orders=save_orders,
    is_trading_session=is_trading_session,
)


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000)
