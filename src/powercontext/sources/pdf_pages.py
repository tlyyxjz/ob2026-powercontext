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
    that states the figure. The offline query pass therefore also asks for short
    phrases built from consecutive question terms. A phrase keeps the ordering and
    adjacency the question itself chose ("net sales", "capital expenditures"),
    which is what separates the statement page from the index page that lists the
    same words apart. The two passes are fused by rank, so a page found by both is
    ranked above a page that only one pass happened to score highly.

    Retrieval ranks entries, not pages, so a document may contribute several hits that
    resolve to the same page. The returned citation tuple never does: each accepted
    citation is dropped as soon as its page identity was already claimed.
    """

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")

    depth = max(limit * 10, _MIN_TERM_DEPTH)
    rankings: list[tuple[str, ...]] = []
    first_hit: dict[str, object] = {}
    for terms in _query_passes(question):
        scores: dict[str, float] = {}
        for term in terms:
            page = await memory.search(SearchMemoryRequest(query=term, mode="fts", limit=depth))
            for hit in page.hits:
                # Accumulate within a pass: a page that matches several question terms
                # supports the answer better than one that matches a single term once.
                scores[hit.entry_id] = scores.get(hit.entry_id, 0.0) + float(hit.score)
                first_hit.setdefault(hit.entry_id, hit)
        if scores:
            rankings.append(tuple(sorted(scores, key=lambda entry_id: scores[entry_id], reverse=True)))

    citations: list[MemoryCitation] = []
    claimed: set[tuple[str, ...]] = set()
    for entry_id in _reciprocal_rank_fusion(rankings):
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


def _reciprocal_rank_fusion(rankings: Sequence[Sequence[str]]) -> tuple[str, ...]:
    """Merge per-pass rankings without letting one pass's score scale dominate.

    Each pass contributes ``1 / (RRF_OFFSET + rank)`` per entry, so a page ranked
    highly by the phrase pass and by the term pass wins over a page that only one
    pass happened to score highly, no matter how different the two raw score
    ranges are.
    """

    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, entry_id in enumerate(ranking):
            fused[entry_id] = fused.get(entry_id, 0.0) + 1.0 / (_RRF_OFFSET + rank)
    return tuple(sorted(fused, key=lambda entry_id: (-fused[entry_id], entry_id)))


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
        "geographies": ("geographic", "geography", "region", "regions", "worldwide", "international", "global"),
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
# Fusion offset: a pass that ranks an entry first contributes 1/1, and the decay is
# steep enough that "second in two passes" still beats "first in one pass".
_RRF_OFFSET = 1
# Consecutive question terms above this count make a phrase too specific for a
# statement page, which words its headings differently from the question.
_MAX_PHRASE_TERMS = 3


def _query_passes(question: str) -> tuple[tuple[str, ...], ...]:
    """Return the ordered offline query passes this implementation searches.

    The first pass is the single-term decomposition that makes a figure-bearing page
    reachable at all. The second asks for adjacent-term phrases, which keeps the
    question's own word order and is what tells a statement row apart from an index
    page that happens to carry the same words in a different layout.
    """

    passes: list[tuple[str, ...]] = []
    terms = _query_terms(question)
    if terms:
        passes.append(terms)
    phrases = _query_phrases(question)
    if phrases:
        passes.append(phrases)
    return tuple(passes)


def _query_phrases(question: str) -> tuple[str, ...]:
    """Build ordered phrases from contiguous question terms, longest first."""

    content = [token for token in _PageQueryTerms._WORD.findall(question.lower()) if token not in _PageQueryTerms._STOP]
    phrases: list[str] = []
    for size in range(min(_MAX_PHRASE_TERMS, len(content)), 1, -1):
        for start in range(len(content) - size + 1):
            phrase = " ".join(content[start : start + size])
            if phrase not in phrases:
                phrases.append(phrase)
    return tuple(phrases)


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
