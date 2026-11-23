"""Declarative site adapters: `spec.yaml` (selectors) -> records. Selectors first, code second.

The pure part (`SiteSpec`, `parse_html`) has no I/O and runs inside the offline sandbox.
Agents write/repair `spec.yaml`; most drift fixes become a one-line selector change.

spec.yaml:
    schema: product                            # any dataset type the registry knows
    record: "article.product_pod"              # one match = one record
    fields:
      url:   {css: "h3 a", attr: href}          # no transform: follows the field type (url)
      title: {css: "h3 a", attr: title}
      price: {css: p.price_color, transform: money}
      stock_quantity: {css: p.availability, pattern: '\\((\\d+) available', transform: int}
      rating: {css: p.star-rating, attr: class, pattern: 'star-rating (\\w+)',
               map: {One: 1, Two: 2, Three: 3, Four: 4, Five: 5}}
    next_page: {css: "li.next a", attr: href}

Meeting specs also use `remove` (child selectors dropped before reading text), the `status`
transform, `documents` (linked agenda/minutes) and `render` (Playwright options).

Reading one field: element (`css`, or the record itself) -> attribute or visible text ->
cleaned -> `pattern` (group 1, else the whole match) -> `map` (exact text -> value; a text
value still goes through the transform) -> transform -> `default` (also transformed) when
nothing was read. Transforms: text, url (= absolute_url), date, time, datetime, int, float,
decimal (= number), money, bool, status; a field without one is read as its dataset type.
Values are read under the source's locale. A value with more than one reading (03/04/2026
without a date order) turns that record into a reject naming the field and the problem.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from scrapeline.adapters.rawdoc import ParseResult, RawDoc, Reject, Target
from scrapeline.catalog.datasets import DatasetType, FieldType, default_registry
from scrapeline.errors import AmbiguousValueError, ConfigError, DatasetError, ParseError
from scrapeline.pipeline.identity import meeting_natural_key
from scrapeline.pipeline.locale import Locale
from scrapeline.pipeline.models import Document
from scrapeline.pipeline.normalise import (
    absolute_url,
    clean_text,
    parse_date,
    parse_datetime,
    parse_money,
    parse_number,
    parse_time,
)

if TYPE_CHECKING:
    from scrapeline.registry.config import SourceConfig

Transform = Literal[
    "text",
    "date",
    "time",
    "datetime",
    "absolute_url",
    "url",
    "int",
    "float",
    "decimal",
    "number",
    "money",
    "bool",
    "status",
]
Scalar = str | int | float | bool | None

IDENTITY_FIELDS = frozenset({"external_id", "canonical_url"})
# A field without an explicit transform is read as its dataset type.
_TYPE_TRANSFORMS: dict[FieldType, Transform] = {
    "text": "text",
    "enum": "text",
    "url": "absolute_url",
    "int": "int",
    "float": "float",
    "decimal": "decimal",
    "money": "money",
    "bool": "bool",
    "date": "date",
    "datetime": "datetime",
    "time": "time",
}
_BOOL_WORDS = {
    **dict.fromkeys(("1", "true", "yes", "y", "on"), True),
    **dict.fromkeys(("0", "false", "no", "n", "off"), False),
}
# A leading phrase followed by detail: 'In stock (22 available)', 'Sold out - back soon'.
_BOOL_PHRASE = re.compile(
    r"(?:(?P<yes>in stock|available|yes)|out of stock|sold out|not available|unavailable|no)\b",
    re.IGNORECASE,
)
_MAX_TEXT_IN_REASON = 80


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    css: str | None = None  # None = the record element itself
    attr: str | None = None  # None = visible text
    transform: Transform | None = None  # None = follow the dataset field's type
    pattern: str | None = Field(default=None, max_length=500)
    map: dict[str, Scalar] | None = None
    optional: bool = False
    default: Scalar = None
    remove: list[str] = Field(default_factory=list)  # child selectors removed before reading text

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid pattern {value!r}: {exc}") from exc
        return value

    @field_validator("map", mode="before")
    @classmethod
    def _text_keys(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        # YAML reads unquoted yes/no/on/off and numbers as non-text; the page text is text.
        odd = [k for k in value if not isinstance(k, str)]
        if odd:
            raise ValueError(f"map keys must be quoted text, got {odd!r}")
        return {clean_text(k) or "": v for k, v in value.items()}


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
    """Validate with `from_yaml(text, dataset=...)` to check against a dataset the registry
    does not hold (a user type shipped into the sandbox)."""

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
    def _known_schema(cls, value: str, info: ValidationInfo) -> str:
        try:
            resolve_dataset(value, _context_dataset(info))
        except DatasetError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @model_validator(mode="after")
    def _fields_exist_on_schema(self, info: ValidationInfo) -> SiteSpec:
        dataset = resolve_dataset(self.schema_, _context_dataset(info))
        unknown = set(self.fields) - set(dataset.fields) - IDENTITY_FIELDS
        if unknown:
            raise ValueError(f"fields not in {self.schema_}: {sorted(unknown)}")
        for doc in self.documents:
            if doc.field not in self.fields:
                raise ValueError(f"documents.field {doc.field!r} is not an extracted field")
        return self

    @classmethod
    def from_yaml(cls, text: str, *, dataset: DatasetType | None = None) -> SiteSpec:
        context = {"dataset": dataset} if dataset is not None else None
        try:
            return cls.model_validate(yaml.safe_load(text), context=context)
        except Exception as exc:  # yaml or validation
            raise ConfigError(f"invalid spec.yaml: {exc}") from exc


def _context_dataset(info: ValidationInfo) -> DatasetType | None:
    found = info.context.get("dataset") if isinstance(info.context, dict) else None
    return found if isinstance(found, DatasetType) else None


def resolve_dataset(name: str, dataset: DatasetType | None = None) -> DatasetType:
    """The dataset type a spec names. An explicit `dataset` must carry that name; otherwise
    the registry is asked (it accepts 'product', 'meeting' and legacy 'Meeting')."""
    registry = default_registry()
    if dataset is None:
        try:
            return registry.get(name)
        except DatasetError:
            raise DatasetError(f"unknown schema {name!r}; known: {registry.names()}") from None
    try:
        canonical = registry.canonical_name(name)
    except DatasetError:
        canonical = name.lower()
    if canonical != dataset.name:
        raise DatasetError(f"spec schema {name!r} does not match dataset {dataset.name!r}")
    return dataset


def _status(text: str | None, default: str | None) -> str | None:
    if text is None:
        return default
    low = text.lower()
    if "cancel" in low:
        return "cancelled"
    if "postpone" in low or "rescheduled" in low:
        return "postponed"
    return default or "scheduled"


def _bool(text: str) -> bool | None:
    """Yes/no words and stock phrases; other text is unreadable (use `map` for site words)."""
    if (word := _BOOL_WORDS.get(text.casefold())) is not None:
        return word
    phrase = _BOOL_PHRASE.match(text)
    if phrase is None:
        return None
    return phrase.group("yes") is not None


def _integer(number: Decimal | None) -> int | None:
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _convert(transform: Transform, text: str, base_url: str, locale: Locale) -> Any:
    match transform:
        case "text":
            return text
        case "date":
            return parse_date(text, locale=locale)
        case "datetime":
            return parse_datetime(text, locale=locale)
        case "time":
            return parse_time(text)
        case "absolute_url" | "url":
            return absolute_url(text, base_url)
        case "int":
            return _integer(parse_number(text, locale=locale))
        case "float":
            number = parse_number(text, locale=locale)
            return None if number is None else float(number)
        case "decimal" | "number":
            return parse_number(text, locale=locale)
        case "money":
            return parse_money(text, locale=locale)
        case "bool":
            return _bool(text)
        case "status":
            return _status(text, None)


def _element_text(node: Node, spec: FieldSpec) -> str | None:
    target: Node | None = node if spec.css is None else node.css_first(spec.css)
    if target is None:
        return None
    if spec.attr:
        return target.attributes.get(spec.attr)
    if spec.remove:
        target = HTMLParser(target.html or "").body or target
        for sel in spec.remove:
            for child in target.css(sel):
                child.decompose()
    return target.text(separator=" ")


def _extract(pattern: str, text: str) -> str | None:
    found = re.search(pattern, text)
    if found is None:
        return None
    return clean_text(found.group(1) if found.re.groups else found.group(0))


def _read(
    node: Node, spec: FieldSpec, transform: Transform, base_url: str, locale: Locale
) -> tuple[str | None, Any]:
    """(text found on the page, value). Text without a value means it could not be read."""
    text = clean_text(_element_text(node, spec))
    value: Scalar = text
    if text is not None and spec.pattern is not None:
        value = _extract(spec.pattern, text)
    if isinstance(value, str) and spec.map is not None and value in spec.map:
        value = spec.map[value]
    if transform == "status":
        default = spec.default if isinstance(spec.default, str) else None
        return text, _status(value if isinstance(value, str) else None, default)
    result: Any = _convert(transform, value, base_url, locale) if isinstance(value, str) else value
    if result is None and spec.default is not None:
        default_value = spec.default
        if isinstance(default_value, str):
            return text, _convert(transform, default_value, base_url, locale)
        return text, default_value
    return text, result


def _transform_for(name: str, spec: FieldSpec, dataset: DatasetType) -> Transform:
    if spec.transform is not None:
        return spec.transform
    field = dataset.fields.get(name)
    return _TYPE_TRANSFORMS[field.type] if field is not None else "text"


def _short(text: str) -> str:
    return text if len(text) <= _MAX_TEXT_IN_REASON else text[: _MAX_TEXT_IN_REASON - 3] + "..."


def parse_html(
    spec: SiteSpec,
    raw: RawDoc,
    *,
    locale: Locale,
    dataset: DatasetType | None = None,
    render: bool = False,
) -> ParseResult:
    """Pure: HTML bytes + spec -> ParseResult (records, rejects, next page).

    `dataset` defaults to the registry entry the spec names; pass it explicitly for a dataset
    type the registry does not hold.
    """
    dataset = resolve_dataset(spec.schema_, dataset)
    model = dataset.record_model()
    transforms = {name: _transform_for(name, f, dataset) for name, f in spec.fields.items()}
    tree = HTMLParser(raw.text)
    base = raw.final_url
    result = ParseResult()
    nodes = tree.css(spec.record)
    if len(nodes) < spec.min_records_per_page:
        raise ParseError(
            f"record selector {spec.record!r} matched {len(nodes)} element(s) "
            f"(expected >= {spec.min_records_per_page}) on {raw.url}"
        )
    for node in nodes:
        data: dict[str, Any] = {}
        texts: dict[str, str | None] = {}
        ambiguous: list[str] = []
        for name, f in spec.fields.items():
            try:
                texts[name], data[name] = _read(node, f, transforms[name], base, locale)
            except AmbiguousValueError as exc:
                texts[name], data[name] = None, None
                ambiguous.append(f"{name}: {exc}")
        if ambiguous:
            result.rejects.append(
                _reject(model.record_type, data, "ambiguous value(s): " + "; ".join(ambiguous))
            )
            continue
        absent = [n for n, f in spec.fields.items() if not f.optional and data[n] is None]
        missing = [n for n in absent if texts[n] is None]
        unreadable = {n: _short(t) for n in absent if (t := texts[n]) is not None}
        if dataset.name == "meeting" and not data.get("external_id"):
            data["external_id"] = meeting_natural_key(
                canonical_url=data.get("canonical_url"),
                body=data.get("body_name"),
                meeting_date=data.get("meeting_date"),
                title=data.get("title"),
            )
        if missing or unreadable:
            problems = [f"required field(s) not found: {missing}"] if missing else []
            if unreadable:
                problems.append(f"required field(s) unreadable: {unreadable}")
            result.rejects.append(_reject(model.record_type, data, "; ".join(problems)))
            continue
        before = len(result.records)
        result.add(model, data)
        if len(result.records) > before:
            owner = result.records[-1].external_id
            for doc in spec.documents:
                url = data.get(doc.field)
                if url:
                    result.add(
                        Document,
                        {
                            "external_id": url,
                            "meeting_external_id": owner,
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


def _reject(record_type: str, data: dict[str, Any], reason: str) -> Reject:
    safe = {
        k: (v if isinstance(v, str | int | float | bool | type(None)) else str(v))
        for k, v in data.items()
    }
    return Reject(record_type=record_type, data=safe, reason=reason)


# Runtime adapter (not used inside the sandbox)

SITES_DIR = Path(__file__).resolve().parent / "sites"


def spec_path_for(source_id: str) -> Path:
    return SITES_DIR / source_id.replace("-", "_") / "spec.yaml"


class DeclarativeAdapter:
    """Adapter whose parsing is fully described by adapters/sites/<source_id>/spec.yaml."""

    def __init__(self, source: SourceConfig, spec_path: Path | None = None) -> None:
        self.source = source
        self.spec_path = spec_path or spec_path_for(source.id)
        if not self.spec_path.exists():
            raise ConfigError(f"missing spec for {source.id}: {self.spec_path}")
        self.spec = SiteSpec.from_yaml(self.spec_path.read_text())
        self.dataset = resolve_dataset(self.spec.schema_)
        if self.dataset.name != source.dataset_name:
            raise ConfigError(
                f"{source.id}: spec collects {self.dataset.name!r} "
                f"but the source config declares {source.dataset_name!r}"
            )

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
        return parse_html(
            self.spec,
            raw,
            locale=self.source.locale,
            dataset=self.dataset,
            render=self.spec.render is not None,
        )
