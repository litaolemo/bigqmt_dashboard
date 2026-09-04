"""条件单（止损/止盈/条件买入）的存储层。

跟 bridge/cb 一样自己管表：这是后加的模块，不进 app.py 的 init_db()，
建表放在 ensure_table()，调用方按需触发（跟 cb/reference.py 一个路数）。

side + compare 两个字段表达触发方式，trigger_type 是给它们起的好记名字，
创建/展示时用：

    stop_loss       sell + lte           跌破 trigger_price 卖出（止损）
    take_profit     sell + gte           涨破 trigger_price 卖出（止盈）
    buy_dip         buy  + lte           跌破 trigger_price 买入（抄底/补仓）
    buy_breakout    buy  + gte           涨破 trigger_price 买入（突破追涨）
    limit_up_break  sell + limit_break   当日最高价碰过涨停价，后来跌破就卖（开板出逃）
    limit_up_buy    buy  + limit_touch   价格触及当日涨停价就买（封板抢排单）

最后两种的"触发价"不是用户填的数字，是当天动态算出来的涨停价（每天都不一样），
所以它们不需要 trigger_price——DYNAMIC_TRIGGER_TYPES 里的类型创建时可以不填。
"""

import threading

import dbaccess
from bridge import market as bridge_market
from bridge import pricetypes

TRIGGER_TYPES = {
    "stop_loss":       ("sell", "lte"),
    "take_profit":     ("sell", "gte"),
    "buy_dip":         ("buy", "lte"),
    "buy_breakout":    ("buy", "gte"),
    "limit_up_break":  ("sell", "limit_break"),
    "limit_up_buy":    ("buy", "limit_touch"),
}

# 这些类型的"触发价"是当天动态算出来的涨停价，不是用户填的固定数字。
DYNAMIC_TRIGGER_TYPES = frozenset({"limit_up_break", "limit_up_buy"})

# 条件单没指定报价方式时用对手方最优，而不是手动面板那个「最新价」：
# 止损单报不掉等于没止损，而它触发的时候人多半不在。对手方最优是立即可
# 成交的市价指令，这是条件单该有的默认，别跟手动下单的默认混为一谈。
DEFAULT_PRICE_TYPE = "peer"

STATUS_ACTIVE = "active"
STATUS_SUBMITTING = "submitting"   # 已经拿到下单权、正在提交给大QMT，见 claim_for_firing
STATUS_TRIGGERED = "triggered"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

_LOCK = threading.RLock()
_ensured = False


def ensure_table():
    global _ensured
    with _LOCK:
        if _ensured:
            return
        conn = dbaccess.connect()
        cursor = conn.cursor()
        try:
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS conditional_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                side TEXT NOT NULL,
                compare TEXT NOT NULL,
                trigger_price REAL NOT NULL,
                volume INTEGER DEFAULT 0,
                percentage INTEGER DEFAULT 0,
                price_type TEXT DEFAULT 'peer',
                trade_mode TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                order_sys_id TEXT,
                message TEXT DEFAULT '',
                operator TEXT DEFAULT '',
                remark TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                triggered_at TIMESTAMP,
                last_notified_at TIMESTAMP
            )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_conditional_orders_active '
                           'ON conditional_orders (status, stock_code)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_conditional_orders_account '
                           'ON conditional_orders (account_id, status)')
            conn.commit()
        finally:
            conn.close()
        _ensured = True


def _row_to_dict(cursor, row):
    if row is None:
        return None
    columns = [d[0] for d in cursor.description]
    return dict(zip(columns, row))


def create(account_id, stock_code, trigger_type, trigger_price=None, trigger_pct=None,
          volume=0, percentage=0, price_type="", trade_mode="", operator="", remark=""):
    """新建一条条件单。校验只管字段本身是否自洽——标的规则/价格笼子留给触发时的
    place_order 去把关，创建时的价格未必就是触发时该遵守的价格笼子（笼子是实时算的）。

    trigger_price / trigger_pct 二选一（普通类型必须给一个；DYNAMIC_TRIGGER_TYPES
    两个都不能给，那类的触发价是当天动态算的涨停价）。trigger_pct 是涨跌幅的
    百分比数值（比如"跌 8% 止损"填 8，不是 -8——方向由 trigger_type 的 compare
    决定：lte 类跌 pct%，gte 类涨 pct%），换算成绝对价格时按创建这一刻的昨收价算，
    之后不会跟着每天的昨收重新计算。
    """
    if trigger_type not in TRIGGER_TYPES:
        raise ValueError("不支持的条件单类型: %r（可选 %s）"
                         % (trigger_type, "/".join(TRIGGER_TYPES)))
    side, compare = TRIGGER_TYPES[trigger_type]
    is_dynamic = trigger_type in DYNAMIC_TRIGGER_TYPES

    if is_dynamic:
        if trigger_price not in (None, 0, 0.0) or trigger_pct is not None:
            raise ValueError(
                "%s 的触发价是当天动态算出的涨停价，不用也不能自己填 "
                "trigger_price / trigger_pct" % trigger_type)
        trigger_price = 0.0
    else:
        if trigger_price not in (None, 0, 0.0) and trigger_pct is not None:
            raise ValueError("trigger_price 和 trigger_pct 二选一，不能同时给")
        if trigger_pct is not None:
            try:
                pct = float(trigger_pct)
            except (TypeError, ValueError):
                raise ValueError("涨跌幅必须是数字")
            if pct <= 0:
                raise ValueError("涨跌幅必须大于 0（方向由条件单类型决定，不用给负数）")
            reference = bridge_market.price_reference(stock_code)
            last_close = reference.get("last_close")
            if not last_close or last_close <= 0:
                raise ValueError("取不到 %s 的昨收价，算不出涨跌幅对应的绝对价格" % stock_code)
            factor = (1 + pct / 100) if compare == "gte" else (1 - pct / 100)
            trigger_price = round(last_close * factor, 3)
        else:
            try:
                trigger_price = float(trigger_price)
            except (TypeError, ValueError):
                raise ValueError("触发价必须是数字")
        if trigger_price <= 0:
            raise ValueError("触发价必须大于 0")

    volume = int(volume or 0)
    percentage = int(percentage or 0)
    if volume <= 0 and percentage <= 0:
        raise ValueError("必须给出数量或百分比二选一")
    if percentage and side != "sell":
        raise ValueError("按百分比只能用于卖出方向（止损/止盈）——买入没有“持仓百分比”这回事")
    if percentage and not (0 < percentage <= 100):
        raise ValueError("百分比必须在 1-100 之间")

    price_type = price_type or DEFAULT_PRICE_TYPE
    try:
        _, price_spec = pricetypes.resolve(price_type, stock_code)
    except ValueError as e:
        raise ValueError(str(e))
    if price_spec["price_role"] == pricetypes.PRICE_ROLE_ORDER:
        # 限价类需要一个独立的委托价，条件单目前只存触发价，两者不是一回事——
        # 触发价过时就可能变成一个挂不出去或者立刻打穿的限价单。先不支持。
        raise ValueError("条件单暂不支持「%s」这类需要单独填委托价的报价方式，"
                         "用市价类（最新价/对手方最优/涨跌停价等）" % price_spec["label"])

    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        cursor.execute('''
        INSERT INTO conditional_orders
            (account_id, stock_code, trigger_type, side, compare, trigger_price,
             volume, percentage, price_type, trade_mode, operator, remark)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (str(account_id), str(stock_code), trigger_type, side, compare, trigger_price,
              volume, percentage, price_type or DEFAULT_PRICE_TYPE, trade_mode or "",
              operator or "", remark or ""))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def list_active(account_id=None):
    """所有 status='active' 的条件单，供触发引擎扫描用。"""
    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        if account_id and str(account_id).lower() != "all":
            cursor.execute(
                "SELECT * FROM conditional_orders WHERE status = ? AND account_id = ? "
                "ORDER BY created_at DESC", (STATUS_ACTIVE, str(account_id)))
        else:
            cursor.execute(
                "SELECT * FROM conditional_orders WHERE status = ? ORDER BY created_at DESC",
                (STATUS_ACTIVE,))
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def list_all(account_id=None, limit=200):
    """全部条件单（含已触发/已撤/失败），管理页/历史查看用。"""
    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        if account_id and str(account_id).lower() != "all":
            cursor.execute(
                "SELECT * FROM conditional_orders WHERE account_id = ? "
                "ORDER BY created_at DESC LIMIT ?", (str(account_id), int(limit)))
        else:
            cursor.execute(
                "SELECT * FROM conditional_orders ORDER BY created_at DESC LIMIT ?",
                (int(limit),))
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def get(order_id):
    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM conditional_orders WHERE id = ?", (int(order_id),))
        return _row_to_dict(cursor, cursor.fetchone())
    finally:
        conn.close()


def cancel(order_id, account_id=None):
    """撤销一条还没触发的条件单。account_id 给了就顺带校验归属，越权撤不掉。

    返回是否真的撤了——已经触发/撤过/失败的条件单再撤一次没有效果。
    """
    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        if account_id:
            cursor.execute(
                "UPDATE conditional_orders SET status = ? "
                "WHERE id = ? AND account_id = ? AND status = ?",
                (STATUS_CANCELLED, int(order_id), str(account_id), STATUS_ACTIVE))
        else:
            cursor.execute(
                "UPDATE conditional_orders SET status = ? WHERE id = ? AND status = ?",
                (STATUS_CANCELLED, int(order_id), STATUS_ACTIVE))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def claim_for_firing(order_id):
    """把 active 原子性地翻成 submitting，拿到"真的去下单"的独占权。

    返回是否抢到了。抢不到（已经不是 active 了——已经被处理过、被撤了、或者
    本来就不在 active）就不该再下单。防的是：place_order 已经成功提交，但
    紧接着落库记录触发结果那一步万一失败/异常，如果没有这道原子翻转，条件
    单会一直停在 active，下一轮检查条件仍然成立就会被再次触发，真的下出
    两笔单——这是交易系统里不能接受的那种错误。
    """
    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE conditional_orders SET status = ? WHERE id = ? AND status = ?",
            (STATUS_SUBMITTING, int(order_id), STATUS_ACTIVE))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def release_claim(order_id):
    """下单没能成功提交：把 submitting 退回 active，留着下一轮正常重试。"""
    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE conditional_orders SET status = ? WHERE id = ? AND status = ?",
            (STATUS_ACTIVE, int(order_id), STATUS_SUBMITTING))
        conn.commit()
    finally:
        conn.close()


def mark_triggered(order_id, order_sys_id, message=""):
    """触发成功：下单已经提交给大QMT。这条条件单的使命到此结束——后续成没成交
    看正常的委托/成交记录，不再由条件单自己跟踪。
    """
    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        cursor.execute('''
        UPDATE conditional_orders
        SET status = ?, order_sys_id = ?, message = ?, triggered_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''', (STATUS_TRIGGERED, str(order_sys_id or ""), message, int(order_id)))
        conn.commit()
    finally:
        conn.close()


def mark_failed(order_id, message):
    """终态失败——不会再重试。目前只有一种情形会走到这里：卖出条件单触发时
    可用持仓已经是 0（仓位已经不在了，条件单的保护对象没了，重试也没有意义）。
    """
    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        cursor.execute('''
        UPDATE conditional_orders SET status = ?, message = ? WHERE id = ?
        ''', (STATUS_FAILED, message, int(order_id)))
        conn.commit()
    finally:
        conn.close()


def record_retry_failure(order_id, message, notified=False):
    """条件已经满足但下单没成功（比如撞上风控闸门）：不消费这条条件单，留着
    active 下一轮再试——已知条件仍然成立，把用户的保护单默默关掉才是真正的风险。

    notified 由调用方（触发引擎）传：只有真的发了通知才更新 last_notified_at，
    否则节流窗口会被每一轮重试都往后推，永远发不出第二条通知。
    """
    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        if notified:
            cursor.execute('''
            UPDATE conditional_orders
            SET message = ?, last_notified_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''', (message, int(order_id)))
        else:
            cursor.execute(
                "UPDATE conditional_orders SET message = ? WHERE id = ?",
                (message, int(order_id)))
        conn.commit()
    finally:
        conn.close()


def reset_for_tests():
    """测试用：清空表内容，不重建表结构。"""
    ensure_table()
    conn = dbaccess.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM conditional_orders")
        conn.commit()
    finally:
        conn.close()
