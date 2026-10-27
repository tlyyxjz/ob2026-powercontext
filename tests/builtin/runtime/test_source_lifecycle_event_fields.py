"""The published ``SourceLifecycleEvent`` field contract is strict.

Pydantic is permissive by default: ``"no"`` becomes ``True``, ``True`` passes an
``int`` bound, and ``"   "`` passes ``min_length=1``. The published field table
says otherwise - text is non-empty *and not whitespace-only*, the counters are
positive *integers*, and ``available`` is a *boolean*. Each rejection below is a
case the acceptance group can exercise, so every one of them is pinned here.
"""

from __future__ import annotations

import pytest
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleEvent
from pydantic import ValidationError


def _event(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "node_id": "source-a",
        "reason": "withdrawn by operator",
        "revision": 3,
        "sequence": 1,
        "available": False,
    }
    values.update(overrides)
    return values


def test_a_well_formed_event_is_accepted() -> None:
    event = SourceLifecycleEvent(**_event())
    assert event.node_id == "source-a"
    assert event.revision == 3
    assert event.sequence == 1
    assert event.available is False


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n", "   \t  "])
@pytest.mark.parametrize("field", ["node_id", "reason"])
def test_blank_text_is_rejected(field: str, blank: str) -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEvent(**_event(**{field: blank}))


@pytest.mark.parametrize("value", [True, False, 0, -1, 3.0, "3", None])
@pytest.mark.parametrize("field", ["revision", "sequence"])
def test_counters_must_be_strict_positive_integers(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEvent(**_event(**{field: value}))


@pytest.mark.parametrize("value", ["no", 0, 1, None, "true"])
def test_available_must_be_a_real_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEvent(**_event(available=value))


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEvent(**_event(unexpected="field"))
