"""进程内清账并发门闩。

需求要区分两种情形，错误码不同：
- 两人**同刻**点不同日期的清账：只许最旧一天成功，其余失败写「顺序锁」。
- 单人**先后**点（前一次已完结）非最旧日：失败写「跳日」。

仅靠 SQLite 行锁无法区分二者（谁先拿到锁取决于调度），而且「先到先得」也不
对——先到的可能是较新一天。这里用线程原语实现确定性门闩，关键是「并发组」由
调用方显式登记，且每个成员在同步点之前就登记好自己的**目标日期**：

- 并发测试用 :class:`ConcurrencyBarrier` 让 N 个线程先 ``register(target_day)``
  进同一组、再在 barrier 上对齐同时放行。门闩对组内**目标日最旧**的那个请求
  放行（它随后走顺序裁决并成功），同组其余请求一律判「顺序锁」，与线程谁先
  谁后完全无关。
- 普通单点请求不属于任何并发组，门闩直接放行给顺序裁决；清非最旧日时由顺序
  门闩判「跳日」。
- 只要还有 running 清账任务，任何新请求都判「顺序锁」。

Flask 默认多线程服务、测试线程与生产同步请求都在同一进程内，进程内锁足够。
"""

import threading


class ConcurrencyBarrier:
    """测试用：把若干线程（各带目标日期）登记为同一并发组并在同刻放行。"""

    def __init__(self, parties: int):
        self._barrier = threading.Barrier(parties)
        self._lock = threading.Lock()
        # 线程 id -> 目标日期
        self._days: dict[int, str] = {}

    def register(self, target_day: str) -> int:
        """当前线程带目标日期加入并发组，返回组 id。"""
        tid = threading.get_ident()
        with self._lock:
            self._days[tid] = target_day
            return id(self)

    def day_of(self, tid: int) -> str | None:
        with self._lock:
            return self._days.get(tid)

    def all_days(self) -> list[str]:
        with self._lock:
            return list(self._days.values())

    def contains(self, tid: int) -> bool:
        with self._lock:
            return tid in self._days

    def wait(self) -> None:
        self._barrier.wait()

    def disband(self) -> None:
        with self._lock:
            self._days.clear()


class _SettlementGate:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._active_groups: dict[int, ConcurrencyBarrier] = {}
        self._admitted: dict[int, int] = {}   # 组 id -> 已放行线程 id
        self._running_task_id = None          # 当前在跑的清账任务

    # ---------------------------------------------------------- 并发组
    def register_group(self, barrier: ConcurrencyBarrier) -> None:
        with self._cond:
            self._active_groups[id(barrier)] = barrier
            self._admitted.pop(id(barrier), None)

    def disband_group(self, barrier: ConcurrencyBarrier) -> None:
        with self._cond:
            self._active_groups.pop(id(barrier), None)
            self._admitted.pop(id(barrier), None)

    def _group_of_thread(self) -> tuple[int, ConcurrencyBarrier] | None:
        tid = threading.get_ident()
        for gid, barrier in self._active_groups.items():
            if barrier.contains(tid):
                return gid, barrier
        return None

    # ---------------------------------------------------------- 裁决
    def check_concurrent(self, target_day: str) -> bool:
        """该清账请求是否应被「顺序锁」拦截。

        - 已有 running 清账任务 → True；
        - 属于某并发组：只有目标日等于组内最旧日的请求放行，其余 True；
        - 普通单点请求 → False（交给顺序门闩裁决跳日）。
        """
        with self._cond:
            if self._running_task_id is not None:
                return True
            found = self._group_of_thread()
            if found is None:
                return False
            gid, barrier = found
            oldest = min(barrier.all_days())
            tid = threading.get_ident()
            if target_day != oldest:
                # 组内较新的一天：顺序锁，只许最旧日成功
                return True
            # 目标日即组内最旧；同组最旧日只放一个（同日并列时先到先得）
            if gid in self._admitted:
                return True
            self._admitted[gid] = tid
            return False

    # ---------------------------------------------------------- 任务状态
    def task_started(self, task_id: int) -> None:
        with self._cond:
            self._running_task_id = task_id
            self._cond.notify_all()

    def task_finished(self) -> None:
        with self._cond:
            self._running_task_id = None
            self._cond.notify_all()

    def running_task_id(self):
        with self._cond:
            return self._running_task_id

    def is_locked(self) -> bool:
        with self._cond:
            return self._running_task_id is not None

    def reset(self) -> None:
        """测试隔离用：清空门闩。"""
        with self._cond:
            self._active_groups.clear()
            self._admitted.clear()
            self._running_task_id = None


# 模块级单例
settlement_gate = _SettlementGate()
