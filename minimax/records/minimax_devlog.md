
### 2026-10-28T04:06:17Z · 生成代码修改

- 任务：`T-001`
- 模型：`MiniMax-M3.1-Flash`
- 耗时：14.26s
- token：prompt=1438 completion=1663 total=3101
- 请求 ID：`0730a7abce51c64a6086f89c6b76aab2`
- 提供的上下文：
  - 规格条目: SPEC-001#R3@v2, SPEC-001#R2@v2, SPEC-001#R5@v2, SPEC-001#R1@v2, SPEC-001#R6@v2, SPEC-001#R4@v2
  - 代码位置: C:/Users/Lenovo/WorkBuddy/2026-09-03-22-46-59/pc-eco/minimax/demo_project/taskboard/service.py:13-50
  - 目标函数: filter_tasks
  - 覆盖测试: test_filters_by_tag, test_blank_tag_means_no_filter, test_tag_combines_with_status_and_priority, test_status_and_min_priority_intersect, test_tag_not_matched_returns_empty, test_tag_match_is_case_insensitive
  - 提示词长度: 3140 字符
- 输出已保存：`records/T-001.minimax_patch.py`

<details><summary>模型输出（原文）</summary>

```
```python
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
```
```

</details>
