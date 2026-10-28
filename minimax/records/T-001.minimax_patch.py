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

    normalized_tag = tag.strip().casefold() if tag is not None else None

    selected: list[Task] = []
    for task in tasks:
        if status == "open" and task.done:
            continue
        if status == "done" and not task.done:
            continue
        if floor is not None and int(task.priority) < floor:
            continue
        if normalized_tag and not any(
            task_tag.casefold() == normalized_tag for task_tag in task.tags
        ):
            continue
        selected.append(task)

    return selected
