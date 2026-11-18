"""Declarative site adapters: `spec.yaml` (selectors) -> records. Selectors first, code second.

The pure part (`SiteSpec`, `parse_html`) has no I/O and runs inside the offline sandbox.
Agents write/repair `spec.yaml`; most drift fixes become a one-line selector change.

spec.yaml:
    schema: Meeting
    record: "table.meetings tbody tr"          # one match = one record
    fields:
      external_id: {attr: data-id}             # no css = the record element itself
      title:       {css: td.title, remove: [em.status]}
      meeting_date: {css: td.date, transform: date}
      status:      {css: em.status, transform: status, optional: true, default: scheduled}
      agenda_url:  {css: a.agenda, attr: href, transform: absolute_url, optional: true}
    next_page: {css: "a[rel=next]", attr: href}
    documents: [{field: agenda_url, kind: agenda}]
    render: {wait_for: "tr.meeting", click_until_gone: "#load-more", max_clicks: 10}
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from scrapeline.adapters.rawdoc import ParseResult, RawDoc, Target
from scrapeline.errors import ConfigError, ParseError
from scrapeline.pipeline.identity import meeting_natural_key
from scrapeline.pipeline.models import SCHEMAS, Document
from scrapeline.pipeline.normalise import absolute_url, clean_text, parse_date, parse_time

Transform = Literal["text", "date", "time", "absolute_url", "int", "status", "bool"]


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    css: str | None = None  # None = the record element itself
    attr: str | None = None  # None = visible text
    transform: Transform = "text"
    optional: bool = False
    default: str | None = None
    remove: list[str] = Field(default_factory=list)  # child selectors removed before reading text


class LinkSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    css: str
    attr: str = "href"


class DocumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    kind: Literal["agenda", "minutes", "packet", "other"] = "other"


class RenderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    wait_for: str | None = None
    click_until_gone: str | None = None
    max_clicks: int = Field(default=10, ge=0, le=50)


class SiteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_version: int = 1
    schema_: str = Field(default="Meeting", alias="schema")
    record: str
    fields: dict[str, FieldSpec]
    next_page: LinkSpec | None = None
    documents: list[DocumentSpec] = Field(default_factory=list)
    render: RenderSpec | None = None
    min_records_per_page: int = 1

    @field_validator("schema_")
    @classmethod
    def _known_schema(cls, value: str) -> str:
        if value not in SCHEMAS:
            raise ValueError(f"unknown schema {value!r}; known: {sorted(SCHEMAS)}")
        return value

    @model_validator(mode="after")
    def _fields_exist_on_schema(self) -> SiteSpec:
        allowed = set(SCHEMAS[self.schema_].model_fields)
        unknown = set(self.fields) - allowed
        if unknown:
            raise ValueError(f"fields not in {self.schema_}: {sorted(unknown)}")
        for doc in self.documents:
            if doc.field not in self.fields:
                raise ValueError(f"documents.field {doc.field!r} is not an extracted field")
        return self

    @classmethod
    def from_yaml(cls, text: str) -> SiteSpec:
        try:
            return cls.model_validate(yaml.safe_load(text))
        except Exception as exc:  # yaml or validation
            raise ConfigError(f"invalid spec.yaml: {exc}") from exc


def _status(text: str | None, default: str | None) -> str | None:
    if text is None:
        return default
    low = text.lower()
    if "cancel" in low:
        return "cancelled"
    if "postpone" in low or "rescheduled" in low:
        return "postponed"
    return default or "scheduled"


def _read(node: Node, spec: FieldSpec, base_url: str) -> Any:
    target: Node | None = node if spec.css is None else node.css_first(spec.css)
    if target is None:
        raw: str | None = None
    elif spec.attr:
        raw = target.attributes.get(spec.attr)
    else:
        if spec.remove:
            target = HTMLParser(target.html or "").body or target
            for sel in spec.remove:
                for child in target.css(sel):
                    child.decompose()
        raw = target.text(separator=" ")
    text = clean_text(raw)
    match spec.transform:
        case "text":
            value: Any = text
        case "date":
            value = parse_date(text)
        case "time":
            value = parse_time(text)
        case "absolute_url":
            value = absolute_url(text, base_url)
        case "int":
            value = int(text) if text and text.lstrip("-").isdigit() else None
        case "bool":
            value = None if text is None else text.lower() in {"1", "true", "yes", "y"}
        case "status":
            return _status(text, spec.default)
    if value is None and spec.default is not None:
        return spec.default
    return value


def parse_html(spec: SiteSpec, raw: RawDoc, *, render: bool = False) -> ParseResult:
    """Pure: HTML bytes + spec -> ParseResult (records, rejects, next page)."""
    tree = HTMLParser(raw.text)
    base = raw.final_url
    model = SCHEMAS[spec.schema_]
    result = ParseResult()
    nodes = tree.css(spec.record)
    if len(nodes) < spec.min_records_per_page:
        raise ParseError(
            f"record selector {spec.record!r} matched {len(nodes)} element(s) "
            f"(expected >= {spec.min_records_per_page}) on {raw.url}"
        )
    for node in nodes:
        data: dict[str, Any] = {name: _read(node, f, base) for name, f in spec.fields.items()}
        missing = [n for n, f in spec.fields.items() if not f.optional and data[n] is None]
        if spec.schema_ == "Meeting" and not data.get("external_id"):
            data["external_id"] = meeting_natural_key(
                canonical_url=data.get("canonical_url"),
                body=data.get("body_name"),
                meeting_date=data.get("meeting_date"),
                title=data.get("title"),
            )
        if missing:
            result.rejects.append(
                _reject(model.record_type, data, f"required field(s) not found: {missing}")
            )
            continue
        before = len(result.records)
        result.add(model, data)
        if len(result.records) > before:
            for doc in spec.documents:
                url = data.get(doc.field)
                if url:
                    result.add(
                        Document,
                        {
                            "external_id": url,
                            "meeting_external_id": data["external_id"],
                            "kind": doc.kind,
                            "url": url,
                            "title": data.get("title"),
                        },
                    )
    if spec.next_page is not None:
        link = tree.css_first(spec.next_page.css)
        href = link.attributes.get(spec.next_page.attr) if link is not None else None
        nxt = absolute_url(href, base)
        if nxt and nxt != raw.url:
            page = int(raw.meta.get("page", 1)) + 1
            result.next_targets.append(
                Target(url=nxt, kind="page", meta={"page": page}, render=render)
            )
    return result


def _reject(record_type: str, data: dict[str, Any], reason: str) -> Any:
    from scrapeline.adapters.rawdoc import Reject

    safe = {
        k: (v if isinstance(v, str | int | float | bool | type(None)) else str(v))
        for k, v in data.items()
    }
    return Reject(record_type=record_type, data=safe, reason=reason)


# -- runtime adapter (not used inside the sandbox) -------------------------------------------

SITES_DIR = Path(__file__).resolve().parent / "sites"


def spec_path_for(source_id: str) -> Path:
    return SITES_DIR / source_id.replace("-", "_") / "spec.yaml"


class DeclarativeAdapter:
    """Adapter whose parsing is fully described by adapters/sites/<source_id>/spec.yaml."""

    def __init__(self, source: Any, spec_path: Path | None = None) -> None:
        self.source = source
        self.spec_path = spec_path or spec_path_for(source.id)
        if not self.spec_path.exists():
            raise ConfigError(f"missing spec for {source.id}: {self.spec_path}")
        self.spec = SiteSpec.from_yaml(self.spec_path.read_text())

    def discover(self, window: Any) -> Iterator[Target]:
        yield Target(
            url=self.source.entry,
            kind="page",
            meta={"page": 1},
            render=self.spec.render is not None,
        )

    def fetch(self, target: Target, client: Any) -> Any:
        return client.get(target.url)

    @property
    def render_options(self) -> RenderSpec | None:
        return self.spec.render

    def parse(self, raw: RawDoc) -> ParseResult:
        return parse_html(self.spec, raw, render=self.spec.render is not None)
