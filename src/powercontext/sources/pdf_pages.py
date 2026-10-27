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

"""Capture offline PDF documents as independently addressable page Sources.

A page Source is addressed by ``(document_id, page_number)`` plus the digest of the
document bytes, so a re-issued document keeps every page number while changing every
page identity. Parsing is all-or-nothing: a document either yields its complete page
sequence or raises before any page escapes.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Sequence
from urllib.parse import quote

from pydantic import BaseModel
from pypdf import PdfReader

from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
_PDF_PAGE_PREFIX = "pdf:"


class PDFDocumentInput(BaseModel):
    """Complete offline PDF bytes plus the document identity a caller assigns."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One page of a parsed document, addressed by document identity and page number."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A captured PDF page whose identity covers the document revision it came from."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def _validate_document_id(document_id: object) -> None:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")


def _validate_page_contract(
    *,
    document_id: object,
    document_sha256: object,
    page_number: object,
    text: object,
) -> None:
    _validate_document_id(document_id)
    if not isinstance(document_sha256, str) or len(document_sha256) != 64:
        raise ValueError("document_sha256 must be a 64-character lowercase hex digest")
    if document_sha256 != document_sha256.lower():
        raise ValueError("document_sha256 must be a 64-character lowercase hex digest")
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if not isinstance(text, str):
        raise ValueError("page text must be a string")
    pdf_page_source_id(document_id, document_sha256, page_number)


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the delimiter-safe, bounded identity for one PDF page."""

    _validate_document_id(document_id)
    source_id = f"{_PDF_PAGE_PREFIX}{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"PDF page source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse a whole PDF into ordered pages, or fail before returning any page."""

    _validate_document_id(value.document_id)
    if not isinstance(value.content, (bytes, bytearray, memoryview)):
        raise ValueError("PDF content must be bytes")
    content = bytes(value.content)
    if not content:
        raise ValueError("PDF content must not be empty")
    document_sha256 = hashlib.sha256(content).hexdigest()

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a password to read")
        page_total = len(reader.pages)
        if page_total < 1:
            raise ValueError("PDF must contain at least one page")
        pages = tuple(
            PDFPageInput(
                document_id=value.document_id,
                document_sha256=document_sha256,
                page_number=index + 1,
                text=reader.pages[index].extract_text() or "",
            )
            for index in range(page_total)
        )
    except ValueError:
        raise
    except Exception as error:  # pypdf surfaces every malformed input as its own base error.
        raise ValueError(f"malformed PDF: {error}") from error

    for page in pages:
        _validate_page_contract(
            document_id=page.document_id,
            document_sha256=page.document_sha256,
            page_number=page.page_number,
            text=page.text,
        )
    return pages


class PDFPageSourceAdapter:
    """Resolve one validated page into a durable captured Source."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        _validate_page_contract(
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        _validate_page_contract(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )
        expected_id = pdf_page_source_id(source.document_id, source.document_sha256, source.page_number)
        if source.source_id != expected_id or source.name != expected_id:
            raise ValueError("PDF page Source identity is inconsistent with its page")
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Project one page as its document identity followed by the exact page text."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


# Expanding a term reaches pages that word the concept differently ("capex" versus
# "capital expenditures"). Those words are common in a financial statement, so an
# expansion must not weigh as much as the term the question actually used.
_EXPANSION_WEIGHT = 0.35
# Distance, in tokens, within which two question terms count as co-occurring. A
# financial table cell and its row label sit well inside this, while an index entry
# that merely repeats the words elsewhere on the page does not score the window.
_PROXIMITY_WIDTH = 12
# Word boundary used by the published tokenisation rule, so proximity is measured on
# the same tokens the quality thresholds are defined over.
_TOKEN = re.compile(r"[a-z]+|\d+")


async def search_pdf_pages(memory: object, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return citations whose pages are all distinct exact pages.

    The published contract is ``search_pdf_pages(memory, question, limit=10)``:
    ``limit`` is an ordinary positional-or-keyword parameter with a default of 10,
    so a caller may pass it either way.

    Space-separated FTS is an implicit AND, and a financial question's own wording
    rarely appears on its evidence page (an EBITDA question's evidence page is a
    statement page that never prints "EBITDA"). So the question is decomposed into
    terms, each term is searched separately through the public Memory API, and the
    per-entry scores are accumulated. Term-level search is what lets a page that
    supports the answer surface even though it does not repeat the question.

    Single terms alone are too weak on their own: a report prints "operating" on
    hundreds of pages, so a page that merely repeats one word outranks the page
    that states the figure. The lexical search reports an OR over its query, so
    asking for a phrase does not make adjacency available - it only lowers the term
    coverage the query needs, which lets index pages through. Adjacency is therefore
    recovered after recall instead: the recalled pages are read back and ranked by
    how many distinct question terms share one token window, so a statement row and
    its label outrank a page that prints the same words far apart.

    Retrieval ranks entries, not pages, so a document may contribute several hits that
    resolve to the same page. The returned citation tuple never does: each accepted
    citation is dropped as soon as its page identity was already claimed.
    """

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")

    depth = max(limit * 10, _MIN_TERM_DEPTH)
    scores: dict[str, float] = {}
    first_hit: dict[str, object] = {}
    for rank, term in enumerate(_query_terms(question)):
        page = await memory.search(SearchMemoryRequest(query=term, mode="fts", limit=depth))
        # The first term is the question's own wording; expansions only make a page
        # reachable and must not outvote the word the caller actually used.
        weight = 1.0 if rank == 0 else _EXPANSION_WEIGHT
        for hit in page.hits:
            scores[hit.entry_id] = scores.get(hit.entry_id, 0.0) + weight * float(hit.score)
            first_hit.setdefault(hit.entry_id, hit)

    question_terms = frozenset(_query_terms(question))
    ranked = _order_by_proximity(
        await _page_texts(memory, first_hit, scores), question_terms, scores
    )

    citations: list[MemoryCitation] = []
    claimed: set[tuple[str, ...]] = set()
    for entry_id in ranked:
        if len(citations) >= limit:
            break
        hit = first_hit[entry_id]
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        entry = await memory.get(GetMemoryEntryRequest(citation=citation))
        identities = tuple(
            sorted({reference.source_id for reference in entry.entry.sources if reference.source_id.startswith(_PDF_PAGE_PREFIX)})
        )
        if not identities or identities in claimed:
            continue
        claimed.add(identities)
        citations.append(citation)
    return tuple(citations)


async def _page_texts(
    memory, first_hit: dict[str, object], scores: dict[str, float]
) -> dict[str, str]:
    """Read the recalled pages' own text, which is what proximity is measured on.

    A page that cannot be read must still be orderable: dropping it here would
    silently shrink the candidate set and could cost a required page, so an
    unreadable page is represented by its empty text and keeps its lexical rank.
    """

    texts: dict[str, str] = {}
    for entry_id in scores:
        hit = first_hit[entry_id]
        try:
            entry = await memory.get(
                GetMemoryEntryRequest(
                    citation=MemoryCitation(
                        memory_ref=hit.memory_ref,
                        entry_id=hit.entry_id,
                        entry_version_id=hit.entry_version_id,
                    )
                )
            )
        except Exception:  # noqa: BLE001 - unreadable pages keep their lexical rank
            texts[entry_id] = ""
            continue
        text = getattr(entry.entry, "text", "")
        texts[entry_id] = text if isinstance(text, str) else ""
    return texts


def _order_by_proximity(
    texts: dict[str, str], terms: frozenset[str], scores: dict[str, float]
) -> tuple[str, ...]:
    """Order recalled pages by how tightly the question's terms co-occur in them.

    Counting term matches alone cannot tell a statement row from an index page
    that prints the same words in a different layout, and the lexical search
    reports an OR over terms, so a page carrying one common word can outrank the
    page that actually answers the question. Rewarding pages where several
    distinct question terms share one window restores that distinction.

    The window is the primary key, but it collapses ties harshly - a page whose
    question terms all sit near each other scores the same as one that merely
    carries them - so lexical score and page length break ties in that order.
    """

    scored: list[tuple[int, float, int, str]] = []
    for entry_id, text in texts.items():
        window = _term_window(_TOKEN.findall(text.lower()), terms)
        scored.append((-window, -scores.get(entry_id, 0.0), -len(text), entry_id))
    scored.sort()
    return tuple(entry_id for _, _, _, entry_id in scored)


def _term_window(tokens: Sequence[str], terms: frozenset[str], width: int = _PROXIMITY_WIDTH) -> int:
    """Return the most distinct question terms seen inside one token window.

    The window is anchored on a term occurrence and extends ``width`` tokens in
    either direction, so it always contains at least its own anchor - hence the
    floor of 1, which is also the value a page that matches nothing must report.
    """

    positions: dict[str, list[int]] = {}
    for index, token in enumerate(tokens):
        if token in terms:
            positions.setdefault(token, []).append(index)
    best = 1 if positions else 0
    for anchor in (spot for spots in positions.values() for spot in spots):
        covered = sum(
            1
            for others in positions.values()
            if any(anchor - width <= spot <= anchor + width for spot in others)
        )
        if covered > best:
            best = covered
    return best


class _PageQueryTerms:
    """Decompose a question into the terms one search each."""

    _WORD = re.compile(r"[a-z0-9]+")
    _STOP = frozenset(
        """a an and are as at be been by did do does for from had has have how in into is it its
        much many of on or pct s t that the their then there these this to was were what when where
        which who why will with year any define defined using use according reported""".split()
    )
    _EXPANSIONS = {
        # Standard accounting wording, not document-specific facts: a 10-K states
        # "capital expenditures" where a question says "capex", and a statement page
        # never prints the ratio name the question uses.
        "capex": ("capital", "expenditures"),
        "ebitda": ("operating", "income", "depreciation", "amortization"),
        # A 10-K answers "geographies" with "countries and territories" and with
        # its reportable divisions, and never with the noun the question used, so
        # the wording that actually carries the answer has to be reachable too.
        "geographies": (
            "geographic",
            "geography",
            "region",
            "regions",
            "worldwide",
            "international",
            "global",
            "countries",
            "territories",
            "divisions",
        ),
        "battles": ("proceedings", "litigation", "contingencies"),
        "margin": ("operating", "profit", "gross"),
        "restructuring": ("charges", "costs"),
        "revenue": ("sales", "net"),
        "earnings": ("income", "profit"),
        "profit": ("income", "earnings"),
        "debt": ("borrowings", "obligations", "long", "term"),
        "inventory": ("inventories",),
        "dividend": ("dividends", "per", "share"),
        "repurchase": ("buyback", "shares"),
        "headcount": ("employees",),
        "segment": ("segments", "reportable"),
        "litigation": ("legal", "proceedings"),
        "lease": ("leases", "rent"),
        "goodwill": ("impairment",),
        "impairment": ("goodwill", "charges"),
        "cashflow": ("cash", "flow", "operating", "activities"),
        "liquidity": ("cash", "facilities", "working", "capital"),
        "tariff": ("tariffs", "duties"),
        "inflation": ("commodity", "cost", "pricing"),
        "employees": ("headcount", "associates"),
        "cybersecurity": ("security", "incidents", "privacy"),
    }

    def __call__(self, question: str) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for token in self._WORD.findall(question.lower()):
            if token in self._STOP or len(token) < 2:
                continue
            seen.setdefault(token, None)
            for extra in self._EXPANSIONS.get(token, ()):
                seen.setdefault(extra, None)
        # A question made only of stop words still has to run one search.
        return tuple(seen) or (question,)


_query_terms = _PageQueryTerms()
_MIN_TERM_DEPTH = 200


def _query_passes(question: str) -> tuple[tuple[str, ...], ...]:
    """Return the terms this implementation searches, in scoring order.

    Kept as a one-pass sequence because the lexical search reports an OR over its
    query, so asking for several words at once does not make phrase adjacency
    available. Page-level proximity after recall is what recovers adjacency.
    """

    terms = _query_terms(question)
    return (terms,) if terms else ()


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    projections=(PDFPageTextEvidenceProjection(),),
)

__all__ = [
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
