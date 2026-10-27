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

"""Lock the span contracts behind query-driven evidence selection."""

from __future__ import annotations

from powercontext.builtin.runtime.evidence_fragments import rank_spans, sentence_spans, tokenize


def test_tokenize_covers_ascii_numbers_and_cjk() -> None:
    assert tokenize("Station 2022 海洋观测") == ("station", "2022", "海", "洋", "观", "测")


def test_sentence_spans_are_trimmed_and_offset_exact() -> None:
    text = "First claim.  Second claim goes on.   \n海洋观测站记录温度。tail without stop"
    spans = sentence_spans(text)
    assert [text[start:end] for start, end in spans] == [
        "First claim.",
        "Second claim goes on.",
        "海洋观测站记录温度。",
        "tail without stop",
    ]
    for start, end in spans:
        assert 0 <= start < end <= len(text)


def test_rank_prefers_query_coverage_over_position() -> None:
    texts = [
        "General notes. " * 30 + "The cobalt sensor is calibrated at 37 degrees.",
        "General notes. " * 30 + "The amber valve opens at 19 degrees.",
    ]
    top = rank_spans("cobalt sensor calibrated", texts)[0]
    assert top.entry_index == 0
    assert "37" in texts[0][top.start : top.end]

    swapped = rank_spans("amber valve opens", texts)[0]
    assert swapped.entry_index == 1
    assert "19" in texts[1][swapped.start : swapped.end]


def test_spans_without_query_terms_are_not_returned() -> None:
    texts = ["Nothing relevant here.", "The cobalt sensor is calibrated at 37 degrees."]
    ranked = rank_spans("cobalt calibrated", texts)
    assert [span.entry_index for span in ranked] == [1]


def test_density_breaks_coverage_ties() -> None:
    texts = ["Cobalt cobalt cobalt. Cobalt and calibrated sensor readings."]
    ranked = rank_spans("cobalt calibrated", texts)
    assert ranked[0].start == len("Cobalt cobalt cobalt. ")
