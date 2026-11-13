import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=15,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 15000")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def db_transaction():
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seed_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crawl_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, item_key)
            );

            -- 跨日账本：每日一册，historical 为只读历史日册
            CREATE TABLE IF NOT EXISTS day_ledgers (
                day TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'historical')),
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- 日册条目：ledger_day 即入账日期；source_day 为来源日期
            -- 今日新收只写 ledger_day = 今日，不进历史日册
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ledger_day TEXT NOT NULL
                    REFERENCES day_ledgers(day),
                source_day TEXT NOT NULL,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                entry_kind TEXT NOT NULL DEFAULT 'new'
                    CHECK (entry_kind IN ('new', 'merged')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(ledger_day, item_key)
            );

            -- 跨日未了：rollover 或手工挂账生成，清账后置 settled
            CREATE TABLE IF NOT EXISTS pending_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_day TEXT NOT NULL,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'settled')),
                settlement_task_id INTEGER
                    REFERENCES settlement_tasks(id),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                settled_at TEXT
            );

            -- 清账任务：一日一任务；partial unique 索引保证全局至多一个 running
            CREATE TABLE IF NOT EXISTS settlement_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_day TEXT NOT NULL,
                method TEXT NOT NULL
                    CHECK (method IN ('merge_today', 'separate_task')),
                status TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'done', 'failed')),
                fail_reason TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_settlement_single_running
                ON settlement_tasks(target_day)
                WHERE status = 'running';

            -- 独立清账任务模式下，每日的独立清账册（今日账册保持空白直到清完）
            CREATE TABLE IF NOT EXISTS settlement_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL
                    REFERENCES settlement_tasks(id) ON DELETE CASCADE,
                source_day TEXT NOT NULL,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(task_id, item_key)
            );

            -- 今日开跑门闩记录：跳日/未清完被拒都留痕，卡片写原因
            CREATE TABLE IF NOT EXISTS run_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seed_path TEXT NOT NULL,
                result TEXT NOT NULL CHECK (result IN ('ok', 'blocked')),
                reason TEXT,
                inserted INTEGER NOT NULL DEFAULT 0,
                skipped_dup INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- 清账口径：一旦选定写死，之后不能改口
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        db.commit()
