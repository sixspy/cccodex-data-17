"""跨日账本领域服务。

四件事在这里闭环：
1. 日册（day_ledgers / ledger_entries）：今日新收只写今日账册，历史日册只读。
2. 跨日未了（pending_items）：日册封历史时挂账，按来源日期记账。
3. 清账任务（settlement_tasks）：必须从最旧的一天顺序清，跳日失败写「跳日」；
   全局同时只许一个清账任务在跑，并发冲突写「顺序锁」；
   口径一旦锁定不能改口，方式对不上当场拦住写「方式不对」。
4. 开跑门闩（run_attempts）：有任何更早日期的未了未清，今日开跑失败写「跳日」；
   清账任务在跑时今日新收失败写「清账未完成」。

两种清账口径（METHOD_*）：
- merge_today：并入今日账册，条目保留 source_day 来源日期字段。
- separate_task：每日生成独立清账任务并写入独立清账册，今日账册保持空白直到清完。
"""

import sqlite3

from app.db import get_db
from app.services.clock import today_str
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html, render_catalog_page
from app.services.settlement_gate import settlement_gate

METHOD_MERGE_TODAY = "merge_today"
METHOD_SEPARATE_TASK = "separate_task"
METHODS = (METHOD_MERGE_TODAY, METHOD_SEPARATE_TASK)
METHOD_LABELS = {
    METHOD_MERGE_TODAY: "并入今日账册（保留来源日期）",
    METHOD_SEPARATE_TASK: "每日独立清账任务（今日账册保持空白）",
}

# 失败原因令牌：页面卡片与测试断言都认这些中文词
REASON_SKIP_DAY = "跳日"
REASON_ORDER_LOCK = "顺序锁"
REASON_METHOD_MISMATCH = "方式不对"
REASON_SETTLEMENT_RUNNING = "清账未完成"

HEADER_RECENT_DAYS = 7


class LedgerError(Exception):
    """带原因令牌的领域错误，reason 直接可写卡片/flash。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------- 口径锁

def get_policy() -> sqlite3.Row | None:
    row = get_db().execute(
        "SELECT key, value FROM app_settings WHERE key = 'settlement_method'"
    ).fetchone()
    return row


def lock_policy(method: str) -> sqlite3.Row:
    """选定清账口径并写死；已锁定后改口当场拦住。"""
    if method not in METHODS:
        raise LedgerError(REASON_METHOD_MISMATCH, "方式不对：未知清账方式")
    db = get_db()
    existing = get_policy()
    if existing is not None:
        if existing["value"] != method:
            raise LedgerError(
                REASON_METHOD_MISMATCH,
                f"方式不对：页眉已写死口径「{METHOD_LABELS[existing['value']]}」，"
                f"不能改成「{METHOD_LABELS[method]}」",
            )
        return existing
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('settlement_method', ?)",
        (method,),
    )
    db.commit()
    return get_policy()


# ---------------------------------------------------------------- 日册

def _ensure_ledger_row(db, day: str) -> None:
    """事务内调用：只插行不提交。"""
    exists = db.execute(
        "SELECT 1 FROM day_ledgers WHERE day = ?", (day,)
    ).fetchone()
    if exists is None:
        db.execute(
            "INSERT INTO day_ledgers (day, title, status) VALUES (?, ?, 'open')",
            (day, f"{day} 日册"),
        )


def ensure_ledger(day: str) -> sqlite3.Row:
    db = get_db()
    _ensure_ledger_row(db, day)
    db.commit()
    return db.execute("SELECT * FROM day_ledgers WHERE day = ?", (day,)).fetchone()


def get_ledger(day: str) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM day_ledgers WHERE day = ?", (day,)
    ).fetchone()


def list_ledgers() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM day_ledgers ORDER BY day DESC"
    ).fetchall()


def list_entries(day: str) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM ledger_entries WHERE ledger_day = ? ORDER BY id", (day,)
    ).fetchall()


def seed_pending(day: str, items: list[dict]) -> int:
    """测试/演示用：给指定历史日期直接挂未了，不经过今日账册。"""
    db = get_db()
    n = 0
    for it in items:
        key = it.get("item_key") or normalize_item_key(
            it.get("seed_path", "/mock/catalog/alpha"), it["title"]
        )
        db.execute(
            """
            INSERT INTO pending_items (source_day, item_key, title, status)
            VALUES (?, ?, ?, 'open')
            """,
            (day, key, it["title"]),
        )
        n += 1
    db.commit()
    return n


def rollover_to(new_today: str) -> dict:
    """跨日：把所有早于 new_today 的在途日册封成历史日册，条目挂为未了。

    历史日册题名在封册时固定，之后只读。
    """
    db = get_db()
    sealed = []
    old_ledgers = db.execute(
        "SELECT * FROM day_ledgers WHERE status = 'open' AND day < ? ORDER BY day",
        (new_today,),
    ).fetchall()
    for ledger in old_ledgers:
        day = ledger["day"]
        entries = list_entries(day)
        db.execute(
            "UPDATE day_ledgers SET status = 'historical' WHERE day = ?", (day,)
        )
        for e in entries:
            db.execute(
                """
                INSERT INTO pending_items (source_day, item_key, title, status)
                VALUES (?, ?, ?, 'open')
                """,
                (day, e["item_key"], e["title"]),
            )
        sealed.append({"day": day, "items": len(entries)})
    db.commit()
    return {"sealed": sealed, "today": new_today}


# ---------------------------------------------------------------- 页眉汇总

def header_summary() -> dict:
    """页眉并排数据：各日未了条数、今日已收、当前口径。只读不建册。"""
    db = get_db()
    today = today_str()
    pending_rows = db.execute(
        """
        SELECT source_day, COUNT(*) AS c
        FROM pending_items
        WHERE status = 'open' AND source_day < ?
        GROUP BY source_day
        ORDER BY source_day ASC
        LIMIT ?
        """,
        (today, HEADER_RECENT_DAYS),
    ).fetchall()
    pending_days = [{"day": r["source_day"], "count": r["c"]} for r in pending_rows]
    today_count = db.execute(
        "SELECT COUNT(*) AS c FROM ledger_entries WHERE ledger_day = ?", (today,)
    ).fetchone()["c"]
    policy = get_policy()
    return {
        "today": today,
        "pending_days": pending_days,
        "pending_total": sum(d["count"] for d in pending_days),
        "today_collected": today_count,
        "policy_method": policy["value"] if policy else None,
        "policy_label": METHOD_LABELS[policy["value"]] if policy else "未锁定",
    }


def open_pending_dates() -> list[str]:
    rows = get_db().execute(
        """
        SELECT DISTINCT source_day FROM pending_items
        WHERE status = 'open' ORDER BY source_day ASC
        """
    ).fetchall()
    return [r["source_day"] for r in rows]


def has_running_settlement(db=None) -> bool:
    db = db or get_db()
    return db.execute(
        "SELECT 1 FROM settlement_tasks WHERE status = 'running' LIMIT 1"
    ).fetchone() is not None


# ---------------------------------------------------------------- 清账任务

def _record_failed_settlement(target_day: str, method: str, reason: str) -> None:
    """被拒的清账也落一张 failed 卡，fail_reason 写原因令牌。"""
    db = get_db()
    db.execute(
        "INSERT INTO settlement_tasks (target_day, method, status, fail_reason, "
        "finished_at) VALUES (?, ?, 'failed', ?, datetime('now'))",
        (target_day, method if method in METHODS else METHODS[0], reason),
    )
    db.commit()


def start_settlement(target_day: str, method: str) -> sqlite3.Row:
    """发起一日清账。

    口径门闩在锁外先拦（方式不对）；顺序门闩全部放进 BEGIN IMMEDIATE 写锁内，
    且锁内先查 running 再查顺序。这样：
    - 单人顺序点非最旧日：无 running → 撞顺序门闩，落「跳日」；
    - 两人并发点不同日期：后拿锁者先看到 running → 落「顺序锁」，只许最旧日成功。

    任何拒绝都落一张 failed 清账卡（卡片写跳日/顺序锁/方式不对）。
    """
    if method not in METHODS:
        raise LedgerError(REASON_METHOD_MISMATCH, "方式不对：未知清账方式")

    db = get_db()
    policy = get_policy()
    if policy is not None and policy["value"] != method:
        message = (
            f"方式不对：页眉已写死「{METHOD_LABELS[policy['value']]}」，"
            f"本次请求却是「{METHOD_LABELS[method]}」"
        )
        _record_failed_settlement(target_day, method, REASON_METHOD_MISMATCH)
        raise LedgerError(REASON_METHOD_MISMATCH, message)

    today = today_str()
    if target_day >= today:
        _record_failed_settlement(target_day, method, REASON_SKIP_DAY)
        raise LedgerError(REASON_SKIP_DAY, "跳日：只能清今天以前的历史日期")

    if target_day not in open_pending_dates():
        _record_failed_settlement(target_day, method, REASON_SKIP_DAY)
        raise LedgerError(
            REASON_SKIP_DAY, f"跳日：{target_day} 没有未了，不能开清账"
        )

    # 进程内并发门闩：同刻并发组里只放目标日最旧的请求 / 已有 running → 顺序锁
    if settlement_gate.check_concurrent(target_day):
        _record_failed_settlement(target_day, method, REASON_ORDER_LOCK)
        raise LedgerError(
            REASON_ORDER_LOCK,
            "顺序锁：同一时刻只许一个清账请求，并发只放最旧的一天",
        )

    # 顺序门闩在写锁内裁决，保证读 pending 与插任务原子
    db.execute("BEGIN IMMEDIATE")
    try:
        open_days_locked = [
            r["source_day"]
            for r in db.execute(
                "SELECT DISTINCT source_day FROM pending_items "
                "WHERE status = 'open' ORDER BY source_day ASC"
            ).fetchall()
        ]
        if target_day not in open_days_locked or any(
            d < target_day for d in open_days_locked
        ):
            oldest = open_days_locked[0] if open_days_locked else target_day
            raise LedgerError(
                REASON_SKIP_DAY,
                f"跳日：还有更旧的未了 {oldest} 未清，必须从最旧的一天清起",
            )
        if policy is None:
            db.execute(
                "INSERT INTO app_settings (key, value) "
                "VALUES ('settlement_method', ?)",
                (method,),
            )
        cur = db.execute(
            "INSERT INTO settlement_tasks (target_day, method, status) "
            "VALUES (?, ?, 'running')",
            (target_day, method),
        )
        task_id = cur.lastrowid
        db.commit()
    except LedgerError as e:
        db.rollback()
        _record_failed_settlement(target_day, method, e.reason)
        raise
    except sqlite3.IntegrityError as e:
        db.rollback()
        _record_failed_settlement(target_day, method, REASON_ORDER_LOCK)
        raise LedgerError(REASON_ORDER_LOCK, f"顺序锁：清账任务冲突（{e}）")
    except Exception:
        db.rollback()
        raise

    # 任务成功占位：在它完结前，任何清账/今日新收都被顺序锁挡住
    settlement_gate.task_started(task_id)

    return db.execute(
        "SELECT * FROM settlement_tasks WHERE id = ?", (task_id,)
    ).fetchone()


def complete_settlement(task_id: int) -> sqlite3.Row:
    """执行并完结一个清账任务：按锁定口径入账。"""
    db = get_db()
    task = db.execute(
        "SELECT * FROM settlement_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if task is None:
        raise LedgerError(REASON_ORDER_LOCK, "清账任务不存在")
    if task["status"] != "running":
        raise LedgerError(
            REASON_ORDER_LOCK, f"清账任务已 {task['status']}，不能重复清账"
        )

    target_day = task["target_day"]
    method = task["method"]
    db.execute("BEGIN IMMEDIATE")
    try:
        items = db.execute(
            "SELECT * FROM pending_items WHERE source_day = ? AND status = 'open'",
            (target_day,),
        ).fetchall()
        if not items:
            raise LedgerError(REASON_SKIP_DAY, f"跳日：{target_day} 已无未了可清")

        if method == METHOD_MERGE_TODAY:
            # 并入今日账册，保留来源日期字段
            today = today_str()
            _ensure_ledger_row(db, today)
            for it in items:
                db.execute(
                    """
                    INSERT OR IGNORE INTO ledger_entries
                        (ledger_day, source_day, item_key, title, entry_kind)
                    VALUES (?, ?, ?, ?, 'merged')
                    """,
                    (today, target_day, it["item_key"], it["title"]),
                )
        else:
            # 独立清账任务：写独立清账册，今日账册不动
            for it in items:
                db.execute(
                    """
                    INSERT OR IGNORE INTO settlement_entries
                        (task_id, source_day, item_key, title)
                    VALUES (?, ?, ?, ?)
                    """,
                    (task_id, target_day, it["item_key"], it["title"]),
                )

        db.execute(
            """
            UPDATE pending_items
            SET status = 'settled', settlement_task_id = ?,
                settled_at = datetime('now')
            WHERE source_day = ? AND status = 'open'
            """,
            (task_id, target_day),
        )
        db.execute(
            "UPDATE settlement_tasks SET status = 'done', "
            "finished_at = datetime('now') WHERE id = ?",
            (task_id,),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    # 任务完结，释放进程内门闩，允许下一日清账或今日新收
    settlement_gate.task_finished()

    return db.execute(
        "SELECT * FROM settlement_tasks WHERE id = ?", (task_id,)
    ).fetchone()


def list_settlement_tasks() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM settlement_tasks ORDER BY id DESC"
    ).fetchall()


def list_settlement_entries(task_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM settlement_entries WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()


def list_pending(day: str | None = None) -> list[sqlite3.Row]:
    db = get_db()
    if day is None:
        return db.execute(
            "SELECT * FROM pending_items WHERE status = 'open' "
            "ORDER BY source_day ASC, id ASC"
        ).fetchall()
    return db.execute(
        "SELECT * FROM pending_items WHERE status = 'open' AND source_day = ? "
        "ORDER BY id",
        (day,),
    ).fetchall()


# ---------------------------------------------------------------- 今日开跑门闩

def collect_today(seed_path: str) -> dict:
    """今日新收：先过门闩（清账在跑 / 更旧未了），再写今日账册。

    今日新收的行 ledger_day 恒为今日，永远不写历史日册。
    每次尝试都落 run_attempts，被拒时卡片写原因。
    """
    if seed_path not in CATALOGS:
        raise ValueError("seed path not registered")

    db = get_db()
    today = today_str()
    ensure_ledger(today)  # 事务外先建行，避免事务内隐式提交
    db.execute("BEGIN IMMEDIATE")
    try:
        # 清账在跑：进程内门闩与库内 running 双重确认
        if settlement_gate.is_locked() or has_running_settlement(db):
            db.execute(
                "INSERT INTO run_attempts (seed_path, result, reason) "
                "VALUES (?, 'blocked', ?)",
                (seed_path, REASON_SETTLEMENT_RUNNING),
            )
            db.commit()
            return {
                "status": "blocked",
                "reason": REASON_SETTLEMENT_RUNNING,
                "inserted": 0,
                "skipped_dup": 0,
            }

        older = db.execute(
            "SELECT DISTINCT source_day FROM pending_items "
            "WHERE status = 'open' AND source_day < ? ORDER BY source_day ASC",
            (today,),
        ).fetchall()
        if older:
            reason = f"{REASON_SKIP_DAY}：更早日期的未了未清（最旧 {older[0]['source_day']}）"
            db.execute(
                "INSERT INTO run_attempts (seed_path, result, reason) "
                "VALUES (?, 'blocked', ?)",
                (seed_path, reason),
            )
            db.commit()
            return {
                "status": "blocked",
                "reason": reason,
                "inserted": 0,
                "skipped_dup": 0,
            }

        _ensure_ledger_row(db, today)
        html = render_catalog_page(seed_path)
        if isinstance(html, tuple):
            html = html[0]
        items = parse_catalog_html(html, seed_path)
        inserted = 0
        for it in items:
            key = normalize_item_key(seed_path, it["title"])
            cur = db.execute(
                """
                INSERT OR IGNORE INTO ledger_entries
                    (ledger_day, source_day, item_key, title, entry_kind)
                VALUES (?, ?, ?, ?, 'new')
                """,
                (today, today, key, it["title"]),
            )
            inserted += cur.rowcount
        skipped = len(items) - inserted
        db.execute(
            "INSERT INTO run_attempts "
            "(seed_path, result, reason, inserted, skipped_dup) "
            "VALUES (?, 'ok', NULL, ?, ?)",
            (seed_path, inserted, skipped),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "status": "ok",
        "inserted": inserted,
        "skipped_dup": skipped,
        "total_source": len(items),
    }


def list_run_attempts() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM run_attempts ORDER BY id DESC LIMIT 20"
    ).fetchall()
