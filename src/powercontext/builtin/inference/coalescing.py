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

"""Coalesce concurrent embedding requests into few, bounded provider calls.

Every caller that asks for a text joins one shared in-flight future for that text, so N
concurrent callers asking for the same text cost one vector. The pending set is flushed
once per scheduling turn (or after ``coalesce_ms``), then split into calls of at most
``max_batch_size`` texts.

Two properties are deliberate:

* a waiter is awaited through :func:`asyncio.shield`, so cancelling one caller never
  cancels the shared provider work that other callers still need;
* a provider failure is delivered to every waiter and cached nowhere, so the next call
  genuinely retries instead of replaying a poisoned result.

The constructor defaults are the published contract (``max_batch_size=64``,
``coalesce_ms=5.0``, ``cache_size=1024``), not an implementation preference: a caller that
omits an argument is asking for exactly those numbers.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Optional

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage

_Vector = tuple[float, ...]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class CoalescingEmbeddingModel:
    """Batch and de-duplicate one embedding model behind the same ``embed`` contract."""

    def __init__(
        self,
        model: object,
        /,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if not _is_int(max_batch_size) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")
        if not _is_number(coalesce_ms) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")
        if not _is_int(cache_size) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_ms = float(coalesce_ms)
        self._cache_size = cache_size
        self._cache: OrderedDict[str, _Vector] = OrderedDict()
        self._pending: dict[str, asyncio.Future] = {}
        self._batch_usage: dict[int, InferenceUsage] = {}
        self._next_batch_id = 0
        self._flush_handle: object = None

    @property
    def profile(self) -> object:
        return self._model.profile

    async def embed(self, texts: object) -> EmbeddingResult:
        """Return one vector per requested text, in the requested order."""

        requested = tuple(texts)  # type: ignore[arg-type]
        if not requested:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        unique = tuple(dict.fromkeys(requested))
        waiters: dict[str, asyncio.Future] = {}
        loop = asyncio.get_running_loop()
        for text in unique:
            if text in self._cache:
                self._cache.move_to_end(text)
                continue
            future = self._pending.get(text)
            if future is None:
                future = loop.create_future()
                self._pending[text] = future
            waiters[text] = future
        if waiters:
            self._schedule_flush()

        resolved: dict[str, _Vector] = {}
        touched: dict[int, InferenceUsage] = {}
        for text in unique:
            future = waiters.get(text)
            if future is None:
                resolved[text] = self._cache[text]
                continue
            vector, batch_id = await asyncio.shield(future)
            resolved[text] = vector
            touched.setdefault(batch_id, self._batch_usage[batch_id])

        return EmbeddingResult(
            vectors=tuple(resolved[text] for text in requested),
            usage=InferenceUsage(
                requests=sum(usage.requests for usage in touched.values()),
                input_tokens=_sum_optional(usage.input_tokens for usage in touched.values()),
                output_tokens=_sum_optional(usage.output_tokens for usage in touched.values()),
            ),
        )

    def _schedule_flush(self) -> None:
        if self._flush_handle is not None:
            return
        loop = asyncio.get_running_loop()
        if self._coalesce_ms <= 0:
            self._flush_handle = loop.call_soon(self._flush)
        else:
            self._flush_handle = loop.call_later(self._coalesce_ms / 1000, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        if not self._pending:
            return
        texts = tuple(self._pending)
        futures = {text: self._pending.pop(text) for text in texts}
        task = asyncio.ensure_future(self._run_batches(texts, futures))
        # The flush outlives any single waiter: hold a reference so it is never collected.
        self._flush_task = task

    async def _run_batches(self, texts: tuple[str, ...], futures: dict[str, asyncio.Future]) -> None:
        for start in range(0, len(texts), self._max_batch_size):
            chunk = texts[start : start + self._max_batch_size]
            targets = [futures[text] for text in chunk]
            try:
                result = await self._model.embed(chunk)
            except asyncio.CancelledError:
                for future in targets:
                    if not future.done():
                        future.cancel()
                raise
            except Exception as error:  # noqa: BLE001 - delivered verbatim to every waiter.
                for future in targets:
                    if not future.done():
                        future.set_exception(error)
                continue
            if len(result.vectors) != len(chunk):
                error = ValueError("embedding provider returned a mismatched vector count")
                for future in targets:
                    if not future.done():
                        future.set_exception(error)
                continue
            batch_id = self._next_batch_id
            self._next_batch_id += 1
            self._batch_usage[batch_id] = result.usage
            for text, vector, future in zip(chunk, result.vectors, targets, strict=True):
                self._remember(text, vector)
                if not future.done():
                    future.set_result((vector, batch_id))

    def _remember(self, text: str, vector: _Vector) -> None:
        if self._cache_size == 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _sum_optional(values: object) -> Optional[int]:
    """Add the reported counters, keeping "unknown" distinct from zero."""

    reported = [value for value in values if value is not None]  # type: ignore[union-attr]
    if not reported:
        return None
    return sum(reported)


__all__ = ["CoalescingEmbeddingModel"]
