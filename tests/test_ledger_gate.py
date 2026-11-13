"""跨日账本与开跑门闩验收：七段。

后台日期通过 TEST_BACKOFFICE_DATE 钉死，保证日期断言可重复。
"""

import pytest

from app.db import get_db
from app.services import ledger
from app.services.clock import today_str
from app.services.ledger import (
    METHOD_MERGE_TODAY,
    METHOD_SEPARATE_TASK,
    LedgerError,
    REASON_METHOD_MISMATCH,
    REASON_ORDER_LOCK,
    REASON_SETTLEMENT_RUNNING,
    REASON_SKIP_DAY,
)

PINNED_TODAY = "2026-11-13"
DAY1 = "2026-11-10"
DAY2 = "2026-11-11"
DAY3 = "2026-11-12"


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


@pytest.fixture()
def pinned(app):
    app.config["TEST_BACKOFFICE_DATE"] = PINNED_TODAY
    return app


def seed_three_days(app):
    """给 11-10 / 11-11 / 11-12 各挂未了。"""
    with app.app_context():
        ledger.seed_pending(
            DAY1,
            [
                {"title": "市政管线测绘记录表", "seed_path": "/mock/catalog/alpha"},
                {"title": "泵站运行日报", "seed_path": "/mock/catalog/beta"},
            ],
        )
        ledger.seed_pending(
            DAY2, [{"title": "雨污分流抽检单", "seed_path": "/mock/catalog/beta"}]
        )
        ledger.seed_pending(
            DAY3, [{"title": "排水井巡查台账", "seed_path": "/mock/catalog/alpha"}]
        )


# ----------------------------------------------------------- 页眉汇总

def test_header_lists_pending_per_day_by_backoffice_date(pinned, client):
    login(client)
    seed_three_days(pinned)
    r = client.get("/ledger/")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    # 页眉并排出现三个来源日的未了条数（2、1、1），顺序最旧在前
    assert DAY1 in text and DAY2 in text and DAY3 in text
    with pinned.app_context():
        summary = ledger.header_summary()
    assert summary["today"] == PINNED_TODAY
    assert [d["day"] for d in summary["pending_days"]] == [DAY1, DAY2, DAY3]
    assert summary["pending_days"][0]["count"] == 2
    assert summary["pending_total"] == 4
    assert summary["today_collected"] == 0
    assert summary["policy_method"] is None
    # 页眉在普通页面也可见
    home = client.get("/").get_data(as_text=True)
    assert "跨日未了" in home and DAY1 in home


# ----------------------------------------------------------- 段一：更旧未了不能先清昨天

def test_cannot_clear_yesterday_when_older_pending_exists(pinned):
    seed_three_days(pinned)
    with pinned.app_context():
        # 直接清昨天（11-12），但还有 11-10/11-11 更旧未了
        with pytest.raises(LedgerError) as ei:
            ledger.start_settlement(DAY3, METHOD_MERGE_TODAY)
        assert ei.value.reason == REASON_SKIP_DAY
        assert "跳日" in ei.value.message
        # 失败留痕：卡片写跳日
        task = get_db().execute(
            "SELECT * FROM settlement_tasks WHERE status='failed'"
        ).fetchone()
        assert task["fail_reason"] == REASON_SKIP_DAY
        assert task["target_day"] == DAY3
        # 没有任何 running 任务，未了一条没动
        assert ledger.has_running_settlement() is False
        assert len(ledger.list_pending()) == 4


def test_must_clear_oldest_first_in_order(pinned):
    seed_three_days(pinned)
    with pinned.app_context():
        # 顺序清：11-10 成功 → 完结 → 11-11 成功 → 完结 → 11-12 成功
        for day in (DAY1, DAY2, DAY3):
            task = ledger.start_settlement(day, METHOD_MERGE_TODAY)
            assert task["target_day"] == day and task["status"] == "running"
            done = ledger.complete_settlement(task["id"])
            assert done["status"] == "done"
        assert ledger.open_pending_dates() == []
        assert len(ledger.list_settlement_tasks()) == 3


# ----------------------------------------------------------- 段二：跳日开跑失败，卡片写跳日

def test_run_today_blocked_with_skip_day_reason(pinned, client):
    login(client)
    seed_three_days(pinned)
    r = client.post(
        "/ledger/collect",
        data={"seed_path": "/mock/catalog/alpha"},
        follow_redirects=True,
    )
    text = r.get_data(as_text=True)
    assert "跳日" in text
    with pinned.app_context():
        attempt = get_db().execute(
            "SELECT * FROM run_attempts WHERE result='blocked' ORDER BY id DESC"
        ).fetchone()
        assert attempt is not None
        assert REASON_SKIP_DAY in attempt["reason"]
        # 今日账册没有任何新收行
        assert ledger.header_summary()["today_collected"] == 0
        # 历史日册里也不可能出现今日新收
        hist = get_db().execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE ledger_day < ?",
            (PINNED_TODAY,),
        ).fetchone()["c"]
        assert hist == 0


def test_run_today_ok_after_all_cleared(pinned):
    seed_three_days(pinned)
    with pinned.app_context():
        for day in (DAY1, DAY2, DAY3):
            t = ledger.start_settlement(day, METHOD_MERGE_TODAY)
            ledger.complete_settlement(t["id"])
        # 清完后今日开跑门闩放行（merge 已占用全部键，此处以去重 0 新增验证放行）
        result = ledger.collect_today("/mock/catalog/alpha")
        assert result["status"] == "ok"
        assert result["total_source"] == 3
        # 今日账册里所有条目 ledger_day 恒为今日，无一写进历史日册
        bad = get_db().execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE ledger_day <> ?",
            (PINNED_TODAY,),
        ).fetchone()["c"]
        assert bad == 0


# ----------------------------------------------------------- 段三：按口径并入或分任务

def test_merge_today_keeps_source_day(pinned):
    seed_three_days(pinned)
    with pinned.app_context():
        t = ledger.start_settlement(DAY1, METHOD_MERGE_TODAY)
        ledger.complete_settlement(t["id"])
        rows = get_db().execute(
            "SELECT * FROM ledger_entries WHERE ledger_day=? AND entry_kind='merged'",
            (PINNED_TODAY,),
        ).fetchall()
        # 11-10 的两条都并入今日账册，来源日期字段保留为 11-10
        assert len(rows) == 2
        assert all(r["source_day"] == DAY1 for r in rows)
        # 口径已锁定
        assert ledger.get_policy()["value"] == METHOD_MERGE_TODAY


def test_separate_task_keeps_today_ledger_blank(pinned):
    seed_three_days(pinned)
    with pinned.app_context():
        t = ledger.start_settlement(DAY1, METHOD_SEPARATE_TASK)
        assert t["method"] == METHOD_SEPARATE_TASK
        ledger.complete_settlement(t["id"])
        # 独立清账册有两条，今日账册保持空白
        se = ledger.list_settlement_entries(t["id"])
        assert len(se) == 2 and all(r["source_day"] == DAY1 for r in se)
        today_rows = get_db().execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE ledger_day=?",
            (PINNED_TODAY,),
        ).fetchone()["c"]
        assert today_rows == 0
        # 继续清完，今日账册始终空白
        for day in (DAY2, DAY3):
            tt = ledger.start_settlement(day, METHOD_SEPARATE_TASK)
            ledger.complete_settlement(tt["id"])
        assert get_db().execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE ledger_day=?",
            (PINNED_TODAY,),
        ).fetchone()["c"] == 0
        # 清完后今日开跑才放行
        result = ledger.collect_today("/mock/catalog/beta")
        assert result["status"] == "ok" and result["inserted"] == 2


# ----------------------------------------------------------- 段四：口径不一致被拦住，写方式不对

def test_method_mismatch_blocked(pinned):
    seed_three_days(pinned)
    with pinned.app_context():
        # 先用 merge_today 锁定
        ledger.lock_policy(METHOD_MERGE_TODAY)
        with pytest.raises(LedgerError) as ei:
            ledger.start_settlement(DAY1, METHOD_SEPARATE_TASK)
        assert ei.value.reason == REASON_METHOD_MISMATCH
        assert "方式不对" in ei.value.message
        # 失败卡写方式不对，且没有 running 任务
        failed = get_db().execute(
            "SELECT * FROM settlement_tasks WHERE status='failed'"
        ).fetchone()
        assert failed["fail_reason"] == REASON_METHOD_MISMATCH
        assert ledger.has_running_settlement() is False
        # 口径没变，用对的方式仍可发起
        t = ledger.start_settlement(DAY1, METHOD_MERGE_TODAY)
        assert t["status"] == "running"


def test_lock_policy_cannot_change_after_locked(pinned):
    with pinned.app_context():
        ledger.lock_policy(METHOD_SEPARATE_TASK)
        with pytest.raises(LedgerError) as ei:
            ledger.lock_policy(METHOD_MERGE_TODAY)
        assert ei.value.reason == REASON_METHOD_MISMATCH
        # 重复锁定同一口径幂等成功
        assert ledger.lock_policy(METHOD_SEPARATE_TASK)["value"] == METHOD_SEPARATE_TASK


# ----------------------------------------------------------- 段五：清账未完成时今日新收失败

def test_collect_blocked_while_settlement_running(pinned):
    seed_three_days(pinned)
    with pinned.app_context():
        t = ledger.start_settlement(DAY1, METHOD_MERGE_TODAY)  # 保持 running
        # 不能开今日新收
        result = ledger.collect_today("/mock/catalog/alpha")
        assert result["status"] == "blocked"
        assert result["reason"] == REASON_SETTLEMENT_RUNNING
        # 也不能开另一日的清账
        with pytest.raises(LedgerError) as ei:
            ledger.start_settlement(DAY2, METHOD_MERGE_TODAY)
        assert ei.value.reason == REASON_ORDER_LOCK
        # 完结后新收门闩解除（但仍有更旧未了时会变成跳日拦截）
        ledger.complete_settlement(t["id"])
        result2 = ledger.collect_today("/mock/catalog/alpha")
        assert result2["status"] == "blocked"
        assert REASON_SKIP_DAY in result2["reason"]


# ----------------------------------------------------------- 段六：并发清账只有最旧日成功

def test_concurrent_settlement_only_oldest_succeeds(pinned):
    from tests.test_ledger_concurrency import run_concurrent_settlement

    seed_three_days(pinned)
    outcomes = run_concurrent_settlement(pinned.config["DATABASE"], DAY1, DAY2)
    succeeded = [o for o in outcomes if o["ok"]]
    failed = [o for o in outcomes if not o["ok"]]
    assert len(succeeded) == 1
    assert succeeded[0]["day"] == DAY1
    assert len(failed) == 1
    assert failed[0]["day"] == DAY2
    assert failed[0]["reason"] == REASON_ORDER_LOCK
    with pinned.app_context():
        running = get_db().execute(
            "SELECT target_day FROM settlement_tasks WHERE status='running'"
        ).fetchall()
        assert [r["target_day"] for r in running] == [DAY1]
        failed_rows = get_db().execute(
            "SELECT fail_reason FROM settlement_tasks WHERE status='failed'"
        ).fetchall()
        assert all(r["fail_reason"] == REASON_ORDER_LOCK for r in failed_rows)


# ----------------------------------------------------------- 段七：页面看得出未了顺序和口径

def test_page_shows_order_and_policy(pinned, client):
    login(client)
    seed_three_days(pinned)
    with pinned.app_context():
        ledger.lock_policy(METHOD_MERGE_TODAY)
    text = client.get("/ledger/").get_data(as_text=True)
    # 口径写死可见
    assert "并入今日账册" in text
    # 最旧一天有「必须先清」标记，且日期出现顺序为最旧在前
    i1, i2, i3 = text.index(DAY1), text.index(DAY2), text.index(DAY3)
    assert i1 < i2 < i3
    assert "最旧，必须先清" in text
    # 历史日册题名只读：日册详情页无编辑表单、无改名入口
    with pinned.app_context():
        ledger.ensure_ledger(DAY1)
        get_db().execute(
            "UPDATE day_ledgers SET status='historical' WHERE day=?", (DAY1,)
        )
        get_db().commit()
    detail = client.get(f"/ledger/days/{DAY1}").get_data(as_text=True)
    assert "历史日册" in detail and "只读" in detail
    # 页面唯一的表单是全局退出表单，没有任何针对日册的写入/改名入口
    assert 'name="title"' not in detail
    assert "/ledger/collect" not in detail
    assert "/settlements" not in detail
    assert 'action=""' not in detail


def test_rollover_seals_open_ledger_to_historical_and_pending(pinned):
    with pinned.app_context():
        # 先在较早的后台日做一笔今日新收
        pinned.config["TEST_BACKOFFICE_DATE"] = DAY2
        ledger.collect_today("/mock/catalog/alpha")
        pinned.config["TEST_BACKOFFICE_DATE"] = PINNED_TODAY
        result = ledger.rollover_to(PINNED_TODAY)
        sealed_days = [s["day"] for s in result["sealed"]]
        assert DAY2 in sealed_days
        row = ledger.get_ledger(DAY2)
        assert row["status"] == "historical"
        # 封账条目挂为未了，来源日期为被封的那天
        pending = ledger.list_pending(DAY2)
        assert len(pending) == 2
        assert all(p["source_day"] == DAY2 for p in pending)
        # 题名已固定
        assert row["title"] == f"{DAY2} 日册"
        assert today_str() == PINNED_TODAY
