"""Adversarial checks for the coalescing wrapper, beyond the published happy path.

Each case maps one row of the published behaviour table:

* ``usage`` counts a batch once per caller that waited on it, and a pure cache
  hit charges zero requests;
* a late caller joins provider work that is already running in the same batch run;
* ``cache_size=0`` still shares in-flight work;
* cancelling one waiter never strands the others.
"""

from __future__ import annotations

import asyncio

import pytest
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel
from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage


class CountingModel:
    """Deterministic offline model that records every batch it is asked to embed."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.profile = type("Profile", (), {"name": "counting", "dimension": 2})()
        self.batches: list[tuple[str, ...]] = []
        self.delay = delay
        self.gate = None

    async def embed(self, texts: object) -> EmbeddingResult:
        chunk = tuple(texts)  # type: ignore[arg-type]
        self.batches.append(chunk)
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        vectors = tuple((float(len(text)), float(sum(map(ord, text)) % 97)) for text in chunk)
        return EmbeddingResult(
            vectors=vectors,
            usage=InferenceUsage(requests=1, input_tokens=len(chunk), output_tokens=0),
        )


async def test_usage_is_counted_once_per_batch_per_caller() -> None:
    base = CountingModel()
    model = CoalescingEmbeddingModel(base, max_batch_size=8, coalesce_ms=0, cache_size=0)

    first, second = await asyncio.gather(model.embed(("a", "a", "b")), model.embed(("a", "b")))

    assert len(base.batches) == 1, base.batches
    assert sorted(base.batches[0]) == ["a", "b"]
    assert len(first.vectors) == 3 and len(second.vectors) == 2
    for result in (first, second):
        assert result.usage.requests == 1, result.usage
        assert result.usage.input_tokens == 2


async def test_pure_cache_hit_charges_no_request() -> None:
    base = CountingModel()
    model = CoalescingEmbeddingModel(base, max_batch_size=8, coalesce_ms=0, cache_size=8)

    await model.embed(("a",))
    again = await model.embed(("a",))

    assert len(base.batches) == 1
    assert again.usage.requests == 0
    assert again.vectors == ((1.0, 0.0),)


async def test_cache_size_zero_still_shares_in_flight_work() -> None:
    base = CountingModel()
    model = CoalescingEmbeddingModel(base, max_batch_size=8, coalesce_ms=0, cache_size=0)

    first, second = await asyncio.gather(model.embed(("a",)), model.embed(("a",)))
    third = await model.embed(("a",))

    assert len(base.batches) == 2, base.batches
    assert first.vectors == second.vectors == third.vectors


async def test_late_caller_joins_running_provider_work() -> None:
    base = CountingModel()
    base.gate = asyncio.Event()
    model = CoalescingEmbeddingModel(base, max_batch_size=8, coalesce_ms=0, cache_size=0)

    early = asyncio.create_task(model.embed(("a", "b")))
    await asyncio.sleep(0.02)
    # The provider is inside embed() and blocked on the gate; a late caller for the
    # same text must still share that batch instead of issuing a second request.
    late = asyncio.create_task(model.embed(("a",)))
    await asyncio.sleep(0.02)
    base.gate.set()

    await asyncio.gather(early, late)
    assert len(base.batches) == 1, base.batches


async def test_cancelling_one_waiter_leaves_the_others_with_results() -> None:
    base = CountingModel()
    base.gate = asyncio.Event()
    model = CoalescingEmbeddingModel(base, max_batch_size=8, coalesce_ms=0, cache_size=0)

    doomed = asyncio.create_task(model.embed(("shared",)))
    survivor = asyncio.create_task(model.embed(("shared", "other")))
    await asyncio.sleep(0.02)

    doomed.cancel()
    base.gate.set()

    with pytest.raises(asyncio.CancelledError):
        await doomed
    result = await survivor

    assert len(result.vectors) == 2
    assert len(base.batches) == 1, base.batches


async def test_batch_limit_is_never_exceeded() -> None:
    base = CountingModel()
    model = CoalescingEmbeddingModel(base, max_batch_size=3, coalesce_ms=0, cache_size=0)

    texts = tuple(f"t{index}" for index in range(10))
    await asyncio.gather(*(model.embed((text,)) for text in texts))

    assert all(len(batch) <= 3 for batch in base.batches), base.batches
    sent = [text for batch in base.batches for text in batch]
    assert sorted(sent) == sorted(texts), sent
