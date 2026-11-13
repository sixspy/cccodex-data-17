"""两人同时点不同日期清账的真并发测试。

两个线程先登记进同一个并发组，再在 barrier 上对齐到同一刻发起
start_settlement，验证只有最旧一天成功，其余失败写「顺序锁」。
判定基于线程组成员身份，不依赖调度时间窗，结果确定。
"""

import sqlite3
import threading

from app import create_app
from app.services.ledger import LedgerError, start_settlement
from app.services.settlement_gate import ConcurrencyBarrier, settlement_gate

PINNED_TODAY = "2026-11-13"


def _worker(db_path, pinned_today, day, method, barrier, outcomes, idx):
    app = create_app()
    app.config["DATABASE"] = db_path
    app.config["TEST_BACKOFFICE_DATE"] = pinned_today
    with app.app_context():
        barrier.register(day)    # 带目标日期加入同一并发组
        barrier.wait()           # 对齐到同一刻
        try:
            task = start_settlement(day, method)
            outcomes[idx] = {"ok": True, "day": task["target_day"]}
        except LedgerError as e:
            outcomes[idx] = {"ok": False, "day": day, "reason": e.reason}
        except sqlite3.OperationalError as e:
            # 锁等待超时也算并发失败；门闩设计应在超时前给出领域原因
            outcomes[idx] = {"ok": False, "day": day, "reason": f"db-lock:{e}"}


def run_concurrent_settlement(db_path, older_day, newer_day, pinned_today=PINNED_TODAY):
    """两个线程同刻分别发起 older_day / newer_day 的清账。"""
    barrier = ConcurrencyBarrier(2)
    settlement_gate.register_group(barrier)
    outcomes = [None, None]
    method = "merge_today"
    t1 = threading.Thread(
        target=_worker,
        args=(db_path, pinned_today, older_day, method, barrier, outcomes, 0),
    )
    t2 = threading.Thread(
        target=_worker,
        args=(db_path, pinned_today, newer_day, method, barrier, outcomes, 1),
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    settlement_gate.disband_group(barrier)
    barrier.disband()
    return outcomes
