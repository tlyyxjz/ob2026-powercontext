"""TaskBoard —— 一个最小但完整的任务管理领域模型。

本模块只提供数据结构与不变量校验，不含任何筛选逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Priority(IntEnum):
    """任务优先级，数值越大越紧急。"""

    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4


@dataclass(frozen=True)
class Task:
    """一条任务。

    不变量：
    - ``id`` 必填且非空；
    - ``title`` 必填且非空；
    - ``tags`` 内部不允许出现空白标签，统一存为小写（写入侧规范化）；
    - ``priority`` 必须落在 :class:`Priority` 的取值范围内。
    """

    id: str
    title: str
    tags: tuple[str, ...] = ()
    priority: Priority = Priority.NORMAL
    done: bool = False

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("Task.id must be a non-empty string")
        if not self.title or not self.title.strip():
            raise ValueError("Task.title must be a non-empty string")
        if not Priority.LOW <= Priority(self.priority) <= Priority.URGENT:
            raise ValueError(f"Task.priority out of range: {self.priority!r}")
        if any(not str(tag).strip() for tag in self.tags):
            raise ValueError("Task.tags must not contain blank tags")

    def with_done(self, done: bool) -> "Task":
        """返回完成状态被改写后的副本。"""

        return Task(
            id=self.id,
            title=self.title,
            tags=self.tags,
            priority=self.priority,
            done=done,
        )


@dataclass
class Board:
    """内存中的任务集合，保持插入顺序。"""

    _tasks: list[Task] = field(default_factory=list)

    def add(self, task: Task) -> Task:
        if any(existing.id == task.id for existing in self._tasks):
            raise ValueError(f"duplicate task id: {task.id}")
        self._tasks.append(task)
        return task

    def all(self) -> tuple[Task, ...]:
        return tuple(self._tasks)

    def __len__(self) -> int:
        return len(self._tasks)
