"""TaskBoard 的查询服务。

筛选语义由 ``specs/SPEC-001-task-filter.md`` 定义；本文件是它的实现。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from taskboard.models import Priority, Task


def filter_tasks(
    tasks: Iterable[Task],
    *,
    status: str = "all",
    tag: str | None = None,
    min_priority: Priority | int | None = None,
) -> list[Task]:
    """按状态、标签、最低优先级筛选任务。

    参数：
        tasks: 待筛选的任务集合，保持入参顺序。
        status: ``"all"`` / ``"open"`` / ``"done"``。
        tag: 单个标签名；``None`` 表示不按标签筛选。
        min_priority: 最低优先级（含），``None`` 表示不限。

    返回：
        满足全部条件的任务列表，顺序与入参一致。
    """

    if status not in {"all", "open", "done"}:
        raise ValueError(f"unsupported status: {status!r}")

    floor: int | None = None
    if min_priority is not None:
        floor = int(Priority(min_priority))

    selected: list[Task] = []
    for task in tasks:
        if status == "open" and task.done:
            continue
        if status == "done" and not task.done:
            continue
        if floor is not None and int(task.priority) < floor:
            continue
        # TODO(SPEC-001#R2): 标签筛选尚未实现，当前忽略 tag 参数。
        selected.append(task)

    return selected


def summarize(tasks: Sequence[Task]) -> dict[str, int]:
    """统计任务总数、未完成数与已完成数。"""

    total = len(tasks)
    done = sum(1 for task in tasks if task.done)
    return {"total": total, "done": done, "open": total - done}
