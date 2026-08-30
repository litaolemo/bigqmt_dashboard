"""打新债（可转债网上申购）。

大QMT 侧有 ipo_subscribe_all，桥接层直接透传。新债申购基本是无风险收益，
适合每天开盘后自动打一次，所以做成定时任务。

**默认 dry-run。** 真申购要在 config/accounts.json 里显式打开，而且账号本身
也得 allow_order —— 自动下单这种事不该靠一个默认值就生效。
"""

import threading
from datetime import datetime

from bridge import config as bridge_config
from bridge import pool as bridge_pool
from cb import reference

_LAST_RUN = {}      # account_id -> 'YYYY-MM-DD'
_LOCK = threading.RLock()


def enabled_for(account_id):
    """该账号是否开启了自动打新债。"""
    cfg = bridge_config.get_account(account_id)
    if cfg is None or not cfg.enabled:
        return False
    return bool(cfg.rpc.get("ipo_subscribe") or getattr(cfg, "ipo_subscribe", False))


def dry_run_for(account_id):
    """是否 dry-run。没显式关掉就是 dry-run。"""
    cfg = bridge_config.get_account(account_id)
    if cfg is None:
        return True
    return not bool(cfg.rpc.get("ipo_live"))


def subscribe(account_id, dry_run=None, stock_type="BOND"):
    """给一个账号打新债。返回结果 dict，绝不抛异常。"""
    dry_run = dry_run_for(account_id) if dry_run is None else dry_run
    pending = reference.pending_applications()
    result = {
        "account_id": account_id,
        "dry_run": dry_run,
        "candidates": pending,
        "ok": False,
        "message": "",
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    if not pending:
        result["ok"] = True
        result["message"] = "今日没有可申购的新债"
        return result
    if dry_run:
        result["ok"] = True
        result["message"] = "dry-run：本应申购 %d 只（%s）" % (
            len(pending), "、".join(p["bond_name"] for p in pending))
        return result
    if not bridge_pool.allow_order(account_id):
        result["message"] = "账号 %s 未开启下单，跳过申购" % account_id
        return result

    try:
        trader = bridge_pool.get_trader(account_id)
        acc = bridge_pool.get_account_ref(account_id)
        response = trader.ipo_subscribe_all(acc, stock_type=stock_type)
        result["ok"] = True
        result["response"] = response
        result["message"] = "已提交申购 %d 只" % len(pending)
    except bridge_pool.BridgeUnavailable as e:
        result["message"] = str(e)
    except Exception as e:
        result["message"] = "%s: %s" % (type(e).__name__, e)
    return result


def run_once(force=False):
    """给所有开启了自动打新的账号各打一次，每天最多一次。"""
    today = datetime.now().strftime("%Y-%m-%d")
    results = []
    for cfg in bridge_config.list_accounts():
        account_id = cfg.account_id
        if not enabled_for(account_id):
            continue
        with _LOCK:
            if not force and _LAST_RUN.get(account_id) == today:
                continue
            _LAST_RUN[account_id] = today
        outcome = subscribe(account_id)
        results.append(outcome)
        print("[打新债] %s: %s" % (account_id, outcome["message"]))
    return results


def reset_state():
    with _LOCK:
        _LAST_RUN.clear()
