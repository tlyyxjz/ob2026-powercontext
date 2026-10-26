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

"""Capture offline CSV records as independently addressable row Sources."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"
_CSV_ROW_PREFIX = "csv:"


class CSVDocumentInput(BaseModel):
    """Complete offline CSV bytes and the exact column used as row identity."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One validated CSV record with its document and header context."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A captured CSV row whose stable identity is independent of file order."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def _validate_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")


def _validate_row_contract(
    *,
    document_id: str,
    primary_key: str,
    primary_key_value: str,
    columns: tuple[str, ...],
    values: tuple[str, ...],
) -> None:
    _validate_document_id(document_id)
    if not isinstance(primary_key, str) or not primary_key:
        raise ValueError("primary_key must be a non-empty string")
    if not isinstance(primary_key_value, str) or not primary_key_value.strip():
        raise ValueError("primary_key_value must be a non-empty, non-blank string")
    if not columns:
        raise ValueError("columns must not be empty")
    if len(columns) != len(values):
        raise ValueError("columns and values must have the same length")
    if len(set(columns)) != len(columns):
        raise ValueError("column names must be unique")
    for column in columns:
        if not isinstance(column, str) or not column.strip():
            raise ValueError("column names must be non-empty and not blank")
    if primary_key not in columns:
        raise ValueError("primary_key must exactly match one column name")
    csv_row_source_id(document_id, primary_key_value)


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and fully validate a CSV document before returning any row."""

    _validate_document_id(value.document_id)
    if not isinstance(value.primary_key, str) or not value.primary_key:
        raise ValueError("primary_key must be a non-empty string")

    try:
        text = value.content.decode("utf-8-sig", errors="strict")
    except (UnicodeDecodeError, AttributeError) as error:
        raise ValueError("CSV content must be valid UTF-8 bytes") from error

    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as error:
        raise ValueError(f"malformed CSV: {error}") from error

    if not records or not records[0]:
        raise ValueError("CSV must contain a non-empty header row")

    columns = tuple(records[0])
    if any(not column.strip() for column in columns):
        raise ValueError("CSV header fields must be non-empty and not blank")
    if len(set(columns)) != len(columns):
        raise ValueError("CSV header fields must be unique")
    if value.primary_key not in columns:
        raise ValueError("primary_key must exactly match one CSV header field")

    primary_index = columns.index(value.primary_key)
    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()

    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(columns):
            raise ValueError("CSV record width must match the header width")
        primary_key_value = record[primary_index]
        if not primary_key_value.strip():
            raise ValueError("CSV primary key values must be non-empty and not blank")
        if primary_key_value in seen_keys:
            raise ValueError("CSV primary key values must be unique within one document")
        seen_keys.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )

    return tuple(rows)


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the delimiter-safe, bounded identity for one CSV row."""

    _validate_document_id(document_id)
    if not isinstance(primary_key_value, str) or not primary_key_value.strip():
        raise ValueError("primary_key_value must be a non-empty, non-blank string")
    source_id = (
        f"{_CSV_ROW_PREFIX}{quote(document_id, safe='')}:"
        f"{quote(primary_key_value, safe='')}"
    )
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


class CSVRowSourceAdapter:
    """Resolve one validated row into a durable captured Source."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        _validate_row_contract(
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        _validate_row_contract(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )
        expected_id = csv_row_source_id(source.document_id, source.primary_key_value)
        if source.source_id != expected_id or source.name != expected_id:
            raise ValueError("CSV row Source identity is inconsistent with its record")
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Project one row as a complete, ordered JSON record plus exact metadata."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    projections=(CSVRowTextEvidenceProjection(),),
)
