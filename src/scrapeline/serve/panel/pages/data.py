"""Data explorer: dataset types, their records, and each record's lineage and history.

Generic datasets live in `data.records` with append-only history in `data.record_versions`.
Typed datasets (the civic pack) have tables of their own that keep only the current row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Connection,
    Table,
    cast,
    exists,
    func,
    literal,
    null,
    or_,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import distinct_on

from scrapeline.catalog.datasets import (
    NAME_PATTERN,
    DatasetRegistry,
    DatasetType,
    FieldDef,
    default_registry,
)
from scrapeline.errors import DatasetError
from scrapeline.pipeline.load import TABLE_FOR
from scrapeline.serve.panel import filters
from scrapeline.serve.panel.deps import EngineDep, Page, PageDep, SettingsDep, render
from scrapeline.settings import Settings
from scrapeline.store import tables as t

router = APIRouter()

MAX_DATASETS = 500
MAX_SOURCE_OPTIONS = 200
MAX_VERSIONS = 100
MAX_TEXT = 160
MAX_URL_TEXT = 48
# Record lists stay readable on a laptop screen: shorter values and at most this many fields.
LIST_TEXT = 80
LIST_URL_TEXT = 36
LIST_FIELDS = 8
MAX_ID = 2**63 - 1
SOURCES_SHOWN = 5

State = Literal["current", "removed", "all"]


@dataclass(frozen=True)
class Cell:
    """A value ready for display. `href` is set only for http(s) links."""

    text: str
    href: str | None = None
    full: str | None = None  # the whole value when `text` is shortened
    empty: bool = False


EMPTY_CELL = Cell(filters.EMPTY, empty=True)


@dataclass(frozen=True)
class DatasetInfo:
    """A dataset type as the panel shows it: its newest definition and where it is defined."""

    name: str
    version: int
    storage: str
    pack: str
    description: str
    fields: Mapping[str, FieldDef]
    synced_version: int | None  # newest version in catalog.dataset_type
    defined_version: int | None  # newest version in the registry files

    @property
    def typed(self) -> bool:
        return self.storage == "typed"

    @property
    def key_fields(self) -> list[str]:
        return [name for name, f in self.fields.items() if f.key]

    @property
    def needs_sync(self) -> bool:
        return self.synced_version is None or (self.defined_version or 0) > self.synced_version


@dataclass
class DatasetUsage:
    current: int = 0
    removed: int | None = None  # typed tables keep no removals
    last_seen: datetime | None = None
    sources: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class RecordFilters:
    source_id: str | None
    state: State
    q: str | None


@dataclass(frozen=True)
class Listing:
    total: int
    rows: list[Any]  # result rows: id, source_id, external_id, last_seen_at, ...
    values: list[dict[str, Any]]  # field values per row, by field name
    source_options: list[str]


@dataclass(frozen=True)
class FieldChange:
    name: str
    old: Cell
    new: Cell


@dataclass(frozen=True)
class VersionView:
    version: int
    change_type: str
    schema_version: int
    valid_from: datetime
    valid_to: datetime | None
    run_id: Any
    raw_sha256: str | None
    changes: list[FieldChange] | None  # None when there is nothing to compare with
    compared_with: int | None


def format_value(value: Any, field_type: str, *, name: str = "", compact: bool = False) -> Cell:
    """Format one stored value by its dataset field type; `compact` for record lists."""
    text_limit = LIST_TEXT if compact else MAX_TEXT
    if value is None or value == "":
        return EMPTY_CELL
    if field_type == "money" and isinstance(value, Mapping):
        return Cell(filters.money(value))
    if field_type == "bool":
        return Cell("yes" if value else "no")
    if field_type in ("int", "float") and isinstance(value, int | float):
        return Cell(filters.number(value))
    if field_type == "decimal":
        return Cell(_decimal_text(value))
    if field_type == "datetime":
        when = _as_datetime(value)
        return Cell(filters.dt(when)) if when else _text_cell(str(value), text_limit)
    if field_type == "date":
        return Cell(value.isoformat() if isinstance(value, date) else str(value))
    if field_type == "time":
        return Cell(_time_text(value))
    if field_type == "url" or (field_type == "text" and _looks_like_url_field(name)):
        return url_cell(str(value), LIST_URL_TEXT if compact else MAX_URL_TEXT)
    if isinstance(value, Mapping | list):
        return _text_cell(json.dumps(value, ensure_ascii=False, sort_keys=True), text_limit)
    return _text_cell(str(value), text_limit)


def url_cell(value: str, limit: int = MAX_URL_TEXT) -> Cell:
    """A URL shortened for display; linked only when it is http(s)."""
    shown = short_url(re.sub(r"^https?://", "", value, flags=re.IGNORECASE), limit)
    href = value if value.lower().startswith(("http://", "https://")) else None
    return Cell(shown, href=href, full=value if shown != value else None)


def short_url(text: str, limit: int) -> str:
    """Keep the host and the last path segment, which say most: shop.example/…/mug.html."""
    if len(text) <= limit:
        return text
    host, _, path = text.partition("/")
    parts = path.rstrip("/").rsplit("/", 1)
    room = limit - len(host) - 3
    if len(parts) < 2 or room < 8:
        return text[: limit - 1] + "…"
    tail = parts[1] if len(parts[1]) <= room else parts[1][: room - 1] + "…"
    return f"{host}/…/{tail}"


def diff_payloads(
    old: Mapping[str, Any] | None, new: Mapping[str, Any] | None, fields: Mapping[str, FieldDef]
) -> list[FieldChange]:
    """Fields whose value differs between two payloads, in dataset field order."""
    old, new = old or {}, new or {}
    names = [name for name in fields if name in old or name in new]
    names += sorted((set(old) | set(new)) - set(fields))
    changes = []
    for name in names:
        before, after = old.get(name), new.get(name)
        if before != after:
            ftype = fields[name].type if name in fields else "text"
            changes.append(
                FieldChange(
                    name,
                    format_value(before, ftype, name=name),
                    format_value(after, ftype, name=name),
                )
            )
    return changes


def version_views(
    rows: Sequence[Mapping[str, Any]], fields: Mapping[str, FieldDef]
) -> list[VersionView]:
    """The timeline of one record from its versions in ascending order.

    A version is valid until the next one starts. Each observation is compared with the last
    observed payload before it, so a restore shows what changed since before the removal.
    The first row has nothing to compare with unless it is version 1 (an insert).
    """
    views = []
    last_payload: Mapping[str, Any] | None = None
    last_version: int | None = None
    for index, row in enumerate(rows):
        following = rows[index + 1] if index + 1 < len(rows) else None
        payload = row["payload"]
        changes: list[FieldChange] | None = None
        compared_with = None
        if payload is not None and last_payload is not None:
            changes = diff_payloads(last_payload, payload, fields)
            compared_with = last_version
        views.append(
            VersionView(
                version=row["version"],
                change_type=row["change_type"],
                schema_version=row["schema_version"],
                valid_from=row["valid_from"],
                valid_to=following["valid_from"] if following else None,
                run_id=row["run_id"],
                raw_sha256=row["raw_sha256"],
                changes=changes,
                compared_with=compared_with,
            )
        )
        if payload is not None:
            last_payload, last_version = payload, row["version"]
    return views


def like_pattern(q: str) -> str:
    """A case-insensitive substring pattern with LIKE wildcards taken literally."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def parse_record_id(value: str) -> int:
    """The numeric id from the URL, or 404: anything else cannot name a row."""
    if not value.isascii() or not value.isdigit() or not 0 < int(value) <= MAX_ID:
        raise HTTPException(404, f"No record {value!r}.")
    return int(value)


def _text_cell(text: str, limit: int = MAX_TEXT) -> Cell:
    if len(text) <= limit:
        return Cell(text)
    return Cell(text[: limit - 1] + "…", full=text)


def _decimal_text(value: Any) -> str:
    try:
        return str(Decimal(str(value)))
    except InvalidOperation:
        return str(value)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _time_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = time.fromisoformat(value)
        except ValueError:
            return str(value)
    if isinstance(value, time):
        return value.strftime("%H:%M:%S" if value.second else "%H:%M")
    return str(value)


def _looks_like_url_field(name: str) -> bool:
    # Typed models declare URLs as plain text; their names say what they hold.
    return name == "url" or name.endswith("_url")


def _registry(settings: Settings) -> tuple[DatasetRegistry | None, str | None]:
    try:
        return default_registry((settings.datasets_dir,)), None
    except DatasetError as exc:
        return None, str(exc)


def _parse_fields(raw: Any) -> dict[str, FieldDef] | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        return {str(name): FieldDef.model_validate(spec) for name, spec in raw.items()}
    except ValidationError:
        return None


def load_datasets(
    conn: Connection, settings: Settings, name: str | None = None
) -> tuple[dict[str, DatasetInfo], str | None]:
    """Every dataset type (or one), from the catalog and the registry files.

    The newer definition wins. For the same version the stored one wins, because records were
    written against it, unless the file defines exactly the same thing: then the file is used
    for its field order, which JSONB does not keep. Also returns a problem message when the
    registry files cannot be loaded.
    """
    dt = t.dataset_type
    query = (
        select(
            dt.c.name,
            dt.c.version,
            dt.c.pack,
            dt.c.storage,
            dt.c.description,
            dt.c.fields,
            dt.c.definition_hash,
        )
        .ext(distinct_on(dt.c.name))
        .order_by(dt.c.name, dt.c.version.desc())
        .limit(MAX_DATASETS)
    )
    if name is not None:
        query = query.where(dt.c.name == name)
    catalog = {row.name: row for row in conn.execute(query)}

    registry, problem = _registry(settings)
    defined: dict[str, DatasetType] = {}
    for candidate in registry or ():
        if name is None or candidate.name == name:
            defined[candidate.name] = candidate  # in version order, so the newest stays

    found: dict[str, DatasetInfo] = {}
    for key in sorted(set(catalog) | set(defined)):
        stored, ds = catalog.get(key), defined.get(key)
        stored_fields = _parse_fields(stored.fields) if stored is not None else None
        if ds is not None and (
            stored is None
            or stored_fields is None
            or ds.version > stored.version
            or (ds.version == stored.version and ds.definition_hash == stored.definition_hash)
        ):
            version, storage, pack, description = ds.version, ds.storage, ds.pack, ds.description
            fields: Mapping[str, FieldDef] = ds.fields
        elif stored is not None and stored_fields is not None:
            version, storage, pack = stored.version, stored.storage, stored.pack
            description, fields = stored.description, stored_fields
        else:
            continue  # an unreadable stored definition and no file to fall back on
        found[key] = DatasetInfo(
            name=key,
            version=version,
            storage=storage,
            pack=pack,
            description=description,
            fields=fields,
            synced_version=stored.version if stored is not None else None,
            defined_version=ds.version if ds is not None else None,
        )
    return found, problem


def _usage(conn: Connection) -> dict[str, DatasetUsage]:
    """Record counts, last sighting and sources per dataset, in one query plus the registry."""
    r = t.records
    generic = select(
        r.c.dataset.label("dataset"),
        r.c.source_id,
        func.count().filter(r.c.removed_at.is_(None)).label("current"),
        func.count().filter(r.c.removed_at.is_not(None)).label("removed"),
        func.max(r.c.last_seen_at).label("last_seen"),
    ).group_by(r.c.dataset, r.c.source_id)
    typed = [
        select(
            literal(name).label("dataset"),
            table.c.source_id,
            func.count().label("current"),
            cast(null(), BigInteger).label("removed"),
            func.max(table.c.last_seen_at).label("last_seen"),
        ).group_by(table.c.source_id)
        for name, table in TABLE_FOR.items()
    ]
    usage: dict[str, DatasetUsage] = {}
    for row in conn.execute(union_all(generic, *typed).limit(MAX_DATASETS * 10)):
        entry = usage.setdefault(row.dataset, DatasetUsage())
        entry.current += row.current
        if row.removed is not None:
            entry.removed = (entry.removed or 0) + row.removed
        if row.last_seen is not None and (
            entry.last_seen is None or row.last_seen > entry.last_seen
        ):
            entry.last_seen = row.last_seen
        entry.sources.add(row.source_id)
    declared = (
        select(t.sources.c.id, t.sources.c.dataset)
        .where(t.sources.c.dataset.is_not(None))
        .limit(MAX_DATASETS * 10)
    )
    for source_id, dataset in conn.execute(declared):
        usage.setdefault(dataset, DatasetUsage()).sources.add(source_id)
    return usage


@router.get("/data", response_class=HTMLResponse)
def datasets(request: Request, engine: EngineDep, settings: SettingsDep) -> HTMLResponse:
    with engine.connect() as conn:
        found, problem = load_datasets(conn, settings)
        usage = _usage(conn)
    rows = [(info, usage.get(name, DatasetUsage())) for name, info in found.items()]
    return render(
        request,
        "data/index.html",
        {"title": "Data", "rows": rows, "problem": problem, "sources_shown": SOURCES_SHOWN},
    )


def _dataset(conn: Connection, settings: Settings, name: str) -> DatasetInfo:
    if re.fullmatch(NAME_PATTERN, name) is None:
        raise HTTPException(404, f"No dataset named {name!r}.")
    info = load_datasets(conn, settings, name)[0].get(name)
    if info is None or (info.typed and name not in TABLE_FOR):
        raise HTTPException(404, f"No dataset named {name!r}.")
    return info


def _matches_payload(payload: ColumnElement[Any], pattern: str) -> ColumnElement[bool]:
    """True when a top-level payload value contains the pattern; keys are not searched."""
    values = func.jsonb_each_text(payload).table_valued("key", "value")
    return exists(
        select(literal(1)).select_from(values).where(values.c.value.ilike(pattern, escape="\\"))
    )


def _source_options(conn: Connection, table: Table, *where: ColumnElement[bool]) -> list[str]:
    query = (
        select(table.c.source_id)
        .where(*where)
        .distinct()
        .order_by(table.c.source_id)
        .limit(MAX_SOURCE_OPTIONS)
    )
    return list(conn.execute(query).scalars())


def _list_generic(conn: Connection, info: DatasetInfo, rf: RecordFilters, page: Page) -> Listing:
    r = t.records
    in_dataset = r.c.dataset == info.name
    conditions = [in_dataset]
    if rf.source_id:
        conditions.append(r.c.source_id == rf.source_id)
    if rf.state == "current":
        conditions.append(r.c.removed_at.is_(None))
    elif rf.state == "removed":
        conditions.append(r.c.removed_at.is_not(None))
    if rf.q:
        pattern = like_pattern(rf.q)
        conditions.append(
            or_(r.c.external_id.ilike(pattern, escape="\\"), _matches_payload(r.c.payload, pattern))
        )
    total = conn.execute(select(func.count()).select_from(r).where(*conditions)).scalar_one()
    rows = conn.execute(
        select(
            r.c.id, r.c.source_id, r.c.external_id, r.c.last_seen_at, r.c.removed_at, r.c.payload
        )
        .where(*conditions)
        .order_by(r.c.last_seen_at.desc(), r.c.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    ).all()
    values = [dict(row.payload or {}) for row in rows]
    return Listing(total, list(rows), values, _source_options(conn, r, in_dataset))


def _list_typed(conn: Connection, info: DatasetInfo, rf: RecordFilters, page: Page) -> Listing:
    table = TABLE_FOR[info.name]
    columns = [name for name in info.fields if name in table.c]
    conditions = []
    if rf.source_id:
        conditions.append(table.c.source_id == rf.source_id)
    if rf.q:
        pattern = like_pattern(rf.q)
        searched = [table.c.external_id] + [
            table.c[name] for name in columns if info.fields[name].type in ("text", "enum")
        ]
        conditions.append(or_(*(column.ilike(pattern, escape="\\") for column in searched)))
    total = conn.execute(select(func.count()).select_from(table).where(*conditions)).scalar_one()
    rows = conn.execute(
        select(
            table.c.id,
            table.c.source_id,
            table.c.external_id,
            table.c.last_seen_at,
            *(table.c[name] for name in columns),
        )
        .where(*conditions)
        .order_by(table.c.last_seen_at.desc(), table.c.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    ).all()
    values = [{name: row._mapping[name] for name in columns} for row in rows]
    return Listing(total, list(rows), values, _source_options(conn, table))


def list_columns(
    info: DatasetInfo, columns: Sequence[str], values: Sequence[Mapping[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    """Which fields a record list shows: (shown, left for the record page, empty here).

    Key fields always show; other fields show when they have a value on this page, in
    definition order, up to LIST_FIELDS in all.
    """
    filled = [
        name
        for name in columns
        if info.fields[name].key or any(v.get(name) not in (None, "") for v in values)
    ]
    keys = [name for name in filled if info.fields[name].key]
    others = [name for name in filled if not info.fields[name].key]
    chosen = set(keys + others[: max(LIST_FIELDS - len(keys), 0)])
    shown = [name for name in filled if name in chosen]
    more = [name for name in filled if name not in chosen]
    return shown, more, [name for name in columns if name not in filled]


@router.get("/data/{dataset}", response_class=HTMLResponse)
def records(
    request: Request,
    dataset: str,
    engine: EngineDep,
    settings: SettingsDep,
    page: PageDep,
    source_id: Annotated[str | None, Query(max_length=100)] = None,
    state: State = "current",
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> HTMLResponse:
    rf = RecordFilters(source_id or None, state, (q or "").strip() or None)
    with engine.connect() as conn:
        info = _dataset(conn, settings, dataset)
        listing = (_list_typed if info.typed else _list_generic)(conn, info, rf, page)
    columns = [name for name in info.fields if not info.typed or name in TABLE_FOR[info.name].c]
    visible, more, empty = list_columns(info, columns, listing.values)
    rows = [
        {
            "id": row.id,
            "external_id": row.external_id,
            "source_id": row.source_id,
            "last_seen_at": row.last_seen_at,
            "removed_at": None if info.typed else row.removed_at,
            "cells": [
                format_value(v.get(n), info.fields[n].type, name=n, compact=True) for n in visible
            ],
        }
        for row, v in zip(listing.rows, listing.values, strict=True)
    ]
    return render(
        request,
        "data/records.html",
        {
            "title": f"{info.name} · Data",
            "info": info,
            "fields": [(name, info.fields[name]) for name in visible],
            "more": more,
            "empty_fields": empty,
            "rows": rows,
            "total": listing.total,
            "page": page,
            "filters": rf,
            "source_options": listing.source_options,
        },
    )


def _provenance_query(table: Table) -> Any:
    sv, s = t.scraper_versions, t.sources
    return select(
        table,
        s.c.name.label("source_name"),
        sv.c.adapter.label("scraper_adapter"),
        sv.c.code_hash.label("scraper_code_hash"),
        sv.c.author.label("scraper_author"),
    ).select_from(
        table.outerjoin(s, s.c.id == table.c.source_id).outerjoin(
            sv, sv.c.id == table.c.scraper_version_id
        )
    )


@router.get("/data/{dataset}/{record_id}", response_class=HTMLResponse)
def record(
    request: Request, dataset: str, record_id: str, engine: EngineDep, settings: SettingsDep
) -> HTMLResponse:
    rid = parse_record_id(record_id)
    versions: list[VersionView] = []
    with engine.connect() as conn:
        info = _dataset(conn, settings, dataset)
        if info.typed:
            table = TABLE_FOR[info.name]
            row = conn.execute(_provenance_query(table).where(table.c.id == rid)).first()
            if row is None:
                raise HTTPException(404, f"No {info.name} record {rid}.")
            values = {name: row._mapping[name] for name in info.fields if name in table.c}
        else:
            r, v = t.records, t.record_versions
            row = conn.execute(
                _provenance_query(r).where(r.c.id == rid, r.c.dataset == info.name)
            ).first()
            if row is None:
                raise HTTPException(404, f"No {info.name} record {rid}.")
            values = dict(row.payload or {})
            history = conn.execute(
                select(
                    v.c.version,
                    v.c.change_type,
                    v.c.schema_version,
                    v.c.payload,
                    v.c.valid_from,
                    v.c.run_id,
                    v.c.raw_sha256,
                )
                .where(v.c.record_id == rid)
                .order_by(v.c.version.desc())
                .limit(MAX_VERSIONS)
            ).all()
            versions = version_views([h._mapping for h in reversed(history)], info.fields)
    names = [name for name in info.fields if name in values] + sorted(
        set(values) - set(info.fields)
    )
    current = [
        (
            name,
            info.fields.get(name),
            format_value(
                values.get(name),
                info.fields[name].type if name in info.fields else "text",
                name=name,
            ),
        )
        for name in names
    ]
    total_versions = 0 if info.typed else row.current_version
    return render(
        request,
        "data/record.html",
        {
            "title": f"{row.external_id} · {info.name}",
            "info": info,
            "row": row,
            "canonical": url_cell(row.canonical_url) if row.canonical_url else None,
            "current": current,
            "versions": list(reversed(versions)),
            "total_versions": total_versions,
            "removed_at": None if info.typed else row.removed_at,
        },
    )
