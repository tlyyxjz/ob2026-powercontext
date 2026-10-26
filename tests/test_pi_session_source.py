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

"""Lock the Pi event contracts the published acceptance suite cannot see."""

from __future__ import annotations

import asyncio
import json

from powercontext.sources import SourceDefinitionRegistry
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiSessionInput,
    parse_pi_session,
)


def _session(records: list[dict], leaf: str | None = None, session_id: str = "lock") -> tuple:
    header = {"type": "session", "version": 3, "id": session_id}
    raw = "\n".join(json.dumps(row) for row in [header, *records]).encode()
    return parse_pi_session(PiSessionInput(content=raw, leaf_id=leaf))


def test_text_blocks_are_joined_with_newlines() -> None:
    """A message with several visible blocks must keep them on separate lines."""

    events = _session(
        [
            {
                "type": "message",
                "id": "a",
                "parentId": None,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "first line"},
                        {"type": "text", "text": "second line"},
                    ],
                },
            }
        ]
    )
    assert [event.content for event in events] == ["assistant: first line\nsecond line"]


def test_tool_arguments_are_sorted_and_keep_unicode() -> None:
    """Compact JSON must sort keys and stay unescaped."""

    events = _session(
        [
            {
                "type": "message",
                "id": "a",
                "parentId": None,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "name": "read", "id": "c1", "arguments": {"z": "值", "a": 1}}],
                },
            }
        ]
    )
    assert [event.content for event in events] == ['assistant: [tool_call read c1] {"a":1,"z":"值"}']


def test_same_event_id_in_different_sessions_keeps_distinct_identity() -> None:
    """Identity includes the session, so equal event ids must still differ."""

    records = [{"type": "message", "id": "shared", "parentId": None, "message": {"role": "user", "content": "hi"}}]
    left = _session(records, session_id="session-a")[0]
    right = _session(records, session_id="session-b")[0]
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))

    async def scenario() -> tuple[str, str]:
        return (await registry.resolve(left)).name, (await registry.resolve(right)).name

    left_name, right_name = asyncio.run(scenario())
    assert left_name != right_name
    assert left.event_id == right.event_id == "shared"
