"""目标功能测试：SPEC-001 R2（标签筛选）。

本文件在本次修改前必须是失败的（断言失败，而非导入/启动错误），
修改后必须全部通过；它与 ``test_service.py`` 共同构成本次验收的测试集，
评审会在修改前后执行同一份测试集。
"""

from __future__ import annotations

import pytest

from taskboard.models import Board, Priority, Task
from taskboard.service import filter_tasks


@pytest.fixture()
def board() -> Board:
    b = Board()
    b.add(Task(id="T-1", title="修复登录页空指针", tags=("bug", "frontend"), priority=Priority.HIGH))
    b.add(Task(id="T-2", title="补充筛选接口文档", tags=("docs",), priority=Priority.LOW))
    b.add(Task(id="T-3", title="重构任务仓库层", tags=("refactor", "backend"), priority=Priority.URGENT))
    b.add(Task(id="T-4", title="修复导出乱码", tags=("bug", "backend"), priority=Priority.NORMAL, done=True))
    return b


def test_filters_by_tag(board: Board) -> None:
    """R2：只保留 tags 中包含该标签的任务。"""

    ids = [t.id for t in filter_tasks(board.all(), tag="bug")]
    assert ids == ["T-1", "T-4"], "tag=bug 应只命中 T-1 与 T-4"


def test_tag_match_is_case_insensitive(board: Board) -> None:
    """R2：标签匹配不区分大小写。"""

    ids = [t.id for t in filter_tasks(board.all(), tag="BUG")]
    assert ids == ["T-1", "T-4"], "tag=BUG 应与 tag=bug 命中相同任务"


def test_tag_is_trimmed(board: Board) -> None:
    """R2：标签两侧空白不影响匹配。"""

    ids = [t.id for t in filter_tasks(board.all(), tag="  bug  ")]
    assert ids == ["T-1", "T-4"]


def test_blank_tag_means_no_filter(board: Board) -> None:
    """R2：空白字符串视为不筛选。"""

    assert len(filter_tasks(board.all(), tag="   ")) == 4
    assert len(filter_tasks(board.all(), tag="")) == 4


def test_tag_combines_with_status_and_priority(board: Board) -> None:
    """R4：标签与状态、优先级条件取交集。"""

    ids = [t.id for t in filter_tasks(board.all(), status="open", tag="backend")]
    assert ids == ["T-3"], "open + backend 只有 T-3"

    ids = [
        t.id
        for t in filter_tasks(board.all(), tag="bug", min_priority=Priority.HIGH)
    ]
    assert ids == ["T-1"], "bug + HIGH 以上只有 T-1"


def test_tag_not_matched_returns_empty(board: Board) -> None:
    """R2：没有任务带该标签时返回空列表。"""

    assert filter_tasks(board.all(), tag="nonexistent") == []
