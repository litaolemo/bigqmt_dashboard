"""MySQL 可选插件：股票基础库（只读）+ 记录板同步库。

原来两套配置的处理方式不一致：
  * 股票基础库 STOCK_DB_CONFIG 直接把账号密码硬编码在 app.py 里；
  * 记录板同步库已经是「环境变量优先 + gitignored JSON」，但读配置的代码
    自己抄了一遍缓存逻辑。
这里统一成同一套，都走 settings，缺配置就报不可用，由调用方降级。

两个库账号不同（只读账号 vs 写账号），所以配置分开，环境变量前缀也分开：
  STOCK_DB_*  → 股票基础库    MYSQL_*  → 记录板同步库
"""

import os
import threading

import settings

_STOCK_CACHE = {"loaded": False, "config": {}}
_SYNC_CACHE = {"loaded": False, "config": {}}
_LOCK = threading.Lock()

# 兼容老部署：改造前这个文件放在仓库根目录
_LEGACY_SYNC_FILE = os.path.join(settings.BASE_DIR, "mysql_sync_config.json")


class MySQLUnavailable(RuntimeError):
    """没配 MySQL。调用方按“该数据源不可用”处理。"""


def get_stock_db_config():
    """股票基础库（bak_basic / etf_type_snapshot）的只读连接配置。"""
    with _LOCK:
        if _STOCK_CACHE["loaded"]:
            return _STOCK_CACHE["config"]
    file_cfg = settings.load_json("mysql")
    config = {
        "host": settings.env_str("STOCK_DB_HOST") or str(file_cfg.get("host") or ""),
        "port": settings.env_int("STOCK_DB_PORT", 0) or int(file_cfg.get("port") or 3306),
        "user": settings.env_str("STOCK_DB_USER") or str(file_cfg.get("user") or ""),
        "password": settings.env_str("STOCK_DB_PASSWORD") or str(file_cfg.get("password") or ""),
        "database": settings.env_str("STOCK_DB_NAME") or str(file_cfg.get("database") or ""),
        "charset": str(file_cfg.get("charset") or "utf8"),
    }
    with _LOCK:
        _STOCK_CACHE.update({"loaded": True, "config": config})
    if not stock_db_available():
        print("[MySQL] 未配置股票基础库（config/mysql.json 或 STOCK_DB_* 环境变量），"
              "下单搜索将只能用本地缓存")
    return config


def stock_db_available():
    c = get_stock_db_config()
    return bool(c.get("host") and c.get("user") and c.get("database"))


def get_stock_db_connection():
    """股票基础库连接；未配置时抛 MySQLUnavailable。"""
    c = get_stock_db_config()
    if not stock_db_available():
        raise MySQLUnavailable("未配置股票基础库，跳过 MySQL 查询")
    import pymysql
    return pymysql.connect(
        host=c["host"], port=c["port"], user=c["user"], password=c["password"],
        database=c["database"], charset=c["charset"], connect_timeout=15,
    )


def get_sync_config():
    """记录板 MySQL 同步配置：环境变量 > config/mysql_sync.json > 根目录旧文件。"""
    with _LOCK:
        if _SYNC_CACHE["loaded"]:
            return _SYNC_CACHE["config"]
    file_cfg = settings.load_json("mysql_sync")
    if not file_cfg and os.path.exists(_LEGACY_SYNC_FILE):
        try:
            import json
            with open(_LEGACY_SYNC_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f) or {}
        except Exception as e:
            print(f"[记录板同步] 读取旧版 mysql_sync_config.json 失败: {e}")
            file_cfg = {}
    config = {
        "enabled": file_cfg.get("enabled", True),
        "host": settings.env_str("MYSQL_HOST") or str(file_cfg.get("host") or ""),
        "port": settings.env_int("MYSQL_PORT", 0) or int(file_cfg.get("port") or 3306),
        "user": settings.env_str("MYSQL_USER") or str(file_cfg.get("user") or ""),
        "password": settings.env_str("MYSQL_PASSWORD") or str(file_cfg.get("password") or ""),
        "database": settings.env_str("MYSQL_DB") or str(file_cfg.get("database") or ""),
        "table": str(file_cfg.get("table") or "research_board_records"),
    }
    with _LOCK:
        _SYNC_CACHE.update({"loaded": True, "config": config})
    return config


def sync_enabled():
    c = get_sync_config()
    return bool(c.get("enabled") and c.get("host") and c.get("user") and c.get("database"))


def get_sync_connection():
    """记录板同步库连接；未启用时抛 MySQLUnavailable。"""
    c = get_sync_config()
    if not sync_enabled():
        raise MySQLUnavailable("记录板 MySQL 同步未启用")
    import pymysql
    return pymysql.connect(
        host=c["host"], port=c["port"], user=c["user"], password=c["password"],
        database=c["database"], charset="utf8mb4", connect_timeout=15,
    )


def reset_cache():
    """测试用。"""
    with _LOCK:
        _STOCK_CACHE.update({"loaded": False, "config": {}})
        _SYNC_CACHE.update({"loaded": False, "config": {}})
