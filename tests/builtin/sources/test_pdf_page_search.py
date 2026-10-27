from __future__ import annotations

import inspect

import pytest
from powercontext.sources.pdf_pages import search_pdf_pages


def test_search_pdf_pages_keeps_the_published_signature() -> None:
    """``search_pdf_pages(memory, question, limit=10)`` is the published contract.

    ``limit`` is an ordinary positional-or-keyword parameter, so a caller that
    follows the signature may pass it positionally. Declaring it keyword-only
    rejects that call with TypeError, which fails every acceptance check that
    retrieves with an explicit limit.
    """

    signature = inspect.signature(search_pdf_pages)
    assert list(signature.parameters) == ["memory", "question", "limit"]

    positional = signature.parameters["limit"]
    assert positional.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert positional.default == 10

    assert signature.bind(None, "question", 20).arguments["limit"] == 20
    assert signature.bind(None, "question", limit=20).arguments["limit"] == 20
    assert signature.bind(None, "question").arguments == {"memory": None, "question": "question"}


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "10"])
def test_search_pdf_pages_rejects_a_limit_that_is_not_a_positive_integer(limit) -> None:
    with pytest.raises(ValueError):
        # Validation runs before any search is issued, so memory can stay a stub.
        import asyncio

        asyncio.run(search_pdf_pages(None, "question", limit=limit))
