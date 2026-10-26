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

"""Capture one selected Pi session branch as immutable per-event Sources."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"
PI_SESSION_VERSION = 3

_PI_EVENT_PREFIX = "pi-event:"
_TOOL_CALL_BLOCK = "toolCall"
_TEXT_BLOCK = "text"
_BASH_ROLE = "bashExecution"
_RESULT_ROLE = "toolResult"
_SUMMARY_TYPES = ("compaction", "branch_summary")
_UNKNOWN = "unknown"
_CHECKPOINT_VERSION = 1


class PiSessionInput(BaseModel):
    """Raw Pi session bytes plus the branch leaf that selects the history path."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, with its complete input record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    """A captured Pi event whose identity depends only on session and event id."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ParsedSession:
    session_id: str
    records: dict[str, dict[str, Any]]
    order: tuple[str, ...]
    events: tuple[PiEventInput, ...]


def _read_jsonl(content: bytes) -> list[dict[str, Any]]:
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except (UnicodeDecodeError, AttributeError) as error:
        raise ValueError("Pi session content must be valid UTF-8 bytes") from error

    lines: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Pi session line is not valid JSON: {error}") from error
        if not isinstance(value, dict):
            raise ValueError("every Pi session line must be a JSON object")
        lines.append(value)
    if not lines:
        raise ValueError("Pi session must start with a session header")
    return lines


def _read_header(header: dict[str, Any]) -> str:
    if header.get("type") != "session":
        raise ValueError("Pi session header must declare type 'session'")
    version = header.get("version")
    if isinstance(version, bool) or version != PI_SESSION_VERSION:
        raise ValueError(f"Pi session header must declare version {PI_SESSION_VERSION}")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Pi session header id must be a non-empty string")
    return session_id


def _build_graph(records: Sequence[dict[str, Any]], session_id: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    known = {session_id}
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("every Pi session record must carry a non-empty id")
        if event_id in known:
            raise ValueError(f"Pi session record id {event_id!r} is duplicated")
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or parent_id not in known:
                raise ValueError(f"Pi session record {event_id!r} references an unknown parent")
        known.add(event_id)
        by_id[event_id] = record
        order.append(event_id)
    return by_id, tuple(order)


def _visible_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == _TEXT_BLOCK:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == _TOOL_CALL_BLOCK:
            parts.append(_tool_call(block))
    return "\n".join(parts)


def _tool_call(block: dict[str, Any]) -> str:
    name = block.get("name")
    call_id = block.get("id")
    name = name if isinstance(name, str) and name else _UNKNOWN
    call_id = call_id if isinstance(call_id, str) and call_id else _UNKNOWN
    arguments = block.get("arguments")
    encoded = "null" if arguments is None else json.dumps(
        arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"[tool_call {name} {call_id}] {encoded}"


def _describe(record: dict[str, Any]) -> tuple[str, str] | None:
    """Return the role and visible body of one record, or None for control nodes."""

    event_type = record.get("type")
    if not isinstance(event_type, str):
        return None

    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        if role == _BASH_ROLE:
            command = message.get("command")
            output = message.get("output")
            body = f"$ {command if isinstance(command, str) else ''}\n{output if isinstance(output, str) else ''}"
        elif role == _RESULT_ROLE:
            tool_name = message.get("toolName")
            tool_call_id = message.get("toolCallId")
            tool_name = tool_name if isinstance(tool_name, str) and tool_name else _UNKNOWN
            tool_call_id = tool_call_id if isinstance(tool_call_id, str) and tool_call_id else _UNKNOWN
            detail = _visible_text(message.get("content"))
            body = f"[tool_result {tool_name} {tool_call_id}] {detail}"
        else:
            body = _visible_text(message.get("content"))
    elif event_type in _SUMMARY_TYPES:
        role = event_type
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else ""
    elif event_type == "custom_message":
        role = "custom"
        body = _visible_text(record.get("content"))
    else:
        return None

    if not body.strip():
        return None
    return role, body


def _event_from_record(session_id: str, record: dict[str, Any]) -> PiEventInput | None:
    described = _describe(record)
    if described is None:
        return None
    role, body = described
    parent_id = record.get("parentId")
    return PiEventInput(
        session_id=session_id,
        event_id=str(record["id"]),
        parent_id=parent_id if isinstance(parent_id, str) else None,
        event_type=str(record["type"]),
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _parse_session(value: PiSessionInput) -> _ParsedSession:
    lines = _read_jsonl(value.content)
    session_id = _read_header(lines[0])
    by_id, order = _build_graph(lines[1:], session_id)

    if not order:
        raise ValueError("Pi session must contain at least one record")
    leaf_id = value.leaf_id if value.leaf_id is not None else order[-1]
    if leaf_id not in by_id:
        raise ValueError(f"Pi session leaf {leaf_id!r} is not present in the file")

    chain: list[dict[str, Any]] = []
    cursor: str | None = leaf_id
    while cursor is not None and cursor != session_id:
        record = by_id[cursor]
        chain.append(record)
        parent_id = record.get("parentId")
        cursor = parent_id if isinstance(parent_id, str) else None
    chain.reverse()

    events = tuple(
        event for event in (_event_from_record(session_id, record) for record in chain) if event is not None
    )
    return _ParsedSession(session_id=session_id, records=by_id, order=order, events=events)


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return every visible event on the selected branch, ordered root to leaf."""

    return _parse_session(value).events


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the bounded, delimiter-safe identity of one Pi event."""

    for field, item in (("session_id", session_id), ("event_id", event_id)):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} must be a non-empty string")
    source_id = f"{_PI_EVENT_PREFIX}{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"Pi event source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


def _record_digest(record: dict[str, Any]) -> str:
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PiEventSourceAdapter:
    """Resolve one parsed event into a durable captured Source."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        expected = pi_event_source_id(source.session_id, source.event_id)
        if source.source_id != expected or source.name != expected:
            raise ValueError("Pi event Source identity is inconsistent with its event")
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Project one event as complete text evidence plus identity metadata."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)


def _load_imported(checkpoint: object) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict):
        raise ValueError("Pi session checkpoint must be a JSON object")
    version = checkpoint.get("version")
    if version != _CHECKPOINT_VERSION:
        raise ValueError(f"Pi session checkpoint version must be {_CHECKPOINT_VERSION}")
    imported = checkpoint.get("events")
    if not isinstance(imported, dict):
        raise ValueError("Pi session checkpoint must record imported events")
    restored: dict[str, str] = {}
    for event_id, digest in imported.items():
        if not isinstance(event_id, str) or not isinstance(digest, str):
            raise ValueError("Pi session checkpoint entries must map event ids to digests")
        restored[event_id] = digest
    return restored


class PiSessionConnector:
    """Import the selected branch incrementally and keep the durable progress."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        parsed = _parse_session(PiSessionInput(content=self.path.read_bytes(), leaf_id=self.leaf_id))
        imported = _load_imported(session.checkpoint)

        for event_id, digest in imported.items():
            record = parsed.records.get(event_id)
            if record is not None and _record_digest(record) != digest:
                raise ValueError(f"imported Pi event {event_id!r} was rewritten in the session file")

        committed = dict(imported)
        for event in parsed.events:
            if event.event_id in committed:
                continue
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            committed[event.event_id] = _record_digest(event.record)

        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"version": _CHECKPOINT_VERSION, "events": committed},
        )


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
