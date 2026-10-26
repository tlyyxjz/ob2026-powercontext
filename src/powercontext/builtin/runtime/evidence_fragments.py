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

"""Rank literal sentence spans of Memory entries against one query.

The module is pure: no I/O, no persistence, no model calls. It only answers "which
character ranges of these exact texts answer this query", so the caller stays responsible
for byte budgets and for keeping every returned range inside the original text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_TERM = re.compile(r"[a-z]+|\d+|[\u3400-\u4dbf\u4e00-\u9fff]")
# ASCII terminators need a following boundary so decimals and abbreviations stay intact;
# full-width terminators end a sentence on their own.
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|[。！？；]")
_LINE = re.compile(r"[^\n]+")


@dataclass(frozen=True)
class RankedSpan:
    """One query-relevant character range of one candidate text."""

    entry_index: int
    start: int
    end: int
    score: float
    density: float


def tokenize(text: str) -> tuple[str, ...]:
    """Lower-case ASCII words, numbers, and single CJK characters."""

    return tuple(_TERM.findall(text.lower()))


def sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Split text into sentence ranges, trimmed of surrounding whitespace."""

    spans: list[tuple[int, int]] = []
    for line in _LINE.finditer(text):
        offset = line.start()
        segment = line.group()
        cursor = 0
        for boundary in _SENTENCE_END.finditer(segment):
            spans.append(_trim(text, offset + cursor, offset + boundary.end()))
            cursor = boundary.end()
        if cursor < len(segment):
            spans.append(_trim(text, offset + cursor, offset + len(segment)))
    return tuple(span for span in spans if span[1] > span[0])


def rank_spans(query: str, texts: Sequence[str], /) -> tuple[RankedSpan, ...]:
    """Return candidate spans ordered by how much of the query they answer.

    A span scores on query-term coverage first and term density second, so a sentence that
    mentions every query term outranks one that repeats a single term. Spans with no query
    term are never returned: the caller decides what to do when nothing matches.
    """

    wanted = frozenset(tokenize(query))
    if not wanted:
        return ()

    ranked: list[RankedSpan] = []
    for index, text in enumerate(texts):
        for start, end in sentence_spans(text):
            terms = tokenize(text[start:end])
            if not terms:
                continue
            matched = wanted.intersection(terms)
            if not matched:
                continue
            coverage = len(matched) / len(wanted)
            density = len(matched) / len(terms)
            score = coverage + 0.25 * density
            if _contains_phrase(text[start:end], query):
                score += 0.5
            ranked.append(
                RankedSpan(
                    entry_index=index,
                    start=start,
                    end=end,
                    score=round(score, 6),
                    density=round(density, 6),
                )
            )
    ranked.sort(key=lambda span: (-span.score, -span.density, span.entry_index, span.start))
    return tuple(ranked)


def _contains_phrase(span: str, query: str) -> bool:
    needle = " ".join(query.lower().split())
    return bool(needle) and needle in " ".join(span.lower().split())


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


__all__ = ["RankedSpan", "rank_spans", "sentence_spans", "tokenize"]
