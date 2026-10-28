"""Context pack wire format and prefix-reuse deltas.

A prepared context is represented as an ordered tuple of byte chunks so a client that
already holds the previous pack can be sent only the suffix. The wire formats below are
the interoperability contract: JSON with sorted keys and no extra whitespace, Base64
chunk values, and a SHA-256 digest computed over the unsigned payload only.

The byte budget matters: ``ContextPack.byte_count`` includes its own JSON field, and a
delta's ``byte_count`` is the serialized size of the delta itself. Both stay honest about
the bytes a receiver would actually have to read.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Union

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512

_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})


class ContextPackError(ValueError):
    """Raised when a context pack or delta violates the wire protocol."""


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _b64encode(item: bytes) -> str:
    return base64.b64encode(item).decode("ascii")


def _b64decode(value: object) -> bytes:
    if not isinstance(value, str):
        raise ContextPackError("chunk-not-a-string")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as error:
        raise ContextPackError("chunk-not-base64") from error


def _as_bytes(item: object) -> bytes:
    if isinstance(item, bytes):
        return item
    if isinstance(item, bytearray):
        return bytes(item)
    if isinstance(item, memoryview):
        return item.tobytes()
    if isinstance(item, str):
        return item.encode("utf-8")
    raise ContextPackError("chunk-not-bytes")


def _digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": PACK_SCHEMA, "items": [_b64encode(item) for item in items]}
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _decode_json(raw: object) -> dict:
    if not isinstance(raw, (bytes, bytearray, memoryview, str)):
        raise ContextPackError("payload-not-bytes")
    if isinstance(raw, str):
        data = raw.encode("utf-8")
    else:
        data = bytes(raw)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError("payload-not-json") from error
    if not isinstance(payload, dict):
        raise ContextPackError("payload-not-object")
    return payload


@dataclass(frozen=True)
class ContextPack:
    """An immutable, ordered set of byte chunks plus its wire identity."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: Iterable[object]) -> "ContextPack":
        chunks = tuple(_as_bytes(item) for item in items)
        digest = _digest(chunks)
        encoded = [_b64encode(chunk) for chunk in chunks]
        # byte_count counts the serialized payload that contains byte_count, so the value
        # must be found by iterating on its own digit width until it stops moving.
        size = 0
        for _ in range(8):
            payload = {"schema": PACK_SCHEMA, "items": encoded, "digest": digest, "byte_count": size}
            measured = len(_canonical(payload))
            if measured == size:
                break
            size = measured
        return cls(items=chunks, digest=digest, byte_count=size)

    @classmethod
    def from_content(cls, content: Union[str, bytes], *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> "ContextPack":
        """Split content into deterministic chunks that are each valid UTF-8 and within the limit."""
        if isinstance(content, (bytes, bytearray, memoryview)):
            try:
                text = bytes(content).decode("utf-8")
            except UnicodeDecodeError as error:
                raise ContextPackError("content-not-utf8") from error
        elif isinstance(content, str):
            text = content
        else:
            raise ContextPackError("content-not-text")
        if not isinstance(chunk_bytes, int) or isinstance(chunk_bytes, bool) or chunk_bytes <= 0:
            raise ContextPackError("invalid-chunk-bytes")
        chunks: list[bytes] = []
        buffer: list[str] = []
        buffered = 0
        for character in text:
            width = len(character.encode("utf-8"))
            if buffered + width > chunk_bytes:
                if not buffer:
                    raise ContextPackError("chunk-bytes-too-small")
                chunks.append("".join(buffer).encode("utf-8"))
                buffer = [character]
                buffered = width
            else:
                buffer.append(character)
                buffered += width
        if buffer:
            chunks.append("".join(buffer).encode("utf-8"))
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": PACK_SCHEMA,
                "items": [_b64encode(item) for item in self.items],
                "digest": self.digest,
                "byte_count": self.byte_count,
            }
        )

    @classmethod
    def from_bytes(cls, raw: object) -> "ContextPack":
        payload = _decode_json(raw)
        if set(payload) != _PACK_FIELDS:
            raise ContextPackError("pack-fields")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("pack-schema")
        encoded = payload["items"]
        if not isinstance(encoded, list):
            raise ContextPackError("pack-items-not-list")
        chunks = tuple(_b64decode(value) for value in encoded)
        if payload["digest"] != _digest(chunks):
            raise ContextPackError("pack-digest")
        measured = len(_canonical(payload))
        if not isinstance(payload["byte_count"], int) or isinstance(payload["byte_count"], bool):
            raise ContextPackError("pack-byte-count")
        if payload["byte_count"] != measured:
            raise ContextPackError("pack-byte-count")
        return cls(items=chunks, digest=payload["digest"], byte_count=measured)


@dataclass(frozen=True)
class ContextPackDelta:
    """The suffix needed to rebuild ``target`` from an already-held ``base`` pack."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> "ContextPackDelta":
        prefix = 0
        for held, wanted in zip(base.items, target.items):
            if held != wanted:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=tuple(target.items[prefix:]),
        )

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": [_b64encode(item) for item in self.appended],
            }
        )

    @classmethod
    def from_bytes(cls, raw: object) -> "ContextPackDelta":
        payload = _decode_json(raw)
        if set(payload) != _DELTA_FIELDS:
            raise ContextPackError("delta-fields")
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("delta-schema")
        prefix = payload["prefix_items"]
        if not isinstance(prefix, int) or isinstance(prefix, bool) or prefix < 0:
            raise ContextPackError("delta-prefix")
        appended = payload["appended"]
        if not isinstance(appended, list):
            raise ContextPackError("delta-appended-not-list")
        return cls(
            base_digest=_require_digest(payload["base_digest"], "delta-base-digest"),
            target_digest=_require_digest(payload["target_digest"], "delta-target-digest"),
            prefix_items=prefix,
            appended=tuple(_b64decode(value) for value in appended),
        )


def _require_digest(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != len("sha256:") + 64:
        raise ContextPackError(reason)
    return value


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from ``base`` plus ``delta``, validating every invariant."""
    if base.digest != delta.base_digest:
        raise ContextPackError("delta-base-mismatch")
    if not isinstance(delta.prefix_items, int) or isinstance(delta.prefix_items, bool):
        raise ContextPackError("delta-prefix")
    if delta.prefix_items < 0 or delta.prefix_items > len(base.items):
        raise ContextPackError("delta-prefix-out-of-range")
    rebuilt = ContextPack.build(tuple(base.items[: delta.prefix_items]) + tuple(delta.appended))
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("delta-target-mismatch")
    return rebuilt


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "DEFAULT_CHUNK_BYTES",
    "DELTA_SCHEMA",
    "PACK_SCHEMA",
    "apply_context_pack_delta",
]
