"""可转债参考数据：转股价、正股、发行规模、申购信息。

数据源 akshare bond_zh_cov（东财），每天更新一次落进 SQLite 的 cb_reference 表。
akshare 拿不到时用库里的旧快照 —— 转股价不是天天变，隔夜数据依然可用，
比整个转债面板空掉强得多。

这里只存「基本不变的参考量」。溢价率这类要跟盘的东西不落库，由 cb.metrics
拿实时价现算（akshare 那几列是快照，盘中就过期了）。
"""

import threading
from datetime import datetime, timedelta

import dbaccess

_CACHE = {"loaded_at": None, "by_bond": {}}
_LOCK = threading.RLock()
CACHE_TTL_SECONDS = 6 * 3600


def ensure_table():
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS cb_reference (
            bond_code TEXT PRIMARY KEY,
            bond_name TEXT,
            stock_code TEXT,
            stock_name TEXT,
            conversion_price REAL,
            issue_size REAL,
            list_date TEXT,
            rating TEXT,
            apply_date TEXT,
            apply_code TEXT,
            apply_limit REAL,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cb_reference_stock '
                       'ON cb_reference (stock_code)')
        conn.commit()
    finally:
        conn.close()


def _normalize(code, prefer=None):
    """转债/正股代码统一成带后缀形式。转债沪深靠代码段分，不能按 5/6 开头猜。"""
    from bridge import instruments
    return instruments.normalize_code(code)


def refresh_from_akshare():
    """从 akshare 拉一遍全市场转债参考数据，写进 cb_reference。返回写入条数。"""
    try:
        import akshare as ak
        df = ak.bond_zh_cov()
    except Exception as e:
        print(f"[转债] akshare bond_zh_cov 拉取失败，沿用库里的旧数据: {e}")
        return 0
    if df is None or df.empty:
        return 0

    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    written = 0
    try:
        for record in df.to_dict("records"):
            bond_code = _normalize(str(record.get("债券代码") or "").strip())
            if not bond_code:
                continue
            cursor.execute('''
            INSERT INTO cb_reference (
                bond_code, bond_name, stock_code, stock_name, conversion_price,
                issue_size, list_date, rating, apply_date, apply_code, apply_limit,
                update_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(bond_code) DO UPDATE SET
                bond_name        = excluded.bond_name,
                stock_code       = excluded.stock_code,
                stock_name       = excluded.stock_name,
                conversion_price = COALESCE(excluded.conversion_price,
                                            cb_reference.conversion_price),
                issue_size       = COALESCE(excluded.issue_size, cb_reference.issue_size),
                list_date        = excluded.list_date,
                rating           = excluded.rating,
                apply_date       = excluded.apply_date,
                apply_code       = excluded.apply_code,
                apply_limit      = excluded.apply_limit,
                update_time      = CURRENT_TIMESTAMP
            ''', (
                bond_code,
                str(record.get("债券简称") or ""),
                _normalize(str(record.get("正股代码") or "").strip()),
                str(record.get("正股简称") or ""),
                _num(record.get("转股价")),
                _num(record.get("发行规模")),
                _text_date(record.get("上市时间")),
                str(record.get("信用评级") or ""),
                _text_date(record.get("申购日期")),
                str(record.get("申购代码") or ""),
                _num(record.get("申购上限")),
            ))
            written += 1
        conn.commit()
    finally:
        conn.close()

    with _LOCK:
        _CACHE["loaded_at"] = None      # 强制下次读走库
    print(f"[转债] 参考数据已更新 {written} 条")
    return written


def _num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value != value else value      # 过滤 NaN


def _text_date(value):
    if value is None:
        return ""
    text = str(value)
    return "" if text in ("NaT", "nan", "None") else text[:10]


def refresh_conversion_prices():
    """补齐 bond_zh_cov 缺失的转股价。返回补上的条数。

    bond_zh_cov 是「发行一览表」，实测只有约三成的行带转股价（新发的带，老券多为空）。
    转股价缺了就算不出转股价值和溢价率，转债面板的核心指标会整列空掉，所以再拉一次
    比价表 bond_cov_comparison 补缺。

    这个接口在机房 IP 上经常被东财掐（RemoteDisconnected），所以走 plugins.proxy 的
    隧道；失败就安静返回 0 —— 补不上就补不上，已有的转股价照常用。
    """
    from plugins import proxy

    try:
        import akshare as ak
        with proxy.env_proxies():
            df = ak.bond_cov_comparison()
    except Exception as e:
        print(f"[转债] 比价表拉取失败，转股价沿用现有数据: {type(e).__name__}: {e}")
        return 0
    if df is None or df.empty:
        return 0

    column = next((c for c in df.columns
                   if "转股价" in c and "溢价" not in c and "价值" not in c), None)
    code_column = next((c for c in df.columns
                        if "代码" in c and "正股" not in c), None)
    if not column or not code_column:
        print(f"[转债] 比价表字段不认识: {list(df.columns)}")
        return 0

    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    filled = 0
    try:
        for record in df.to_dict("records"):
            bond_code = _normalize(str(record.get(code_column) or "").strip())
            price = _num(record.get(column))
            if not bond_code or price is None or price <= 0:
                continue
            cursor.execute(
                "UPDATE cb_reference SET conversion_price = ?, "
                "update_time = CURRENT_TIMESTAMP "
                "WHERE bond_code = ? AND (conversion_price IS NULL OR conversion_price <= 0)",
                (price, bond_code))
            filled += cursor.rowcount
        conn.commit()
    finally:
        conn.close()

    with _LOCK:
        _CACHE["loaded_at"] = None
    if filled:
        print(f"[转债] 从比价表补齐 {filled} 条转股价")
    return filled


def refresh_all():
    """完整刷新：一览表 + 转股价补缺。日更任务调这个。"""
    written = refresh_from_akshare()
    filled = refresh_conversion_prices() if written else 0
    return {"written": written, "conversion_price_filled": filled}


def coverage():
    """转股价覆盖率。缺转股价的券算不出溢价率，前端要能提示出来。"""
    rows = load_all()
    total = len(rows)
    priced = sum(1 for r in rows.values()
                 if r.get("conversion_price") and r["conversion_price"] > 0)
    return {"total": total, "with_conversion_price": priced,
            "missing": total - priced, "stale": is_stale()}


def load_all(force=False):
    """全部转债参考数据 {bond_code: dict}，带内存缓存。"""
    with _LOCK:
        loaded_at = _CACHE["loaded_at"]
        if not force and loaded_at and \
                (datetime.now() - loaded_at).total_seconds() < CACHE_TTL_SECONDS:
            return _CACHE["by_bond"]

    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            SELECT bond_code, bond_name, stock_code, stock_name, conversion_price,
                   issue_size, list_date, rating, apply_date, apply_code, apply_limit,
                   update_time
            FROM cb_reference
        ''')
        columns = ["bond_code", "bond_name", "stock_code", "stock_name",
                   "conversion_price", "issue_size", "list_date", "rating",
                   "apply_date", "apply_code", "apply_limit", "update_time"]
        rows = {row[0]: dict(zip(columns, row)) for row in cursor.fetchall()}
    finally:
        conn.close()

    with _LOCK:
        _CACHE.update({"loaded_at": datetime.now(), "by_bond": rows})
    return rows


def get(bond_code):
    """单只转债的参考数据；没有返回 None。"""
    return load_all().get(_normalize(bond_code))


def stock_of(bond_code):
    """转债对应的正股代码。"""
    return (get(bond_code) or {}).get("stock_code") or ""


def is_stale(max_age_hours=48):
    """库里的数据是不是太旧了（akshare 长期失败时给前端提示用）。"""
    rows = load_all()
    if not rows:
        return True
    newest = max((r.get("update_time") or "") for r in rows.values())
    if not newest:
        return True
    try:
        return datetime.now() - datetime.strptime(newest[:19], "%Y-%m-%d %H:%M:%S") \
            > timedelta(hours=max_age_hours)
    except ValueError:
        return True


def pending_applications(today=None):
    """今天可以申购的新债 [{bond_code, bond_name, apply_code, apply_limit}]。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    return [
        {"bond_code": r["bond_code"], "bond_name": r["bond_name"],
         "apply_code": r["apply_code"], "apply_limit": r["apply_limit"]}
        for r in load_all().values()
        if (r.get("apply_date") or "") == today and r.get("apply_code")
    ]
