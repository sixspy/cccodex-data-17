"""跨日账本路由：页眉门闩、历史日册、清账任务、今日开跑四处联动。"""

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import get_db
from app.routes.auth import login_required
from app.services import ledger
from app.services.clock import today_str
from app.services.ledger import (
    METHOD_LABELS,
    METHODS,
    LedgerError,
)
from app.services.mock_catalog import CATALOGS

bp = Blueprint("ledger", __name__, url_prefix="/ledger")


@bp.get("/")
@login_required
def index():
    db = get_db()
    summary = ledger.header_summary()
    ledgers = ledger.list_ledgers()
    pending = ledger.list_pending()
    tasks = ledger.list_settlement_tasks()
    attempts = ledger.list_run_attempts()

    # 未了按来源日分组，最旧的一天排最前
    pending_groups: dict[str, list] = {}
    for p in pending:
        pending_groups.setdefault(p["source_day"], []).append(p)
    pending_groups = dict(sorted(pending_groups.items()))

    ledger_counts = {
        r["ledger_day"]: r["c"]
        for r in db.execute(
            "SELECT ledger_day, COUNT(*) AS c FROM ledger_entries GROUP BY ledger_day"
        )
    }

    running_task = next((t for t in tasks if t["status"] == "running"), None)
    return render_template(
        "ledger/index.html",
        summary=summary,
        ledgers=ledgers,
        ledger_counts=ledger_counts,
        pending_groups=pending_groups,
        tasks=tasks,
        attempts=attempts,
        running_task=running_task,
        methods=METHODS,
        method_labels=METHOD_LABELS,
        seeds=sorted(CATALOGS.keys()),
    )


@bp.post("/policy")
@login_required
def lock_policy():
    method = request.form.get("method", "").strip()
    try:
        row = ledger.lock_policy(method)
        flash(f"清账口径已写死：{METHOD_LABELS[row['value']]}，之后不能改口", "ok")
    except LedgerError as e:
        flash(e.message, "error")
    return redirect(url_for("ledger.index"))


@bp.post("/rollover")
@login_required
def rollover():
    """跨日封账：把早于后台今日的在途日册转为只读历史日册并挂未了。"""
    result = ledger.rollover_to(today_str())
    if result["sealed"]:
        detail = "、".join(f"{s['day']}（{s['items']} 条）" for s in result["sealed"])
        flash(f"已跨日封账：{detail} 转为历史日册并挂入未了", "ok")
    else:
        flash("没有需要封账的在途日册", "ok")
    return redirect(url_for("ledger.index"))


@bp.post("/collect")
@login_required
def collect():
    seed = request.form.get("seed_path", "").strip()
    if seed not in CATALOGS:
        flash("种子路径必须是已注册的 mock 目录", "error")
        return redirect(url_for("ledger.index"))
    try:
        result = ledger.collect_today(seed)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("ledger.index"))
    if result["status"] == "blocked":
        flash(f"今日开跑被拦：{result['reason']}", "error")
    else:
        flash(
            f"今日新收完成：入账 {result['inserted']} 条，"
            f"去重跳过 {result['skipped_dup']} 条",
            "ok",
        )
    return redirect(url_for("ledger.index"))


@bp.post("/settlements")
@login_required
def start_settlement():
    target_day = request.form.get("target_day", "").strip()
    method = request.form.get("method", "").strip()
    try:
        task = ledger.start_settlement(target_day, method)
        flash(
            f"清账任务已发起：{target_day}，口径「{METHOD_LABELS[task['method']]}」，"
            "状态 running",
            "ok",
        )
    except LedgerError as e:
        flash(e.message, "error")
    return redirect(url_for("ledger.index"))


@bp.post("/settlements/<int:task_id>/complete")
@login_required
def complete_settlement(task_id: int):
    try:
        task = ledger.complete_settlement(task_id)
        flash(
            f"清账任务 {task_id} 完成：{task['target_day']} 的未了已按"
            f"「{METHOD_LABELS[task['method']]}」清完",
            "ok",
        )
    except LedgerError as e:
        flash(e.message, "error")
    return redirect(url_for("ledger.index"))


@bp.get("/days/<day>")
@login_required
def day_detail(day: str):
    ledger_row = ledger.get_ledger(day)
    if ledger_row is None:
        flash("日册不存在", "error")
        return redirect(url_for("ledger.index"))
    entries = ledger.list_entries(day)
    is_historical = ledger_row["status"] == "historical"
    return render_template(
        "ledger/day_detail.html",
        ledger_row=ledger_row,
        entries=entries,
        is_historical=is_historical,
        today=today_str(),
    )
