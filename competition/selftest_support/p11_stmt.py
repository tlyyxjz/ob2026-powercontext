"""Count every statement a single hybrid search issues, and the channel sizes.

The published acceptance test only counts statements that mention BOTH
``pc_memory_entry_heads`` and ``pc_memory_vector_entries``, so a second, purely
lexical candidate statement is invisible to it. This probe counts everything and
records what the service actually receives from the composite index.
"""

from __future__ import annotations

import tempfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
from sqlalchemy import Engine, event

PROFILE = EmbeddingProfile(
    profile_id="p11-stmt-v1",
    model="deterministic",
    dimension=384,
    distance="l2",
    normalization="unit",
)

ENTRIES = 60
Statement = tuple[str, object]


def unit_vector(text: str) -> tuple[float, ...]:
    buckets = [0.0] * PROFILE.dimension
    for token in text.casefold().split():
        buckets[zlib.crc32(token.encode("utf8")) % PROFILE.dimension] += 1.0
    norm = sum(value * value for value in buckets) ** 0.5 or 1.0
    return tuple(value / norm for value in buckets)


class _Embedding:
    profile = PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(vectors=tuple(unit_vector(text) for text in texts))


@contextmanager
def _statements() -> Iterator[list[Statement]]:
    statements: list[Statement] = []

    def record(_connection, _cursor, statement, parameters, _context, _executemany) -> None:
        statements.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


CHANNELS: list[tuple[str, int, int]] = []
_ORIGINAL = CompositeMemoryIndex.search


async def _recording_search(self, connection, scope_id, request, /):
    channels = await _ORIGINAL(self, connection, scope_id, request)
    CHANNELS.append((request.mode, len(channels.fts), len(channels.vector)))
    return channels


def classify(statement: str) -> str:
    folded = statement.casefold()
    marks = []
    if "pc_memory_entry_heads" in folded:
        marks.append("heads")
    if "pc_memory_vector_entries" in folded:
        marks.append("vectors")
    if "against" in folded:
        marks.append("against")
    if "l2_distance" in folded:
        marks.append("l2")
    return "+".join(marks) or "-"


async def main() -> int:
    CompositeMemoryIndex.search = _recording_search
    database_path = Path(tempfile.mkdtemp(prefix="p11-stmt-")) / "db"
    entries = tuple(
        MemoryEntryInput(kind="fact", text=f"sharedtoken record number {index} about topic {index % 7}")
        for index in range(ENTRIES)
    )
    async with open_builtin_contexts(
        BuiltinConfig(database=SeekDBConfig(path=database_path)), embedding_model=_Embedding()
    ) as contexts:
        memory = (await contexts.get("project")).artifacts.memory
        head = await memory.remember(memory=None, entries=entries, mode="append")
        assert head is not None
        print(f"entries={ENTRIES} candidate_limit={max(10 * 4, 32)}", flush=True)

        CHANNELS.clear()
        with _statements() as statements:
            result = await memory.search("sharedtoken", memories=(head,), mode="hybrid", limit=10)

        print(f"total_statements={len(statements)}", flush=True)
        for position, (statement, _parameters) in enumerate(statements, start=1):
            collapsed = " ".join(statement.split())
            print(f"  [{position}] {classify(statement):22s} {collapsed[:70]}", flush=True)
        for mode, fts_size, vector_size in CHANNELS:
            print(f"service_received mode={mode} fts={fts_size} vector={vector_size}", flush=True)
        print(f"hits={len(result.hits)}", flush=True)
        print("matched_by=" + ",".join("/".join(hit.matched_by) for hit in result.hits), flush=True)

        CHANNELS.clear()
        with _statements() as one_shot:
            await memory.search("sharedtoken", memories=(head,), mode="fts", limit=10)
        print(f"fts_only_statements={len(one_shot)} channels={CHANNELS}", flush=True)

        CHANNELS.clear()
        with _statements() as vector_shot:
            await memory.search("sharedtoken", memories=(head,), mode="vector", limit=10)
        print(f"vector_only_statements={len(vector_shot)} channels={CHANNELS}", flush=True)

    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
