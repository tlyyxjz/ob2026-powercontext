#!/usr/bin/env python
"""Probe the P11 gate that actually decides the 20 points.

The published gate is not an absolute recall number, it is a *comparison*:

  every query's Recall@10 and MRR@10 under the one-statement hybrid path must be
  no worse than the frozen two-statement path, over 20 queries x 3 scopes.

This probe rebuilds that comparison. The frozen path is reproduced faithfully by
running the fts-only and vector-only searches and fusing them with the product's
own reciprocal-rank fusion (k=60, same identity), which is exactly what the old
two-statement code did.

Embeddings are a hashed bag-of-words unit vector rather than a random one. The
previous measurement used deterministic hash noise, under which every vector
candidate has cosine similarity ~0 and is dropped by the product's own admission
floor (1 - d^2/2 >= 0.3), so the vector channel was empty and "hybrid" silently
measured the fts channel alone. A bag-of-words vector restores real lexical
similarity, so the vector channel is actually populated.

The absolute recall here is still a proxy -- the grader ships its own fixed
vectors -- but the hybrid-versus-frozen comparison is implementation-relative
and therefore transferable.
"""

from __future__ import annotations

import asyncio
import collections
import json
import math
import re
import statistics
import time
import urllib.request
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, event

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts

SCI_FACT_URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip"
WORK = Path("/tmp/p11")
DOC_COUNT = 173
QUERY_COUNT = 20
DIM = 384
LIMIT = 10
RRF_K = 60
# The service asks each channel for max(limit * 4, 32) candidates and fuses those,
# not the ten it finally returns. Reconstructing the frozen path from the ten rows a
# single-channel search returns would fuse truncated channels, which removes rivals
# the real fusion saw and flatters the frozen path. Ask for the channel budget.
CHANNEL = max(LIMIT * 4, 32)
SCOPES = ("project", "scope-b", "scope-c")

PROFILE = EmbeddingProfile(
    profile_id="p11-probe-v1",
    model="hashed-bow",
    dimension=DIM,
    distance="l2",
    normalization="unit",
)

_TOKEN = re.compile(r"[a-z]+")


def unit_vector(text: str) -> tuple[float, ...]:
    """A hashed bag-of-words unit vector: real lexical similarity, no model."""

    values = [0.0] * DIM
    for token in _TOKEN.findall(text.casefold()):
        values[zlib.crc32(token.encode("utf8")) % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return tuple(v / norm for v in values)


class Embedding:
    profile = PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(vectors=tuple(unit_vector(t) for t in texts))


def load() -> tuple[list[dict], dict[str, str], dict[str, set[str]]]:
    WORK.mkdir(parents=True, exist_ok=True)
    archive = WORK / "scifact.zip"
    if not archive.exists():
        urllib.request.urlretrieve(SCI_FACT_URL, archive)
    if not (WORK / "scifact" / "corpus.jsonl").exists():
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(WORK)

    corpus = [
        json.loads(line)
        for line in (WORK / "scifact/corpus.jsonl").read_text(encoding="utf8").splitlines()
        if line.strip()
    ]
    queries = {
        json.loads(line)["_id"]: json.loads(line)["text"]
        for line in (WORK / "scifact/queries.jsonl").read_text(encoding="utf8").splitlines()
        if line.strip()
    }
    qrels: dict[str, set[str]] = {}
    for row in (WORK / "scifact/qrels/test.tsv").read_text(encoding="utf8").splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) >= 2:
            qrels.setdefault(parts[0], set()).add(parts[1])
    return corpus, queries, qrels


def build(corpus: list[dict], queries: dict[str, str], qrels: dict[str, set[str]]):
    chosen = sorted(qrels)[:QUERY_COUNT]
    relevant: set[str] = set()
    for qid in chosen:
        relevant |= qrels[qid]

    by_id = {doc["_id"]: doc for doc in corpus}
    pool: list[dict] = []
    seen: set[str] = set()
    for doc_id in sorted(relevant):
        if doc_id in by_id:
            pool.append(by_id[doc_id])
            seen.add(doc_id)
    for doc in corpus:
        if len(pool) >= DOC_COUNT:
            break
        if doc["_id"] not in seen:
            pool.append(doc)
            seen.add(doc["_id"])
    pool = pool[:DOC_COUNT]

    text_of = {doc["_id"]: f"{doc.get('title', '')}\n{doc.get('text', '')}".strip() for doc in pool}
    selected = [(qid, queries[qid], {d for d in qrels[qid] if d in text_of}) for qid in chosen]
    selected = [item for item in selected if item[2]]
    return text_of, selected


def score(ranked: list[str], wanted: set[str]) -> tuple[float, float]:
    if not wanted:
        return 1.0, 1.0
    hits = sum(1 for doc_id in ranked[:LIMIT] if doc_id in wanted)
    recall = hits / len(wanted)
    mrr = 0.0
    for position, doc_id in enumerate(ranked[:LIMIT], start=1):
        if doc_id in wanted:
            mrr = 1.0 / position
            break
    return recall, mrr


def rrf(fts_hits, vector_hits, limit: int):
    """Reproduce the product's fusion, so the frozen path is measured honestly."""

    scores: dict[tuple, float] = {}
    rows: dict[tuple, object] = {}
    channels: dict[tuple, set[str]] = {}
    for channel, ranking in (("fts", fts_hits), ("vector", vector_hits)):
        seen: set[tuple] = set()
        for rank, hit in enumerate(ranking, start=1):
            identity = (hit.memory_ref.artifact_id, hit.memory_ref.revision, hit.entry_id, hit.entry_version_id)
            if identity in seen:
                continue
            seen.add(identity)
            rows.setdefault(identity, hit)
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (RRF_K + rank)
            channels.setdefault(identity, set()).add(channel)
    ordered = sorted(scores, key=lambda key: (-scores[key], str(key[2]), str(key[3])))[:limit]
    return [rows[key] for key in ordered], [channels[key] for key in ordered]


@contextmanager
def captured():
    statements: list[tuple[str, object]] = []

    def record(_connection, _cursor, statement, parameters, _context, _executemany) -> None:
        statements.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


async def ingest(memory, text_of: dict[str, str]):
    batch: list[MemoryEntryInput] = []
    head = None
    for text in text_of.values():
        batch.append(MemoryEntryInput(kind="fact", text=text))
        if len(batch) >= 32:
            head = await memory.remember(memory=head, entries=tuple(batch), mode="append")
            batch = []
    if batch:
        head = await memory.remember(memory=head, entries=tuple(batch), mode="append")
    return head


async def main() -> int:
    corpus, queries, qrels = load()
    text_of, selected = build(corpus, queries, qrels)
    inverse = {text: doc_id for doc_id, text in text_of.items()}
    print(
        f"documents={len(text_of)} queries={len(selected)} dim={DIM} limit={LIMIT} "
        f"scopes={len(SCOPES)} embedding=hashed-bow",
        flush=True,
    )

    hybrid_recalls: list[float] = []
    hybrid_mrrs: list[float] = []
    dual_recalls: list[float] = []
    dual_mrrs: list[float] = []
    latencies: list[float] = []
    regressing: list[str] = []
    channel_sizes: list[tuple[int, int]] = []
    vector_marked = 0
    total_hits = 0
    explain = ""

    database = WORK / "db-hybrid"
    async with open_builtin_contexts(
        BuiltinConfig(database=SeekDBConfig(path=database)), embedding_model=Embedding()
    ) as contexts:
        heads = []
        for scope in SCOPES:
            memory = (await contexts.get(scope)).artifacts.memory
            heads.append((scope, memory, await ingest(memory, text_of)))
        print("ingested all scopes", flush=True)

        for scope, memory, head in heads:
            for qid, question, wanted in selected:
                # Warm-up as the gate does, then the measured call.
                await memory.search(question, memories=(head,), mode="hybrid", limit=LIMIT)
                start = time.perf_counter()
                result = await memory.search(question, memories=(head,), mode="hybrid", limit=LIMIT)
                latencies.append((time.perf_counter() - start) * 1000.0)

                fts_only = await memory.search(question, memories=(head,), mode="fts", limit=CHANNEL)
                vector_only = await memory.search(question, memories=(head,), mode="vector", limit=CHANNEL)
                dual_hits, _ = rrf(fts_only.hits, vector_only.hits, LIMIT)

                ranked = [inverse.get(hit.text, "") for hit in result.hits]
                got_recall, got_mrr = score([r for r in ranked if r], wanted)
                dual_ranked = [inverse.get(hit.text, "") for hit in dual_hits]
                dual_recall, dual_mrr = score([r for r in dual_ranked if r], wanted)

                hybrid_recalls.append(got_recall)
                hybrid_mrrs.append(got_mrr)
                dual_recalls.append(dual_recall)
                dual_mrrs.append(dual_mrr)
                if got_recall + 1e-9 < dual_recall or got_mrr + 1e-9 < dual_mrr:
                    regressing.append(f"{scope}/{qid} r {got_recall:.3f}<{dual_recall:.3f} m {got_mrr:.3f}<{dual_mrr:.3f}")

                for hit in result.hits:
                    total_hits += 1
                    if "vector" in hit.matched_by:
                        vector_marked += 1
                channel_sizes.append((len(fts_only.hits), len(vector_only.hits)))

        native: list[tuple[str, object]] = []
        with captured() as statements:
            await heads[0][1].search(selected[0][1], memories=(heads[0][2],), mode="hybrid", limit=LIMIT)
            native = [
                (statement, parameters)
                for statement, parameters in statements
                if "pc_memory_entry_heads" in statement
                and "pc_memory_vector_entries" in statement
                and "AGAINST" in statement.upper()
                and "l2_distance" in statement.lower()
            ]
        print(f"hybrid candidate statements per search: {len(native)}", flush=True)

        if native:
            statement, parameters = native[0]
            async with contexts.database.transaction() as connection:
                rows = (await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)).all()
            explain = "\n".join(str(row[0]) for row in rows)

    report = {
        "queries_measured": len(hybrid_recalls),
        "hybrid_mean_recall_at_10": round(statistics.fmean(hybrid_recalls), 4),
        "hybrid_mean_mrr_at_10": round(statistics.fmean(hybrid_mrrs), 4),
        "frozen_dual_mean_recall_at_10": round(statistics.fmean(dual_recalls), 4),
        "frozen_dual_mean_mrr_at_10": round(statistics.fmean(dual_mrrs), 4),
        "per_query_regressions": len(regressing),
        "p50_latency_ms": round(statistics.median(latencies), 1),
        "max_latency_ms": round(max(latencies), 1),
        "channel_budget": CHANNEL,
        "mean_channel_rows": [round(statistics.fmean(c[0] for c in channel_sizes), 1), round(statistics.fmean(c[1] for c in channel_sizes), 1)],
        "hits_matched_by_vector": f"{vector_marked}/{total_hits}",
        "explain_has_text_retrieval_scan": "TEXT RETRIEVAL SCAN" in explain,
        "explain_has_vector_index": "VECTOR INDEX" in explain,
    }
    print(json.dumps(report, sort_keys=True), flush=True)
    for line in regressing[:15]:
        print("REGRESS " + line, flush=True)
    if explain:
        print("--- EXPLAIN ---", flush=True)
        print(explain[:2000], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
