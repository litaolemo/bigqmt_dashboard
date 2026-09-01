"""每账号的大QMT 连接配置。

来源优先级：config/accounts.json > BIGQMT_* 环境变量（单账号快速起步）。

配置里的 rpc 段整包透传给 BigQmtRpcClient 的 redis_config —— 桥接层就是拿这一个
dict 承载所有传输参数的（host/port/db/password，以及嵌套的 transport / zmq /
mysql / formula_server），所以换 ZMQ、换 MySQL 传输都只改配置不改代码。
"""

import threading

import settings

DEFAULT_TIMEOUT_SECONDS = 6.0
DEFAULT_POLL_SECONDS = 4.0          # 交易时段
DEFAULT_IDLE_POLL_SECONDS = 60.0    # 非交易时段

_CACHE = {"loaded": False, "accounts": {}, "quote_account_id": ""}
_LOCK = threading.Lock()


class AccountConfig:
    """一个交易账号对应的大QMT 连接参数。"""

    __slots__ = ("account_id", "alias", "enabled", "account_type", "allow_order",
                 "timeout_seconds", "poll_seconds", "idle_poll_seconds", "rpc")

    def __init__(self, raw):
        self.account_id = str(raw.get("account_id") or "").strip()
        self.alias = str(raw.get("alias") or "").strip() or self.account_id
        self.enabled = bool(raw.get("enabled", True))
        self.account_type = str(raw.get("account_type") or "STOCK").strip().upper()
        # 下单默认关闭：接进来先跑只读链路，对完账再显式打开。
        # 注意大QMT 侧还有一道 rpc_allow_order_methods，两边都开才真的能下单。
        self.allow_order = bool(raw.get("allow_order", False))
        self.timeout_seconds = float(raw.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        self.poll_seconds = float(raw.get("poll_seconds") or DEFAULT_POLL_SECONDS)
        self.idle_poll_seconds = float(raw.get("idle_poll_seconds") or DEFAULT_IDLE_POLL_SECONDS)
        self.rpc = dict(raw.get("rpc") or {})

    @property
    def transport(self):
        return str(self.rpc.get("transport") or "redis").lower()

    def describe(self):
        """给日志/接口用的脱敏摘要，绝不带 password。"""
        return {
            "account_id": self.account_id,
            "alias": self.alias,
            "enabled": self.enabled,
            "account_type": self.account_type,
            "allow_order": self.allow_order,
            "transport": self.transport,
            "host": self.rpc.get("host", ""),
            "port": self.rpc.get("port", ""),
            "db": self.rpc.get("db", ""),
        }

    def __repr__(self):
        return "<AccountConfig %s transport=%s>" % (self.account_id, self.transport)


def _from_env():
    """没有 accounts.json 时，用 BIGQMT_* 环境变量兜出一个单账号配置。"""
    account_id = settings.env_str("BIGQMT_ACCOUNT_ID")
    if not account_id:
        return {}
    rpc = {
        "transport": settings.env_str("BIGQMT_RPC_TRANSPORT", "redis"),
        "host": settings.env_str("BIGQMT_REDIS_HOST", "127.0.0.1"),
        "port": settings.env_int("BIGQMT_REDIS_PORT", 6379),
        "db": settings.env_int("BIGQMT_REDIS_DB", 5),
        "username": settings.env_str("BIGQMT_REDIS_USERNAME"),
        "password": settings.env_str("BIGQMT_REDIS_PASSWORD"),
    }
    return {
        "accounts": [{
            "account_id": account_id,
            "allow_order": settings.env_bool("BIGQMT_ALLOW_ORDER", False),
            "timeout_seconds": settings.env_float("BIGQMT_RPC_TIMEOUT_SECONDS",
                                                  DEFAULT_TIMEOUT_SECONDS),
            "rpc": rpc,
        }],
    }


def _load():
    raw = settings.load_json("accounts")
    source = "config/accounts.json"
    if not raw or not raw.get("accounts"):
        raw = _from_env()
        source = "BIGQMT_* 环境变量"
    accounts = {}
    for item in (raw.get("accounts") or []):
        try:
            cfg = AccountConfig(item)
        except Exception as e:
            print(f"[bridge] 跳过一条无法解析的账号配置: {e}")
            continue
        if not cfg.account_id:
            print("[bridge] 跳过缺少 account_id 的账号配置")
            continue
        if cfg.account_id in accounts:
            print(f"[bridge] 账号 {cfg.account_id} 配置重复，后一条覆盖前一条")
        accounts[cfg.account_id] = cfg
    if accounts:
        print(f"[bridge] 从 {source} 载入 {len(accounts)} 个账号: "
              f"{', '.join(sorted(accounts))}")
    else:
        print("[bridge] 未配置任何大QMT 账号（config/accounts.json 或 BIGQMT_ACCOUNT_ID），"
              "直连功能不可用，面板只读历史数据")
    return accounts, str(raw.get("quote_account_id") or "").strip()


def _ensure_loaded():
    with _LOCK:
        if not _CACHE["loaded"]:
            accounts, quote_id = _load()
            _CACHE.update({"loaded": True, "accounts": accounts, "quote_account_id": quote_id})
        return _CACHE


def list_accounts(enabled_only=True):
    cache = _ensure_loaded()
    items = list(cache["accounts"].values())
    return [a for a in items if a.enabled] if enabled_only else items


def account_ids(enabled_only=True):
    return [a.account_id for a in list_accounts(enabled_only)]


def get_account(account_id):
    """返回 AccountConfig；未配置该账号返回 None。"""
    return _ensure_loaded()["accounts"].get(str(account_id or "").strip())


def is_configured(account_id):
    cfg = get_account(account_id)
    return bool(cfg and cfg.enabled)


def quote_account_id():
    """行情主连接用哪个账号。留空则取第一个启用账号。

    行情与账号无关，让 N 个账号各自订阅一遍全市场纯属浪费，固定挑一条连接。
    """
    cache = _ensure_loaded()
    explicit = cache["quote_account_id"]
    if explicit and explicit in cache["accounts"]:
        return explicit
    enabled = [a.account_id for a in cache["accounts"].values() if a.enabled]
    return enabled[0] if enabled else ""


def reset_cache():
    """测试用。"""
    with _LOCK:
        _CACHE.update({"loaded": False, "accounts": {}, "quote_account_id": ""})
    settings.reset_cache("accounts")
