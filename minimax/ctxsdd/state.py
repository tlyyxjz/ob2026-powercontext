"""用 PowerContext 持久化任务状态，并在后续运行中读取复用。

每一次 ``save`` 都向同一 Scope 的 Memory 追加一条 ``task-state`` 条目；
``load`` 取该任务版本号最大的那条，因此模块重启后仍能读回最近状态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from powercontext.builtin.artifacts.memory.models import MemoryEntryInput
from powercontext.builtin.persistence.sqlite.profile import SQLiteConfig
from powercontext.builtin.runtime.composition import open_builtin_contexts
from powercontext.builtin.runtime.config import BuiltinConfig
from powercontext.errors import ArtifactNotFoundError

from ctxsdd.config import Settings

KIND = "task-state"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class TaskStateStore:
    """基于 PowerContext Memory 的任务状态存取。"""

    settings: Settings

    def _config(self) -> BuiltinConfig:
        return BuiltinConfig(
            database=SQLiteConfig(url=self.settings.sqlite_url),
        )

    @staticmethod
    def scope_for(task_id: str) -> str:
        """一个任务一个 Scope，便于按任务隔离状态。"""

        return f"ctxsdd/{task_id}"

    async def save(self, task_id: str, state: dict[str, Any]) -> dict[str, Any]:
        """追加一条任务状态快照，返回写入后的条目标识。"""

        payload = {
            "task_id": task_id,
            "updated_at": _now(),
            **state,
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        async with open_builtin_contexts(self._config()) as contexts:
            context = await contexts.get(self.scope_for(task_id))
            memory = context.artifacts.memory
            artifact_id = context.artifacts.memory_artifact_id

            # 第一次保存时 Memory Artifact 还不存在（memory=None 表示新建）；
            # 之后必须基于当前最新 revision 追加，否则会触发版本冲突。
            try:
                head, _ = await memory.head_entries(artifact_id)
            except ArtifactNotFoundError:
                head = None

            await memory.remember(
                memory=head,
                entries=[MemoryEntryInput(kind=KIND, text=text)],
                mode="append",
            )
            _, entries = await memory.head_entries(artifact_id)

        latest = max(
            (e for e in entries if e.kind == KIND),
            key=lambda e: e.created_in_revision,
            default=None,
        )
        return {
            "scope": self.scope_for(task_id),
            "artifact_id": artifact_id,
            "entries_total": len(entries),
            "revision": latest.created_in_revision if latest else None,
        }

    async def load(self, task_id: str) -> dict[str, Any] | None:
        """读回该任务最近一次保存的状态；无记录时返回 ``None``。

        注意：任务在**从空状态开始时**必然没有 Memory Artifact，
        PowerContext 会抛 ``ArtifactNotFoundError``。这里把它当作
        「尚无状态」处理，而不是错误，这样第一次运行也能正常进入后续流程。
        """

        async with open_builtin_contexts(self._config()) as contexts:
            context = await contexts.get(self.scope_for(task_id))
            memory = context.artifacts.memory
            artifact_id = context.artifacts.memory_artifact_id
            try:
                _, entries = await memory.head_entries(artifact_id)
            except ArtifactNotFoundError:
                return None

        candidates = [e for e in entries if e.kind == KIND]
        if not candidates:
            return None
        # 注意：MemoryEntryVersion.version 是「该 entry 自己的版本号」，
        # 不是写入序号；跨 entry 排序必须用 created_in_revision。
        latest = max(candidates, key=lambda e: e.created_in_revision)
        data = json.loads(latest.text)
        data["_revision"] = latest.created_in_revision
        data["_entries_in_scope"] = len(entries)
        return data

    async def history(self, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """按版本升序返回该任务的历史状态，用于观察进展轨迹。"""

        async with open_builtin_contexts(self._config()) as contexts:
            context = await contexts.get(self.scope_for(task_id))
            memory = context.artifacts.memory
            artifact_id = context.artifacts.memory_artifact_id
            try:
                _, entries = await memory.head_entries(artifact_id)
            except ArtifactNotFoundError:
                return []

        rows = []
        for entry in sorted(
            (e for e in entries if e.kind == KIND), key=lambda e: e.created_in_revision
        ):
            try:
                rows.append(json.loads(entry.text))
            except json.JSONDecodeError:
                continue
        return rows[-limit:]


def append_state_log(settings: Settings, task_id: str, action: str, payload: dict[str, Any]) -> Path:
    """把状态读写追加到人类可读的记录文件（供「技术使用记录」核对）。"""

    settings.records_dir.mkdir(parents=True, exist_ok=True)
    path = settings.records_dir / "powercontext_state.log"
    line = json.dumps(
        {"at": _now(), "task_id": task_id, "action": action, **payload},
        ensure_ascii=False,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return path
