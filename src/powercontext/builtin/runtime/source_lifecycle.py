# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Dependency-ordered Source lifecycle state.

Every piece of generated Memory or Topic Memory is registered against the Sources
that support it. Withdrawing a Source must stop derived artifacts from reaching
new generation while leaving audit reads intact, and restoring it must bring the
whole subtree back without rewriting any content.

This module owns only the dependency graph, the activation policy and its durable
storage. Applying the graph to the Memory/Topic read paths belongs to the runtime
application layer, which is why nothing here imports the runtime.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.exc import IntegrityError

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE
from powercontext.sources import SourceRef

LifecycleKind: TypeAlias = Literal["source", "memory", "topic"]
LifecyclePolicy: TypeAlias = Literal["all", "any"]
LifecyclePurpose: TypeAlias = Literal["generation", "audit"]
LifecycleCitation: TypeAlias = SourceRef | MemoryCitation | ArtifactRef

LIFECYCLE_SCHEMA = "powercontext.source-lifecycle.v1"

_CITATION_TYPES: dict[str, type[SourceRef] | type[MemoryCitation] | type[ArtifactRef]] = {
    "source": SourceRef,
    "memory": MemoryCitation,
    "topic": ArtifactRef,
}


class SourceLifecycleError(RuntimeError):
    """Reject a lifecycle operation that would corrupt the dependency graph."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SourceLifecycleEvent(BaseModel):
    """One ordered availability observation for a registered root Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    sequence: int = Field(ge=1)
    available: bool
    reason: str = Field(min_length=1)


class LifecycleNode(BaseModel):
    """One registered node of the evidence dependency graph."""

    model_config = ConfigDict(frozen=True)

    node_id: str = Field(min_length=1)
    kind: LifecycleKind
    revision: int = Field(ge=1)
    parents: tuple[str, ...] = ()
    policy: LifecyclePolicy | None = None
    available: bool = True
    sequence: int = Field(default=0, ge=0)
    citation: LifecycleCitation


def active_nodes(nodes: tuple[LifecycleNode, ...]) -> frozenset[str]:
    """Return the fixpoint of active nodes for one dependency graph.

    Roots are active while available. A derived node is active when its policy is
    satisfied by active parents, evaluated to a fixpoint so multi-level chains are
    resolved regardless of registration order.
    """

    active: set[str] = {node.node_id for node in nodes if node.kind == "source" and node.available}
    derived = tuple(node for node in nodes if node.kind != "source")
    changed = True
    while changed:
        changed = False
        for node in derived:
            if node.node_id in active:
                continue
            support = tuple(parent in active for parent in node.parents)
            satisfied = all(support) if node.policy == "all" else any(support)
            if satisfied:
                active.add(node.node_id)
                changed = True
    return frozenset(active)


def _detect_cycle(nodes: tuple[LifecycleNode, ...]) -> str | None:
    """Return a node participating in a cycle, if any."""

    edges = {node.node_id: node.parents for node in nodes}
    for origin in edges:
        seen: set[str] = set()
        stack = list(edges.get(origin, ()))
        while stack:
            current = stack.pop()
            if current == origin:
                return origin
            if current in seen:
                continue
            seen.add(current)
            stack.extend(edges.get(current, ()))
    return None


def register_node(
    nodes: tuple[LifecycleNode, ...],
    node: LifecycleNode,
) -> tuple[LifecycleNode, ...]:
    """Register one derived node after validating identity, parents and acyclicity."""

    if role := next((existing for existing in nodes if existing.node_id == node.node_id), None):
        if role.kind == "source":
            raise SourceLifecycleError("node-is-a-root")
        raise SourceLifecycleError("duplicate-node")
    if node.kind == "source":
        raise SourceLifecycleError("source-registration-requires-root")
    if node.policy is None:
        raise SourceLifecycleError("missing-policy")
    if not node.parents:
        raise SourceLifecycleError("missing-parents")
    if len(set(node.parents)) != len(node.parents):
        raise SourceLifecycleError("duplicate-parent")
    if node.node_id in node.parents:
        raise SourceLifecycleError("self-referential-node")
    known = {existing.node_id for existing in nodes}
    unknown = next((parent for parent in node.parents if parent not in known), None)
    if unknown is not None:
        raise SourceLifecycleError("unknown-parent")
    extended = (*nodes, node)
    if _detect_cycle(extended) is not None:
        raise SourceLifecycleError("dependency-cycle")
    return extended


class SourceLifecycleStore:
    """Persist one Scope's dependency graph as a single canonical document."""

    def __init__(self, database: object) -> None:
        self._database = database

    @property
    def database(self) -> object:
        return self._database

    async def load(
        self,
        connection: AsyncConnection,
        scope_id: str,
    ) -> tuple[tuple[LifecycleNode, ...], int]:
        row = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_TABLE.c.state, SOURCE_LIFECYCLE_TABLE.c.generation).where(
                    SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id
                )
            )
        ).first()
        if row is None:
            return (), 0
        return decode_nodes(row[0]), int(row[1])

    async def save(
        self,
        connection: AsyncConnection,
        scope_id: str,
        nodes: tuple[LifecycleNode, ...],
        generation: int,
    ) -> int:
        payload = encode_nodes(nodes)
        if generation == 0:
            try:
                await connection.execute(
                    insert(SOURCE_LIFECYCLE_TABLE).values(
                        scope_id=scope_id,
                        state=payload,
                        generation=1,
                    )
                )
                return 1
            except IntegrityError:
                # A concurrent runtime inserted the first document; fall through to update.
                pass
        result = await connection.execute(
            update(SOURCE_LIFECYCLE_TABLE)
            .where(SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id)
            .values(state=payload, generation=generation + 1)
        )
        if result.rowcount == 0:
            await connection.execute(
                insert(SOURCE_LIFECYCLE_TABLE).values(
                    scope_id=scope_id,
                    state=payload,
                    generation=1,
                )
            )
            return 1
        return generation + 1


def encode_nodes(nodes: tuple[LifecycleNode, ...]) -> bytes:
    payload = {
        "schema": LIFECYCLE_SCHEMA,
        "nodes": [
            {
                "node_id": node.node_id,
                "kind": node.kind,
                "revision": node.revision,
                "parents": list(node.parents),
                "policy": node.policy,
                "available": node.available,
                "sequence": node.sequence,
                "citation": node.citation.model_dump(mode="json"),
            }
            for node in nodes
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def decode_nodes(payload: object) -> tuple[LifecycleNode, ...]:
    if not isinstance(payload, bytes | bytearray | memoryview):
        raise SourceLifecycleError("invalid-stored-state")
    try:
        document = json.loads(bytes(payload).decode())
    except (UnicodeDecodeError, ValueError) as error:
        raise SourceLifecycleError("invalid-stored-state") from error
    if not isinstance(document, dict) or document.get("schema") != LIFECYCLE_SCHEMA:
        raise SourceLifecycleError("invalid-stored-state")
    rows = document.get("nodes")
    if not isinstance(rows, list):
        raise SourceLifecycleError("invalid-stored-state")
    decoded: list[LifecycleNode] = []
    for row in rows:
        try:
            kind = row["kind"]
            citation_type = _CITATION_TYPES[kind]
            decoded.append(
                LifecycleNode(
                    node_id=row["node_id"],
                    kind=kind,
                    revision=row["revision"],
                    parents=tuple(row.get("parents") or ()),
                    policy=row.get("policy"),
                    available=bool(row.get("available", True)),
                    sequence=row.get("sequence", 0),
                    citation=citation_type.model_validate(row["citation"]),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SourceLifecycleError("invalid-stored-state") from error
    return tuple(decoded)


class ScopedSourceLifecycle:
    """Register evidence dependencies and apply their activation to one Scope.

    The runtime is received by duck type on purpose: this module must stay importable
    from the runtime application layer without creating an import cycle.
    """

    def __init__(self, runtime: object, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = scope_id

    # ---- registration -------------------------------------------------

    async def register_source(self, node_id: str, citation: SourceRef, *, revision: int) -> None:
        node = LifecycleNode(node_id=node_id, kind="source", revision=revision, citation=citation)

        def add(nodes: tuple[LifecycleNode, ...]) -> tuple[LifecycleNode, ...]:
            if any(existing.node_id == node_id for existing in nodes):
                raise SourceLifecycleError("duplicate-node")
            return (*nodes, node)

        await self._mutate(add)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: tuple[str, ...],
        policy: LifecyclePolicy,
    ) -> None:
        node = LifecycleNode(
            node_id=node_id,
            kind="memory",
            revision=1,
            parents=tuple(parents),
            policy=policy,
            citation=citation,
        )
        await self._mutate(lambda nodes: register_node(nodes, node))

    async def register_topic(
        self,
        node_id: str,
        citation: ArtifactRef,
        *,
        parents: tuple[str, ...],
        policy: LifecyclePolicy,
    ) -> None:
        node = LifecycleNode(
            node_id=node_id,
            kind="topic",
            revision=1,
            parents=tuple(parents),
            policy=policy,
            citation=citation,
        )
        await self._mutate(lambda nodes: register_node(nodes, node))

    # ---- observation --------------------------------------------------

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        async with self._runtime._context(self.scope_id) as context:  # type: ignore[attr-defined]
            async with self._runtime._locked(self.scope_id):  # type: ignore[attr-defined]
                async with self._store.database.transaction() as connection:  # type: ignore[union-attr]
                    nodes, generation = await self._store.load(connection, self.scope_id)
                    index = {node.node_id: node for node in nodes}
                    node = index.get(event.node_id)
                    if node is None:
                        raise SourceLifecycleError("unknown-node")
                    if node.kind != "source":
                        raise SourceLifecycleError("not-a-root")
                    if event.revision != node.revision:
                        raise SourceLifecycleError("revision-mismatch")
                    if event.sequence < node.sequence:
                        raise SourceLifecycleError("stale-sequence")
                    if event.sequence == node.sequence:
                        if event.available != node.available:
                            raise SourceLifecycleError("conflicting-observation")
                        # An identical observation is an idempotent replay: report the
                        # current activation without rewriting Memory or the graph.
                        return sorted(active_nodes(nodes))
                    updated = tuple(
                        item.model_copy(
                            update={"available": event.available, "sequence": event.sequence},
                        )
                        if item.node_id == node.node_id
                        else item
                        for item in nodes
                    )
                    active = active_nodes(updated)
                    await self._store.save(connection, self.scope_id, updated, generation)
                await self._apply_memory(context, updated, active)
        return sorted(active)

    async def resolve(self, node_id: str, *, purpose: LifecyclePurpose) -> LifecycleCitation:
        nodes = await self._load()
        node = next((item for item in nodes if item.node_id == node_id), None)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == "audit":
            return node.citation
        if node.node_id not in active_nodes(nodes):
            raise SourceLifecycleError("inactive-node")
        return node.citation

    # ---- internals ----------------------------------------------------

    @property
    def _store(self) -> SourceLifecycleStore:
        return self._runtime._source_lifecycle_store  # type: ignore[attr-defined]

    async def _load(self) -> tuple[LifecycleNode, ...]:
        async with self._store.database.transaction() as connection:  # type: ignore[union-attr]
            nodes, _ = await self._store.load(connection, self.scope_id)
        return nodes

    async def _mutate(
        self,
        change: "Callable[[tuple[LifecycleNode, ...]], tuple[LifecycleNode, ...]]",
    ) -> None:
        async with self._runtime._context(self.scope_id) as context:  # type: ignore[attr-defined]
            async with self._runtime._locked(self.scope_id):  # type: ignore[attr-defined]
                async with self._store.database.transaction() as connection:  # type: ignore[union-attr]
                    nodes, generation = await self._store.load(connection, self.scope_id)
                    updated = change(nodes)
                    active = active_nodes(updated)
                    await self._store.save(connection, self.scope_id, updated, generation)
                # A node registered under already-withdrawn evidence must leave
                # generation immediately, so registration mirrors activation too.
                await self._apply_memory(context, updated, active)

    async def _apply_memory(
        self,
        context: object,
        nodes: tuple[LifecycleNode, ...],
        active: frozenset[str],
    ) -> None:
        """Mirror the graph's activation onto the exact Memory entries it names."""

        memory_nodes = tuple(node for node in nodes if node.kind == "memory")
        if not memory_nodes:
            return
        service = context.artifacts.memory  # type: ignore[attr-defined]
        artifact_id = context.artifacts.memory_artifact_id  # type: ignore[attr-defined]
        for node in memory_nodes:
            memory = await _head_or_none(service, artifact_id)
            if memory is None:
                continue
            citation = node.citation
            entries = await service.entries(memory)
            target = next((entry for entry in entries if entry.entry_id == citation.entry_id), None)
            if target is None:
                continue
            state = next(
                (
                    item.state
                    for item in memory.content.manifest.entries
                    if item.entry_id == target.entry_id and item.entry_version_id == target.entry_version_id
                ),
                None,
            )
            want_active = node.node_id in active
            if want_active and state != "active":
                await service.reactivate(memory, entries=(target,))
            elif not want_active and state == "active":
                await service.forget(memory, entries=(target,), reason="source-lifecycle")


async def _head_or_none(service: object, artifact_id: str) -> object | None:
    from powercontext.errors import ArtifactNotFoundError

    try:
        return await service.head(artifact_id)  # type: ignore[attr-defined]
    except ArtifactNotFoundError:
        return None


class SourceLifecycleApplication:
    """Select the lifecycle service for one Scope."""

    def __init__(self, runtime: object) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, scope_id)

