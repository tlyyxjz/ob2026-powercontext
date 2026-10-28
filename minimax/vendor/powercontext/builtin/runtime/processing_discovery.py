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

"""Bounded reconciliation of Source-driven Family progress at startup."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.supervision import ArtifactProcessingFence, ArtifactProcessingLeaseRepository
from powercontext.builtin.persistence.tables import (
    PROFILE_POLICIES_TABLE,
    SOURCES_TABLE,
    SOURCE_JOURNAL_HEADS_TABLE,
)


async def enabled_profile_scopes(connection: AsyncConnection, scope_ids: tuple[str, ...]) -> frozenset[str]:
    """Qualify one bounded automatic page before accepting any invocation.

    Hold enabled Policy rows through admission so concurrent policy changes
    serialize with the decision. This reads metadata only, before any Worker
    authorization, model construction, or retry state exists for the page.
    """

    if not scope_ids:
        return frozenset()
    table = PROFILE_POLICIES_TABLE
    rows = await connection.scalars(
        select(table.c.scope_id)
        .where(table.c.scope_id.in_(scope_ids), table.c.generation_enabled.is_(True))
        .with_for_update()
    )
    return frozenset(rows)


def _pending_journal_count(scope_id, consumed_sequence: int):
    """Count persisted journal identities after one consumed sequence.

    Positions are unique and dense per Scope, so this is a journal-identity
    count: a replayed capture or a receipt trust upgrade adds no position, and a
    lineage-only Source occupies exactly one. Bodies are never read.

    The statement is returned directly rather than as a scalar subquery: a
    ``ScalarSelect`` is not executable, so ``connection.scalar`` rejects it.
    """

    table = SOURCES_TABLE
    return select(func.count()).select_from(table).where(
        table.c.scope_id == scope_id, table.c.journal_position > consumed_sequence
    )


class SourceProcessingPendingProvider:
    """Inspect journal metadata, never Source bodies, prompts, or models."""

    def __init__(self, database: AsyncDatabase, binding_name: str, artifact_family: str) -> None:
        self._database = database
        self._binding = binding_name
        self._family = artifact_family

    async def reconcile(self, after_scope_id: str | None, limit: int, *, fence: ArtifactProcessingFence) -> str | None:
        heads = SOURCE_JOURNAL_HEADS_TABLE
        statement = select(heads.c.scope_id, heads.c.position).order_by(heads.c.scope_id).limit(limit)
        if after_scope_id is not None:
            statement = statement.where(heads.c.scope_id > after_scope_id)
        if self._family == "profile":
            statement = statement.join(PROFILE_POLICIES_TABLE, PROFILE_POLICIES_TABLE.c.scope_id == heads.c.scope_id)
            statement = statement.where(PROFILE_POLICIES_TABLE.c.generation_enabled.is_(True))
        async with self._database.transaction() as connection:
            await ArtifactProcessingLeaseRepository().require_fence(connection, fence)
            rows = (await connection.execute(statement)).all()
            intents = ArtifactProcessingIntentRepository()
            cursors = SourceCursorRepository()
            for scope, high in rows:
                cursor = await cursors.load(connection, scope, self._binding)
                if int(high) <= (0 if cursor is None else cursor.cursor.sequence):
                    continue
                intent = await intents.load(connection, scope, self._binding, for_update=True)
                if intent is None or intent.dirty_generation == intent.clean_generation:
                    await intents.mark_dirty(connection, scope, self._binding)
        return str(rows[-1][0]) if len(rows) == limit else None


class CountAdmissionPendingProvider(SourceProcessingPendingProvider):
    """Publish only the Scopes whose pending journal identities reached the threshold.

    Counting uses the journal position of already persisted identities, so a
    replayed capture or a receipt trust upgrade never adds a pending identity,
    and a lineage-only Source occupies exactly one position. Bodies, prompts and
    models are never read here. The accepted invocation is the ordinary one; a
    successful cursor advance removes the consumed positions from the count.
    """

    def __init__(
        self,
        database: AsyncDatabase,
        binding_name: str,
        artifact_family: str,
        threshold: int,
    ) -> None:
        super().__init__(database, binding_name, artifact_family)
        if type(threshold) is not int or threshold < 1:
            raise ValueError("count threshold must be a positive integer")  # noqa: TRY003
        self._threshold = threshold

    async def reconcile(self, after_scope_id: str | None, limit: int, *, fence: ArtifactProcessingFence) -> str | None:
        heads = SOURCE_JOURNAL_HEADS_TABLE
        statement = select(heads.c.scope_id, heads.c.position).order_by(heads.c.scope_id).limit(limit)
        if after_scope_id is not None:
            statement = statement.where(heads.c.scope_id > after_scope_id)
        if self._family == "profile":
            statement = statement.join(PROFILE_POLICIES_TABLE, PROFILE_POLICIES_TABLE.c.scope_id == heads.c.scope_id)
            statement = statement.where(PROFILE_POLICIES_TABLE.c.generation_enabled.is_(True))
        async with self._database.transaction() as connection:
            await ArtifactProcessingLeaseRepository().require_fence(connection, fence)
            rows = (await connection.execute(statement)).all()
            cursors = SourceCursorRepository()
            intents = ArtifactProcessingIntentRepository()
            for scope, high in rows:
                cursor = await cursors.load(connection, scope, self._binding)
                consumed = 0 if cursor is None else cursor.cursor.sequence
                pending = int(
                    await connection.scalar(_pending_journal_count(scope, int(consumed))) or 0
                )
                # Below the threshold keep the pending input untouched: it stays
                # available for a later capture, the time condition, or flush.
                if pending < self._threshold:
                    continue
                intent = await intents.load(connection, scope, self._binding, for_update=True)
                if intent is None or intent.dirty_generation == intent.clean_generation:
                    await intents.mark_dirty(connection, scope, self._binding)
        return str(rows[-1][0]) if len(rows) == limit else None
