"""Record schemas. Every record carries `schema_version` (CLAUDE.md §4.1).

Parsers build these; validation failures become rows in `pipeline.rejects`, never silent drops.
Lineage columns (run_id, raw_sha256, ...) are added by the pipeline, not by parsers.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from scrapeline.pipeline.normalise import clean_text

MeetingStatus = Literal["scheduled", "cancelled", "postponed"]
DocumentKind = Literal["agenda", "minutes", "packet", "other"]


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    record_type: ClassVar[str]
    schema_version: ClassVar[int]
    # Fields that do not participate in content_hash (identity / provenance).
    hash_exclude: ClassVar[frozenset[str]] = frozenset({"external_id", "canonical_url"})

    external_id: str
    canonical_url: str | None = None

    @field_validator("external_id")
    @classmethod
    def _external_id_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("external_id is empty")
        return value

    @field_validator("canonical_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("canonical_url must be http(s)")
        return value


class Body(Record):
    record_type: ClassVar[str] = "body"
    schema_version: ClassVar[int] = 1

    name: str
    body_type: str | None = None
    active: bool | None = None

    @field_validator("name", "body_type", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        return clean_text(value) if isinstance(value, str) else value


class Meeting(Record):
    record_type: ClassVar[str] = "meeting"
    schema_version: ClassVar[int] = 1
    hash_exclude: ClassVar[frozenset[str]] = frozenset(
        {"external_id", "canonical_url", "source_modified_at"}
    )

    title: str
    meeting_date: date
    start_time: time | None = None
    body_external_id: str | None = None
    body_name: str | None = None
    location: str | None = None
    status: MeetingStatus = "scheduled"
    agenda_url: str | None = None
    minutes_url: str | None = None
    source_modified_at: datetime | None = None

    @field_validator("title", "body_name", "location", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        return clean_text(value) if isinstance(value, str) else value

    @field_validator("title")
    @classmethod
    def _title_present(cls, value: str) -> str:
        if not value:
            raise ValueError("title is empty")
        return value

    @field_validator("meeting_date")
    @classmethod
    def _plausible_date(cls, value: date) -> date:
        if not date(1990, 1, 1) <= value <= date(2100, 1, 1):
            raise ValueError(f"implausible meeting_date {value}")
        return value

    @field_validator("agenda_url", "minutes_url")
    @classmethod
    def _doc_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("document url must be http(s)")
        return value


class Document(Record):
    """A document linked from a meeting. external_id = its URL (stable for most portals)."""

    record_type: ClassVar[str] = "document"
    schema_version: ClassVar[int] = 1

    meeting_external_id: str | None = None
    kind: DocumentKind = "other"
    title: str | None = None
    url: str

    @field_validator("url")
    @classmethod
    def _doc_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("document url must be http(s)")
        return value


class AgendaItem(Record):
    record_type: ClassVar[str] = "agenda_item"
    schema_version: ClassVar[int] = 1

    meeting_external_id: str
    sequence: int | None = None
    item_number: str | None = None
    title: str
    action: str | None = None
    matter_type: str | None = None

    @field_validator("title", "action", "matter_type", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        return clean_text(value) if isinstance(value, str) else value


RECORD_TYPES: dict[str, type[Record]] = {
    cls.record_type: cls for cls in (Body, Meeting, Document, AgendaItem)
}
SCHEMAS: dict[str, type[Record]] = {
    cls.__name__: cls for cls in (Body, Meeting, Document, AgendaItem)
}
