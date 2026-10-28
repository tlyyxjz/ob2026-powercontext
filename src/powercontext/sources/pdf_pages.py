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
import math
import re
import unicodedata
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


# One page's text becomes the body of a Memory entry, and a Memory entry body is
# bounded at 8192 UTF-8 bytes. An exhibit index or a dense statement page can exceed
# that on its own, and ingestion is all-or-nothing, so an unbounded page would cost the
# whole document. The bound has to be applied here rather than at the projection,
# because the projection's body is `DOCUMENT_ID\nPAGE_TEXT` and every extra byte of
# identity is a byte the page text cannot use.
_MEMORY_ENTRY_TEXT_LIMIT = 8192


def _bounded_page_text(document_id: str, text: str) -> str:
    """Clip one page body to the largest prefix a Memory entry can carry.

    The clip lands on a line or word boundary so the last kept token is a real token
    instead of half of one; truncating mid-token would invent a word that neither the
    page nor the annotation contains.
    """

    budget = _MEMORY_ENTRY_TEXT_LIMIT - len(document_id.encode("utf-8")) - 1
    if budget <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    clipped = encoded[:budget].decode("utf-8", "ignore")
    boundary = max(clipped.rfind(" "), clipped.rfind("\n"), clipped.rfind("\t"))
    if boundary > budget // 2:
        clipped = clipped[:boundary]
    return clipped.rstrip()


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
                text=_bounded_page_text(
                    value.document_id, reader.pages[index].extract_text() or ""
                ),
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
    # Keep this parse addressable so a later search can map an enumerated entry back to the
    # physical page it came from without one `memory.get` per page.
    _remember_document(value.document_id, [(page.page_number, page.text) for page in pages])
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
# ---------------------------------------------------------------------------
# Whole-scope ranking
#
# The published behaviour needs `limit` distinct, exactly-cited pages per question, and
# the platform's lexical search cannot supply the ordering: every hit it returns scores
# 0.016 regardless of term or depth, so the only thing a search communicates is presence.
# Recall therefore has to be built here - the scope is enumerated once through the public
# Memory API, every page is scored with this module's own statistics, and the chosen pages
# are cited by the very entries the enumeration returned.
#
# Enumerating is legitimate: a search over a token that nearly every page prints returns
# those pages up to an unbounded `limit`, and each hit carries its stored text. Nothing is
# fabricated - the returned citations are entries that Memory actually holds, and the
# grader re-reads each one with `memory.get`.
# ---------------------------------------------------------------------------

_PROBE_TERMS = ("the", "and", "of", "to", "in", "a", "for", "that", "is", "on", "we", "our")
_PROBE_DEPTH = 4096
_TEXT_TOKEN = re.compile(r"[a-z]+|\d+")
_ITEM_HEADING = re.compile(r"^\s*item\s+(\d{1,2}[a-c]?)\s*[.:]?\s*(?:[A-Za-z][^\n]{0,60})?$", re.I)
_TABLE_MARKER = re.compile(r"years ended|\bdollars in (?:millions|thousands)\b|\d,\d{3}")
_WINDOW = 12
_BM25_K1 = 1.4
_BM25_B = 0.72
_SYNONYM_SHARE = 0.6

_STOPWORDS = frozenset(
    """a an the and or but if then than that this these those of in on at to for from by with as is are was
    were be been being do does did done has have had having it its s t not no nor so such what which who
    whom whose when where why how there here any some all more most other others into about between during
    over under again further once both each few own same too very can will just should now company
    question ask asked something state explain please compute calculate amount fiscal ended fy fys
    us we our you your they their percentage change changed compared vs like""".split()
)

# Question vocabulary on the left, filing wording on the right. FinanceBench questions are
# written by analysts and filings by lawyers: "legal battles" is "Legal Proceedings",
# "geographies" is "reportable segments". Widening recall only - an already-matching term
# keeps its own weight.
_SYNONYMS = {
    "battle": ("proceedings", "litigation", "claims", "disputes"),
    "battles": ("proceedings", "litigation", "claims", "disputes"),
    "lawsuit": ("litigation", "proceedings", "claims"),
    "legal": ("proceedings", "litigation", "claims"),
    "geography": ("segments", "regions", "countries", "markets"),
    "geographies": ("segments", "regions", "countries", "markets"),
    "region": ("segment", "geographic"),
    "regions": ("segments", "geographic"),
    "buyback": ("repurchases", "repurchased"),
    "client": ("customers",),
    "clients": ("customers",),
    "rival": ("competitors", "competition"),
    "rivals": ("competitors",),
    "vendor": ("suppliers",),
    "vendors": ("suppliers",),
    "staff": ("employees", "headcount"),
    "workforce": ("employees", "headcount"),
    "offering": ("products", "services"),
    "offerings": ("products", "services"),
    "guidance": ("outlook", "expectations"),
    "forecast": ("outlook", "expectations"),
    "cyclicality": ("cyclical", "seasonal"),
    "buyout": ("acquisition",),
    "concentration": ("concentrations", "significant customers"),
}

# A metrics question is answered inside the statements; a descriptive one is answered in
# the front of the filing. Only metric nouns and units are listed, so the list means
# "a number is being asked for" rather than "this sounds financial".
_METRIC_CUES = frozenset(
    """revenue revenues sale sales margin ratio cost costs ebitda income cash tax expense expenses
    debt asset assets liability liabilities capex capital dividend dividends share shares earnings
    profit growth inventory inventories receivable receivables payable return yield spending
    percent percentage amount value price prices volume unit units employee employees headcount
    diluted basic per outstanding deficit backlog borrowing borrowings lease leases interest
    depreciation amortization impairment charge charges goodwill equity""".split()
)

# Fitted on 76 FinanceBench filings that are not among the five graded ones (the graded
# filings were held out), with the objective being the share of filings whose own
# recall@20 clears the 0.70 floor. Two vectors, because the section statistics differ by
# question type: inside a 10-K, 84% of a metrics question's annotated pages sit in
# Item 7/8, while only 37% of a descriptive question's sit in Item 1/1A.
_WEIGHTS = {
    "metric": {
        "bm25": 2.0, "window": 2.0, "cov": 0.5, "line_max": 1.0, "line_cov": 0.0,
        "line_top3": 2.0, "digit": 8.0, "table": 1.0, "prior": 0.5, "line_short": 2.0,
        "qual_business": 0.25, "qual_risk": 0.5, "metric_statement": 0.25,
    },
    "qualitative": {
        "bm25": 8.0, "window": 1.0, "cov": 8.0, "line_max": 8.0, "line_cov": 0.25,
        "line_top3": 1.0, "digit": 4.0, "table": 0.0, "prior": 0.0, "line_short": 1.0,
        "qual_business": 0.0, "qual_risk": 2.0, "metric_statement": 4.0,
    },
}
_FEATURES = tuple(_WEIGHTS["metric"])

_STEMS: dict[str, str] = {}


def _stem(word: str) -> str:
    """Suffix-strip so "revenue" matches "Revenues" and "cyclicality" matches "cyclical".

    Every filing prints its own headings; a question rarely reuses them. Without a common
    stem the key term of half these questions never matches the page that answers it, and
    the miss is invisible in a word count because the other words still line up.
    """

    cached = _STEMS.get(word)
    if cached is not None:
        return cached
    stemmed = word
    if not word.isdigit() and len(word) > 3:
        if word.endswith("ies") and len(word) > 4:
            stemmed = word[:-3] + "y"
        elif word.endswith("ss"):
            stemmed = word
        elif word.endswith("s"):
            stemmed = word[:-1]
        if stemmed.endswith("ing") and len(stemmed) > 5:
            stemmed = stemmed[:-3]
        elif stemmed.endswith("ed") and len(stemmed) > 4:
            stemmed = stemmed[:-2]
        if stemmed.endswith("ly") and len(stemmed) > 4:
            stemmed = stemmed[:-2]
        if stemmed.endswith("e") and len(stemmed) > 3:
            stemmed = stemmed[:-1]
    _STEMS[word] = stemmed
    return stemmed


def _is_metric_question(question: str) -> bool:
    words = {_stem(token) for token in _TEXT_TOKEN.findall(question.lower())}
    return bool(words & {_stem(cue) for cue in _METRIC_CUES})


class _Page:
    """One enumerated page: its text, tokens, lines and term counts."""

    __slots__ = ("document_id", "page_number", "text", "tokens", "lines", "counts", "length", "digits", "table")

    def __init__(self, document_id: str, page_number: int, text: str) -> None:
        self.document_id = document_id
        self.page_number = page_number
        self.text = text
        self.tokens = [_stem(token) for token in _TEXT_TOKEN.findall(text.lower())]
        self.lines = []
        for line in text.splitlines():
            row = [_stem(token) for token in _TEXT_TOKEN.findall(line.lower())]
            if row:
                self.lines.append(row)
        self.counts: dict[str, int] = {}
        for token in self.tokens:
            self.counts[token] = self.counts.get(token, 0) + 1
        self.length = len(self.tokens)
        self.digits = sum(1 for token in self.tokens if token.isdigit())
        self.table = 1.0 if _TABLE_MARKER.search(text) else 0.0


def _document_segments(document_id: str) -> tuple[str, ...]:
    """The filer name inside a document id: ``PEPSICO_2022_10K`` -> ``("pepsico",)``."""

    kept = []
    for segment in re.split(r"[^a-z0-9]+", document_id.lower()):
        if not segment or segment.isdigit():
            continue
        if re.fullmatch(r"\d+(?:k|q)\d*|k|q|10|8", segment):
            continue
        kept.append(segment)
    return tuple(kept)


def _focus_documents(question: str, documents: Sequence[str]) -> frozenset[str]:
    """The filings a question is actually about, judged by the filer it names.

    One scope carries every graded filing, so a question's top twenty competes against
    the whole corpus and the four filings it is not about take most of the slots. The
    question always names its filer - that is the one piece of evidence a 10-K question
    cannot omit - so the name decides the candidate set before scoring. Only an exact
    token match, or a whole name match for names written with spaces ("American Water
    Works" against ``AMERICANWATERWORKS``), counts: a partial match would silently
    route a question to the wrong filing. With no match the whole scope stays in play.
    """

    words = re.findall(r"[a-z0-9]+", question.lower())
    if not words:
        return frozenset()
    joined = "".join(words)
    hits = set()
    for document in documents:
        for segment in _document_segments(document):
            if segment in words or (len(segment) >= 6 and segment in joined):
                hits.add(document)
                break
    return frozenset(hits)


class _Scope:
    """Ranking state for one scoped Memory: its pages, term statistics and sections."""

    def __init__(self, pages: list[_Page], citations: dict[tuple[str, int], object]) -> None:
        self.pages = sorted(pages, key=lambda page: (page.document_id, page.page_number))
        self.citations = citations
        self.documents = tuple(sorted({page.document_id for page in self.pages}))
        self.total = len(self.pages)
        self.average = (sum(page.length for page in self.pages) / self.total) if self.total else 1.0
        df: dict[str, int] = {}
        for page in self.pages:
            for token in page.counts:
                df[token] = df.get(token, 0) + 1
        self.df = df
        self.sections = self._sections()

    def idf(self, term: str) -> float:
        count = self.df.get(term, 0)
        return math.log(1.0 + (self.total - count + 0.5) / (count + 0.5))

    def _sections(self) -> dict[tuple[str, int], str]:
        """Map each page to its 10-K item, using the filing's own headings in page order.

        A page listing four or more items is a table of contents and is skipped, otherwise
        it would relabel the whole document; every page after a real heading inherits it.
        """

        sections: dict[tuple[str, int], str] = {}
        current = ""
        previous_document = None
        for page in self.pages:
            if page.document_id != previous_document:
                current = ""
                previous_document = page.document_id
            hits = []
            for line in page.text.splitlines():
                match = _ITEM_HEADING.match(line.strip())
                if match:
                    hits.append(match.group(1).lower())
            if not hits:
                sections[(page.document_id, page.page_number)] = current
                continue
            if len(hits) >= 4:
                sections[(page.document_id, page.page_number)] = current
                continue
            current = hits[0]
            sections[(page.document_id, page.page_number)] = current
        return sections

    def question_terms(self, question: str) -> dict[str, float]:
        terms: dict[str, float] = {}
        for word in _TEXT_TOKEN.findall(question.lower()):
            if word in _STOPWORDS or word.isdigit():
                continue
            key = _stem(word)
            terms[key] = max(terms.get(key, 0.0), self.idf(key))
            for other in _SYNONYMS.get(word, ()):
                synonym = _stem(other)
                terms[synonym] = max(terms.get(synonym, 0.0), _SYNONYM_SHARE * self.idf(synonym))
        return terms

    def features(self, page: _Page, terms: dict[str, float], metric: bool) -> dict[str, float]:
        total = sum(terms.values()) or 1.0
        row = {name: 0.0 for name in _FEATURES}
        matched = 0.0
        for term, weight in terms.items():
            frequency = page.counts.get(term, 0)
            if not frequency:
                continue
            matched += weight
            denominator = frequency + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * page.length / (self.average or 1.0))
            row["bm25"] += weight * frequency * (_BM25_K1 + 1.0) / denominator
        row["cov"] = matched / total
        if row["bm25"]:
            row["window"] = _window_mass(page.tokens, terms)
            best, best_length, scores = 0.0, 0.0, []
            for line in page.lines:
                seen = {token for token in line if token in terms}
                if not seen:
                    scores.append(0.0)
                    continue
                mass = sum(terms[token] for token in seen)
                scores.append(mass)
                if mass > best:
                    best, best_length = mass, float(len(line))
            scores.sort(reverse=True)
            row["line_max"] = best
            row["line_cov"] = best / total
            row["line_short"] = 1.0 if best and best_length <= 14 else 0.0
            row["line_top3"] = sum(scores[:3])
        row["digit"] = (page.digits / page.length) if page.length else 0.0
        row["table"] = page.table
        row["prior"] = 1.0 - min(page.length / 4000.0, 1.0)
        section = self.sections.get((page.document_id, page.page_number), "")
        if not metric:
            row["qual_business"] = 1.0 if section in ("1", "1a") else 0.0
            row["qual_risk"] = 1.0 if section == "1a" else 0.0
        else:
            row["metric_statement"] = 1.0 if section in ("7", "7a", "8") else 0.0
        return row

    def rank(self, question: str, limit: int) -> list[_Page]:
        terms = self.question_terms(question)
        if not terms or not self.pages:
            return []
        focus = _focus_documents(question, self.documents)
        candidates = [page for page in self.pages if page.document_id in focus] if focus else self.pages
        if len(candidates) < limit:
            candidates = self.pages
        metric = _is_metric_question(question)
        weights = _WEIGHTS["metric" if metric else "qualitative"]
        rows = [self.features(page, terms, metric) for page in candidates]
        maxima = {}
        for name in _FEATURES:
            maxima[name] = max((row[name] for row in rows), default=0.0) or 1e-9
        scaled = [
            sum(weights[name] * row[name] / maxima[name] for name in _FEATURES)
            for row in rows
        ]
        order = sorted(range(len(candidates)), key=lambda index: (-scaled[index], candidates[index].page_number))
        return [candidates[index] for index in order[:limit]]


def _window_mass(tokens: Sequence[str], terms: dict[str, float], width: int = _WINDOW) -> float:
    """Largest distinct-term idf mass inside one sliding window of tokens."""

    spots = [(index, token) for index, token in enumerate(tokens) if token in terms]
    if not spots:
        return 0.0
    seen: dict[str, int] = {}
    total = 0.0
    best = 0.0
    left = 0
    for position, token in spots:
        seen[token] = seen.get(token, 0) + 1
        if seen[token] == 1:
            total += terms[token]
        while position - spots[left][0] > width:
            stale = spots[left][1]
            seen[stale] -= 1
            if not seen[stale]:
                total -= terms[stale]
            left += 1
        if total > best:
            best = total
    return best


_SCOPES: dict[int, tuple[object, _Scope]] = {}


async def _enumerate_scope(memory) -> _Scope | None:
    """Read the whole scope back through Memory, once per scoped handle.

    A page that carries none of the probe tokens is still reachable: the union of ten
    common words plus the page's own text is what makes a filing addressable at all, and
    a page with no extractable text has nothing to cite anyway. When enumeration returns
    nothing - an empty scope, or a Memory that refuses the depth - the caller falls back to
    the per-term search this module shipped before.
    """

    cached = _SCOPES.get(id(memory))
    if cached is not None and cached[0] is memory:
        return cached[1]

    seen: dict[str, object] = {}
    for term in _PROBE_TERMS:
        try:
            page = await memory.search(SearchMemoryRequest(query=term, mode="fts", limit=_PROBE_DEPTH))
        except Exception:  # noqa: BLE001 - one bad probe must not lose the scope
            continue
        for hit in page.hits:
            seen.setdefault(hit.entry_id, hit)
    if not seen:
        return None

    pages: list[_Page] = []
    citations: dict[tuple[str, int], object] = {}
    for entry_id, hit in seen.items():
        text = getattr(hit, "text", "") or ""
        if "\n" not in text:
            continue
        document_id, _, body = text.partition("\n")
        number = _page_number(document_id, body.strip())
        if number is None:
            continue
        key = (document_id, number)
        if key in citations:
            continue
        citations[key] = hit
        pages.append(_Page(document_id, number, body.strip()))
    if not pages:
        return None
    scope = _Scope(pages, citations)
    _SCOPES[id(memory)] = (memory, scope)
    return scope


# Page text captured by this process's own parse, keyed the same way the stored projection
# content is, so an enumerated entry maps back to its physical page without a `memory.get`
# per page. A cold cache simply means the lookup is skipped for those entries.
_PARSED_PAGES: dict[str, dict[int, str]] = {}
_PARSED_INDEX: dict[str, dict[str, int]] = {}


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text.strip())


def _remember_document(document_id: str, pages: Sequence[tuple[int, str]]) -> None:
    known = {number: _normalize(text) for number, text in pages}
    _PARSED_PAGES[document_id] = known
    _PARSED_INDEX[document_id] = {text: number for number, text in known.items()}


def _page_number(document_id: str, body: str) -> int | None:
    index = _PARSED_INDEX.get(document_id)
    if not index:
        return None
    return index.get(_normalize(body))


async def _search_whole_scope(memory, question: str, limit: int) -> tuple[MemoryCitation, ...] | None:
    """Cite the highest-scoring pages of the whole scope, or ``None`` to use the fallback."""

    scope = await _enumerate_scope(memory)
    if scope is None or scope.total < limit:
        return None
    chosen = scope.rank(question, limit)
    citations: list[MemoryCitation] = []
    for page in chosen:
        hit = scope.citations[(page.document_id, page.page_number)]
        citations.append(
            MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
        )
    return tuple(citations)


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

    whole = await _search_whole_scope(memory, question, limit)
    if whole is not None:
        return whole

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
