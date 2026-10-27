"""A recorded lifecycle transition is only complete once Memory has it.

The graph commit and the Memory write own separate transactions, so an event can
be recorded and then fail to propagate. The retry contract says the same event
must finish that propagation, which means the graph has to remember that the
transition is unfinished - otherwise an identical replay looks like a completed
idempotent one and returns without touching Memory.
"""

from __future__ import annotations

import json

from powercontext.builtin.runtime.source_lifecycle import (
    LIFECYCLE_SCHEMA,
    LifecycleNode,
    decode_nodes,
    encode_nodes,
)
from powercontext.sources import SourceRef


def _source(available: bool = False, *, propagated: bool = True) -> LifecycleNode:
    return LifecycleNode(
        node_id="source-a",
        kind="source",
        revision=3,
        available=available,
        sequence=1,
        citation=SourceRef(source_type="text", source_id="a"),
        propagated=propagated,
    )


def test_propagation_flag_survives_a_round_trip() -> None:
    for propagated in (True, False):
        restored = decode_nodes(encode_nodes((_source(propagated=propagated),)))
        assert restored[0].propagated is propagated


def test_a_document_without_the_flag_is_treated_as_propagated() -> None:
    # Documents written before the flag existed only stored finished transitions.
    stored = json.dumps(
        {
            "schema": LIFECYCLE_SCHEMA,
            "nodes": [
                {
                    "node_id": "source-a",
                    "kind": "source",
                    "revision": 3,
                    "parents": [],
                    "policy": None,
                    "available": False,
                    "sequence": 1,
                    "citation": {"source_type": "text", "source_id": "a"},
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    restored = decode_nodes(stored)
    assert restored[0].propagated is True
