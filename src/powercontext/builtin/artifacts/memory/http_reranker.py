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

"""Score Memory candidates through an external HTTP reranking service.

The coarse pool is sent in bounded batches and the returned scores are fused into one
global order, so the final selection never depends on how the pool was split. Every
malformed answer fails loudly instead of degrading into a silent reordering: a missing
rank, a duplicated rank, an unknown field, or a non-finite score is a protocol error.
Transport failures are deliberately left as ``httpx`` errors, because a caller that
cannot reach the service must be able to tell that apart from a rejected answer.
"""

from __future__ import annotations

import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import (
    MemoryRerankDecision,
    _validate_bounds,
)
from powercontext.builtin.inference.models import InferenceUsage

_SCORE_FIELDS = frozenset({"rank", "score"})


class HTTPMemoryReranker:
    """Batch a coarse candidate pool through one HTTP scoring endpoint."""

    policy_id = "http-rerank-v1"

    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 8,
        timeout: float = 10.0,
    ) -> None:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("rerank url must be a non-empty string")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("rerank batch_size must be a positive integer")
        self._url = url
        self._client = client
        self._batch_size = batch_size
        self._timeout = timeout

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate once, then select the best ``limit`` original ranks."""

        _validate_bounds(len(candidates), limit)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("rerank query must be a non-empty string")

        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            ranks = tuple(range(start + 1, start + len(batch) + 1))
            payload = {
                "query": query,
                "candidates": [
                    {"rank": rank, "text": candidate.text}
                    for rank, candidate in zip(ranks, batch, strict=True)
                ],
            }
            response = await self._client.post(self._url, json=payload, timeout=self._timeout)
            requests += 1
            scores.update(_read_scores(response, expected_ranks=ranks))

        ordered = sorted(scores, key=lambda rank: (-scores[rank], rank))
        return MemoryRerankDecision(
            selected_ranks=tuple(ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _read_scores(response: httpx.Response, *, expected_ranks: tuple[int, ...]) -> dict[int, float]:
    """Validate one response against the exact ranks this batch asked about."""

    if response.status_code != httpx.codes.OK:
        raise ValueError(f"rerank service returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as error:
        raise ValueError("rerank response is not JSON") from error
    if not isinstance(body, dict) or set(body) != {"scores"}:
        raise ValueError("rerank response must contain exactly the scores field")
    entries = body["scores"]
    if not isinstance(entries, list):
        raise ValueError("rerank scores must be a list")
    if len(entries) != len(expected_ranks):
        raise ValueError("rerank scores must cover every batched candidate exactly once")

    scores: dict[int, float] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _SCORE_FIELDS:
            raise ValueError("rerank score entries must contain exactly rank and score")
        rank = entry["rank"]
        if not isinstance(rank, int) or isinstance(rank, bool) or rank not in expected_ranks:
            raise ValueError("rerank score rank is not part of this batch")
        if rank in scores:
            raise ValueError("rerank score ranks must be unique within one batch")
        score = entry["score"]
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ValueError("rerank score must be a number")
        if not math.isfinite(score):
            raise ValueError("rerank score must be finite")
        scores[rank] = float(score)
    if set(scores) != set(expected_ranks):
        raise ValueError("rerank scores must cover every batched candidate exactly once")
    return scores


__all__ = ["HTTPMemoryReranker"]
