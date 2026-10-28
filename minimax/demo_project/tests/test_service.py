"""回归测试：这些行为在本次修改前后都必须保持不变。

SPEC-001 的 R1（状态）、R3（最低优先级）、R4（条件交集）、R5（顺序）由本文件覆盖。
"""

from __future__ import annotations

import pytest

from taskboard.models import Board, Priority, Task
from taskboard.service import filter_tasks, summarize


@pytest.fixture()
def board() -> Board:
    b = Board()
    b.add(Task(id="T-1", title="修复登录页空指针", tags=("bug", "frontend"), priority=Priority.HIGH))
    b.add(Task(id="T-2", title="补充筛选接口文档", tags=("docs",), priority=Priority.LOW))
    b.add(Task(id="T-3", title="重构任务仓库层", tags=("refactor", "backend"), priority=Priority.URGENT))
    b.add(Task(id="T-4", title="修复导出乱码", tags=("bug", "backend"), priority=Priority.NORMAL, done=True))
    return b


def test_status_all_returns_everything(board: Board) -> None:
    assert len(filter_tasks(board.all(), status="all")) == 4


def test_status_open_excludes_done(board: Board) -> None:
    ids = [t.id for t in filter_tasks(board.all(), status="open")]
    assert ids == ["T-1", "T-2", "T-3"]


def test_status_done_returns_only_done(board: Board) -> None:
    ids = [t.id for t in filter_tasks(board.all(), status="done")]
    assert ids == ["T-4"]


def test_min_priority_is_inclusive(board: Board) -> None:
    ids = [t.id for t in filter_tasks(board.all(), min_priority=Priority.HIGH)]
    assert ids == ["T-1", "T-3"]


def test_status_and_min_priority_intersect(board: Board) -> None:
    ids = [t.id for t in filter_tasks(board.all(), status="open", min_priority=Priority.HIGH)]
    assert ids == ["T-1", "T-3"]


def test_unknown_status_is_rejected(board: Board) -> None:
    with pytest.raises(ValueError):
        filter_tasks(board.all(), status="archived")


def test_order_follows_input(board: Board) -> None:
    ids = [t.id for t in filter_tasks(board.all(), status="open")]
    assert ids == ["T-1", "T-2", "T-3"], "筛选不得改变入参顺序"


def test_summarize_counts(board: Board) -> None:
    assert summarize(board.all()) == {"total": 4, "done": 1, "open": 3}
