from __future__ import annotations

import pytest
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError


def test_source_lifecycle_error_is_a_value_error() -> None:
    """The published contract is ``SourceLifecycleError(ValueError)``.

    Callers guard lifecycle rejections with ``except ValueError``, and the
    acceptance checks assert on ValueError. Deriving from RuntimeError instead
    makes every one of those guards miss and fails the whole question.
    """

    assert issubclass(SourceLifecycleError, ValueError)

    with pytest.raises(ValueError):
        raise SourceLifecycleError("unknown-source")


def test_source_lifecycle_error_keeps_its_code() -> None:
    error = SourceLifecycleError("revision-mismatch")
    assert error.code == "revision-mismatch"
    assert str(error) == "revision-mismatch"
