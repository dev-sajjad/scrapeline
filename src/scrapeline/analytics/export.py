"""Parquet lake exporter: copies operational tables from PostgreSQL into `analytics.layout`.

Rows are read through SQLAlchemy in chunks, staged in an in-process DuckDB database with the
declared column types, and written to Parquet by DuckDB. Every file is written under a
temporary name and renamed into place, and the export state is saved last, so a crashed
export leaves at most complete files behind and is simply retried (readers dedupe on the
primary key).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import duckdb
import pandas as pd
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, Select, select, text
from sqlalchemy.sql.elements import ColumnElement

from scrapeline.analytics.layout import (
    LAKE_LAYOUT_VERSION,
    OVERLAP,
    STATE_PATH,
    TABLES,
    ExportReport,
    LakeTable,
)
from scrapeline.catalog.datasets import DatasetRegistry
from scrapeline.errors import ConfigError, DatasetError
from scrapeline.ops.logging import get_logger
from scrapeline.pipeline.records import keeps_personal_data
from scrapeline.registry.config import SourceConfig, UsageRights
from scrapeline.store import tables as t

log = get_logger(__name__)

PARTIAL_SUFFIX = ".tmp"
WORK_PREFIX = "work-"
DEFAULT_CADENCE = str(SourceConfig.model_fields["cadence"].default)
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")

# The earliest start of any other open transaction in this database bounds what is surely
# committed. A row stamped exactly at that start may belong to the open transaction, hence
# the microsecond. Sessions of other roles hide xact_start unless the export role has
# pg_read_all_stats, so the exporter must run as the writers' role or hold that grant.
_CUTOFF_SQL = text(
    """
    SELECT least(now(), (
        SELECT min(xact_start) - interval '1 microsecond'
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND backend_type = 'client backend'
          AND xact_start IS NOT NULL
          AND pid <> pg_backend_pid()
    ))
    """
)


@dataclass(frozen=True)
class TableState:
    """Export progress of one table, as kept in `STATE_PATH`.

    `recent` maps the primary keys already exported within OVERLAP of the watermark to their
    watermark values ("overlap" tables only), so re-reading the overlap writes no duplicates.
    """

    watermark: datetime | None = None
    recent: Mapping[str, datetime] = field(default_factory=dict)
    export_id: str | None = None
    exported_at: datetime | None = None
    rows: int = 0


def load_state(lake_dir: Path) -> dict[str, TableState]:
    """Export state per table; empty for a new lake."""
    path = lake_dir / STATE_PATH
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"unreadable lake state {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("layout_version") != LAKE_LAYOUT_VERSION:
        raise ConfigError(
            f"lake state {path} is not for layout version {LAKE_LAYOUT_VERSION}; "
            "export into a new lake directory"
        )
    try:
        return {name: _state_from_json(entry) for name, entry in data["tables"].items()}
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"malformed lake state {path}: {exc}") from exc


def save_state(lake_dir: Path, state: Mapping[str, TableState]) -> None:
    """Replace the state file atomically."""
    path = lake_dir / STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "layout_version": LAKE_LAYOUT_VERSION,
        "tables": {name: _state_to_json(entry) for name, entry in sorted(state.items())},
    }
    partial = path.with_name(path.name + PARTIAL_SUFFIX)
    partial.write_text(json.dumps(document, indent=2) + "\n")
    _fsync(partial)
    os.replace(partial, path)
    _fsync(path.parent)


def _state_to_json(entry: TableState) -> dict[str, Any]:
    return {
        "watermark": _iso(entry.watermark),
        "recent": {key: when.isoformat() for key, when in sorted(entry.recent.items())},
        "export_id": entry.export_id,
        "exported_at": _iso(entry.exported_at),
        "rows": entry.rows,
    }


def _state_from_json(data: Mapping[str, Any]) -> TableState:
    return TableState(
        watermark=_parse_iso(data.get("watermark")),
        recent={key: datetime.fromisoformat(when) for key, when in data["recent"].items()},
        export_id=data.get("export_id"),
        exported_at=_parse_iso(data.get("exported_at")),
        rows=int(data["rows"]),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def next_recent(
    previous: Mapping[str, datetime], fresh: Mapping[str, datetime], watermark: datetime
) -> dict[str, datetime]:
    """Keys to remember for the next overlap window: exported, and within OVERLAP of it."""
    horizon = watermark - OVERLAP
    return {key: when for key, when in {**previous, **fresh}.items() if when > horizon}


def new_export_id(at: datetime) -> str:
    """Unique, time-sortable id used in file names."""
    return f"{at.astimezone(UTC):%Y%m%dT%H%M%S%fZ}-{secrets.token_hex(3)}"


def partition_date(value: datetime) -> date:
    """The `date` partition: the UTC date of the partition_time column."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).date()


def render_path(table: LakeTable, export_id: str, row: Mapping[str, Any]) -> str:
    """Path of the file that holds `row`, relative to the lake root."""
    if table.mode == "snapshot":
        return table.path
    values = {"export_id": export_id}
    for part in table.partition_by:
        if part == "date":
            assert table.partition_time is not None
            when = row[table.partition_time]
            if when is None:
                raise ConfigError(f"{table.name}: {table.partition_time} is empty")
            values[part] = partition_date(when).isoformat()
            continue
        value = str(row[part])
        if not _SAFE_SEGMENT.fullmatch(value):
            raise ConfigError(f"{table.name}: {part}={value!r} is not a safe partition value")
        values[part] = value
    return table.path.format(**values)


def lake_status(lake_dir: Path) -> dict[str, Any]:
    """Files and rows per table on disk, with each table's export state."""
    state = load_state(lake_dir)
    tables: dict[str, Any] = {}
    with duckdb.connect() as duck:
        for name, table in TABLES.items():
            files = sorted(str(p) for p in lake_dir.glob(table.glob) if p.is_file())
            rows = 0
            if files:
                found = duck.execute(
                    "SELECT coalesce(sum(num_rows), 0) FROM parquet_file_metadata(?)", [files]
                ).fetchone()
                rows = int(found[0]) if found else 0
            entry: dict[str, Any] = {"mode": table.mode, "files": len(files), "rows": rows}
            if name in state:
                done = state[name]
                entry["last_export"] = {
                    "export_id": done.export_id,
                    "at": _iso(done.exported_at),
                    "rows": done.rows,
                }
                if table.mode == "append":
                    entry["watermark"] = _iso(done.watermark)
                if table.late_commits == "overlap":
                    entry["recent_keys"] = len(done.recent)
            tables[name] = entry
    return {"lake_dir": str(lake_dir), "layout_version": LAKE_LAYOUT_VERSION, "tables": tables}


@dataclass(frozen=True)
class _PersonalData:
    """Which sources keep personal data, and which payload fields are personal data."""

    keeps: Mapping[str, bool]
    fields: Mapping[tuple[str, int], frozenset[str]]

    @classmethod
    def load(cls, conn: Connection, registry: DatasetRegistry) -> _PersonalData:
        keeps = {
            source_id: keeps_personal_data(_usage(config))
            for source_id, config in conn.execute(select(t.sources.c.id, t.sources.c.config))
        }
        # The catalog covers dataset versions that have left the registry files.
        fields: dict[tuple[str, int], frozenset[str]] = {
            (name, version): frozenset(
                key
                for key, spec in (defs or {}).items()
                if isinstance(spec, dict) and spec.get("pii")
            )
            for name, version, defs in conn.execute(
                select(t.dataset_type.c.name, t.dataset_type.c.version, t.dataset_type.c.fields)
            )
        }
        fields.update({(ds.name, ds.version): frozenset(ds.pii_fields) for ds in registry})
        return cls(keeps, fields)

    def strip(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = row["payload"]
        if payload is None or self.keeps.get(row["source_id"], False):
            return row
        key = (row["dataset"], row["dataset_version"])
        if key not in self.fields:
            raise DatasetError(
                f"unknown dataset {key[0]} v{key[1]}: cannot tell which fields are personal data"
            )
        drop = self.fields[key]
        if drop:
            row["payload"] = {name: value for name, value in payload.items() if name not in drop}
        return row


def _usage(config: Any) -> UsageRights:
    try:
        return UsageRights.model_validate((config or {}).get("usage") or {})
    except (AttributeError, ValidationError):
        return UsageRights()  # unreadable usage grants nothing


@dataclass(frozen=True)
class _Query:
    select: Select[Any]
    watermark: ColumnElement[Any] | None = None
    prepare: Callable[[dict[str, Any]], dict[str, Any]] | None = None


class ParquetLakeExporter:
    """Implements `layout.LakeExporter`: PostgreSQL tables to Parquet files under `lake_dir`.

    One export at a time per lake; a file lock serialises them.
    """

    def __init__(
        self,
        engine: Engine,
        lake_dir: Path,
        registry: DatasetRegistry,
        *,
        chunk_size: int = 10_000,
    ) -> None:
        self.engine = engine
        self.lake_dir = lake_dir
        self.registry = registry
        self.chunk_size = chunk_size

    def export(self, *, tables: Collection[str] | None = None) -> ExportReport:
        """Export the named tables (every table by default); all files land, then the state."""
        names = _selected(tables)
        started_at = datetime.now(UTC)
        report = ExportReport(new_export_id(started_at), started_at)
        state_dir = (self.lake_dir / STATE_PATH).parent
        state_dir.mkdir(parents=True, exist_ok=True)
        pending: list[tuple[Path, Path]] = []
        with _exclusive(state_dir / "exports.lock"):
            _remove_leftovers(self.lake_dir)
            state = load_state(self.lake_dir)
            with (
                tempfile.TemporaryDirectory(dir=state_dir, prefix=WORK_PREFIX) as work,
                duckdb.connect(config={"temp_directory": work}) as duck,
            ):
                duck.execute("SET TimeZone = 'UTC'")
                needs_cutoff = any(TABLES[name].late_commits == "cutoff" for name in names)
                cutoff = self._cutoff() if needs_cutoff else None
                try:
                    for name in names:
                        previous = state.get(name, TableState())
                        state[name] = self._export_table(
                            duck, TABLES[name], report, previous, cutoff, pending
                        )
                    _publish(pending)
                    save_state(self.lake_dir, state)
                except BaseException:
                    for partial, _ in pending:
                        partial.unlink(missing_ok=True)
                    raise
        report.files = [final.relative_to(self.lake_dir).as_posix() for _, final in pending]
        log.info("lake.export", export_id=report.export_id, rows=report.rows, files=len(pending))
        return report

    def _cutoff(self) -> datetime:
        with self.engine.connect() as conn:
            cutoff: datetime = conn.execute(_CUTOFF_SQL).scalar_one()
        return cutoff

    def _export_table(
        self,
        duck: duckdb.DuckDBPyConnection,
        table: LakeTable,
        report: ExportReport,
        previous: TableState,
        cutoff: datetime | None,
        pending: list[tuple[Path, Path]],
    ) -> TableState:
        stage = _Stage(duck, table)
        key = table.columns[0]
        skip = previous.recent if table.late_commits == "overlap" else {}
        for chunk in self._chunks(table, previous, cutoff):
            stage.add(
                [
                    [_text(row[c.name], c.type) for c in table.columns]
                    + [render_path(table, report.export_id, row)]
                    for row in chunk
                    if _text(row[key.name], key.type) not in skip
                ]
            )

        for relative in [table.path] if table.mode == "snapshot" else stage.paths():
            final = self.lake_dir / relative
            final.parent.mkdir(parents=True, exist_ok=True)
            partial = final.with_name(final.name + PARTIAL_SUFFIX)
            pending.append((partial, final))
            stage.write(relative, partial)
            _fsync(partial)

        rows = stage.count()
        report.rows[table.name] = rows
        watermark, recent = previous.watermark, dict(previous.recent)
        if table.late_commits == "cutoff":
            assert cutoff is not None
            watermark = cutoff if watermark is None else max(watermark, cutoff)
        elif table.late_commits == "overlap":
            assert table.watermark is not None
            latest = stage.latest(table.watermark)
            if latest is not None:
                watermark = latest if watermark is None else max(watermark, latest)
            if watermark is not None:
                fresh = stage.keys_after(table.watermark, watermark - OVERLAP)
                recent = next_recent(previous.recent, fresh, watermark)
        stage.drop()
        return TableState(
            watermark=watermark.astimezone(UTC) if watermark is not None else None,
            recent=recent,
            export_id=report.export_id,
            exported_at=report.started_at,
            rows=rows,
        )

    def _chunks(
        self, table: LakeTable, previous: TableState, cutoff: datetime | None
    ) -> Iterator[Sequence[dict[str, Any]]]:
        if table.name == "dataset_types":
            yield self._dataset_types()
            return
        with self.engine.connect() as conn:
            query = self._query(table.name, conn)
            stmt = query.select
            if query.watermark is not None:
                stmt = stmt.where(*_window(query.watermark, table, previous, cutoff))
            result = conn.execute(stmt, execution_options={"yield_per": self.chunk_size})
            for partition in result.partitions():
                rows = [dict(row._mapping) for row in partition]
                yield [query.prepare(row) for row in rows] if query.prepare else rows

    def _query(self, name: str, conn: Connection) -> _Query:
        match name:
            case "record_versions":
                v, r = t.record_versions, t.records
                return _Query(
                    select(
                        v.c.id.label("version_id"),
                        v.c.record_id,
                        r.c.source_id,
                        r.c.dataset,
                        v.c.schema_version.label("dataset_version"),
                        r.c.external_id,
                        v.c.version,
                        v.c.change_type,
                        v.c.valid_from,
                        v.c.content_hash,
                        v.c.payload,
                        v.c.run_id,
                        v.c.raw_sha256,
                        v.c.scraper_version_id,
                        v.c.created_at,
                    ).select_from(v.join(r, r.c.id == v.c.record_id)),
                    v.c.created_at,
                    _PersonalData.load(conn, self.registry).strip,
                )
            case "runs":
                run = t.run
                return _Query(
                    select(
                        run.c.id.label("run_id"),
                        run.c.source_id,
                        run.c.kind,
                        run.c.status,
                        run.c.started_at,
                        run.c.finished_at,
                        run.c.scraper_version_id,
                        run.c.stats,
                        run.c.error,
                    ),
                    run.c.finished_at,
                )
            case "check_results":
                check = t.check_result
                return _Query(
                    select(
                        check.c.id,
                        check.c.run_id,
                        check.c.source_id,
                        check.c.check_name,
                        check.c.status,
                        check.c.value,
                        check.c.threshold,
                        check.c.created_at,
                    ),
                    check.c.created_at,
                )
            case "fetch_log":
                fetch = t.fetch_log
                return _Query(
                    select(
                        fetch.c.id,
                        fetch.c.run_id,
                        fetch.c.source_id,
                        fetch.c.url.label("_url"),
                        fetch.c.status,
                        fetch.c.attempt,
                        fetch.c.elapsed_ms,
                        fetch.c.nbytes,
                        fetch.c.kind,
                        fetch.c.fetched_at,
                        fetch.c.error,
                    ),
                    fetch.c.fetched_at,
                    _host_only,
                )
            case "agent_runs":
                agent = t.agent_run
                return _Query(
                    select(
                        agent.c.id,
                        agent.c.role,
                        agent.c.source_id,
                        agent.c.model,
                        agent.c.prompt_version,
                        agent.c.attempt,
                        agent.c.outcome,
                        agent.c.input_tokens,
                        agent.c.output_tokens,
                        agent.c.cost_usd,
                        agent.c.started_at,
                        agent.c.finished_at,
                    ),
                    agent.c.finished_at,
                )
            case "classification_log":
                logged = t.classification_log
                return _Query(
                    select(
                        logged.c.id,
                        logged.c.site_id,
                        logged.c.old_category,
                        logged.c.new_category,
                        logged.c.confidence,
                        logged.c.method,
                        logged.c.taxonomy_version,
                        logged.c.actor,
                        logged.c.created_at,
                    ),
                    logged.c.created_at,
                )
            case "incidents":
                incident = t.incident
                return _Query(
                    select(
                        incident.c.id,
                        incident.c.source_id,
                        incident.c.kind,
                        incident.c.status,
                        incident.c.summary,
                        incident.c.opened_at,
                        incident.c.closed_at,
                    )
                )
            case "sources":
                source = t.sources
                return _Query(
                    select(
                        source.c.id,
                        source.c.name,
                        source.c.platform,
                        source.c.dataset,
                        source.c.site_id,
                        source.c.status,
                        source.c.config.label("_config"),
                        source.c.created_at,
                    ),
                    prepare=_cadence_from_config,
                )
            case "sites":
                site = t.site
                return _Query(
                    select(
                        site.c.id,
                        site.c.host,
                        site.c.registered_domain,
                        site.c.category,
                        site.c.category_confidence,
                        site.c.category_method,
                        site.c.tags,
                        site.c.country,
                        site.c.language,
                        site.c.platform,
                        site.c.classified_at,
                    )
                )
            case "site_categories":
                category = t.site_category
                return _Query(
                    select(
                        category.c.code,
                        category.c.label,
                        category.c.description,
                        category.c.taxonomy_version,
                    )
                )
            case _:
                raise ConfigError(f"no export query for lake table {name!r}")

    def _dataset_types(self) -> list[dict[str, Any]]:
        """Every dataset type in the registry, plus older versions only the catalog knows."""
        rows = {
            (ds.name, ds.version): {
                "name": ds.name,
                "version": ds.version,
                "pack": ds.pack,
                "storage": ds.storage,
                "description": ds.description,
                "fields": {name: f.model_dump(mode="json") for name, f in ds.fields.items()},
                "key_fields": list(ds.key_fields),
            }
            for ds in self.registry
        }
        catalog = t.dataset_type
        with self.engine.connect() as conn:
            stored = conn.execute(
                select(
                    catalog.c.name,
                    catalog.c.version,
                    catalog.c.pack,
                    catalog.c.storage,
                    catalog.c.description,
                    catalog.c.fields,
                    catalog.c.key_fields,
                )
            )
            for row in stored:
                rows.setdefault((row.name, row.version), dict(row._mapping))
        return list(rows.values())


def _selected(tables: Collection[str] | None) -> list[str]:
    if tables is None:
        return list(TABLES)
    unknown = sorted(set(tables) - set(TABLES))
    if unknown:
        raise ConfigError(f"unknown lake table(s) {unknown}; known: {sorted(TABLES)}")
    return [name for name in TABLES if name in tables]


def _window(
    column: ColumnElement[Any], table: LakeTable, previous: TableState, cutoff: datetime | None
) -> list[ColumnElement[bool]]:
    """Rows to read for one append table: see the late_commits rules in `analytics.layout`."""
    if table.late_commits == "cutoff":
        assert cutoff is not None
        window = [column <= cutoff]
        if previous.watermark is not None:
            window.append(column > previous.watermark)
        return window
    if previous.watermark is None:
        return [column.is_not(None)]
    return [column > previous.watermark - OVERLAP]


def _host_only(row: dict[str, Any]) -> dict[str, Any]:
    url = row.pop("_url")
    try:
        row["host"] = urlsplit(url).hostname
    except ValueError:
        row["host"] = None  # a malformed logged URL still counts as a fetch
    return row


def _cadence_from_config(row: dict[str, Any]) -> dict[str, Any]:
    config = row.pop("_config") or {}
    row["cadence"] = config.get("cadence") or DEFAULT_CADENCE
    return row


def _text(value: Any, duck_type: str) -> str | None:
    """A value as text that DuckDB casts back to `duck_type` without loss."""
    if value is None:
        return None
    if duck_type == "JSON":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if duck_type == "TIMESTAMPTZ":
        aware: datetime = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.isoformat()
    if duck_type == "DOUBLE":
        return repr(float(value))
    return str(value)


class _Stage:
    """One table's rows for this export in a DuckDB temp table, typed as the layout declares.

    Each row carries `_path`, the file it belongs to. SQL here is built only from layout
    names (quoted) and escaped literals, never from data.
    """

    def __init__(self, duck: duckdb.DuckDBPyConnection, table: LakeTable) -> None:
        self.duck = duck
        self.table = table
        self.name = _ident(f"stage_{table.name}")
        self.key = _ident(table.columns[0].name)
        columns = ", ".join(f"{_ident(c.name)} {c.type}" for c in table.columns)
        duck.execute(f"CREATE TEMP TABLE {self.name} ({columns}, _path VARCHAR)")

    def add(self, rows: list[list[str | None]]) -> None:
        if not rows:
            return
        # Text columns keep pandas from guessing types; DuckDB casts each to its declared type.
        names = [c.name for c in self.table.columns] + ["_path"]
        frame = pd.DataFrame(rows, columns=names, dtype="string")
        casts = ", ".join(f"CAST({_ident(c.name)} AS {c.type})" for c in self.table.columns)
        self.duck.register("chunk", frame)
        try:
            self.duck.execute(f"INSERT INTO {self.name} SELECT {casts}, _path FROM chunk")  # noqa: S608
        finally:
            self.duck.unregister("chunk")

    def paths(self) -> list[str]:
        query = f"SELECT DISTINCT _path FROM {self.name} ORDER BY 1"  # noqa: S608
        return [str(path) for (path,) in self.duck.execute(query).fetchall()]

    def write(self, relative: str, target: Path) -> None:
        """Write the rows of one file, ordered by primary key, as Parquet to `target`."""
        columns = ", ".join(_ident(c.name) for c in self.table.columns)
        rows = f"SELECT {columns} FROM {self.name} WHERE _path = {_literal(relative)}"  # noqa: S608
        self.duck.execute(
            f"COPY ({rows} ORDER BY {self.key}) TO {_literal(str(target))} (FORMAT parquet)"
        )

    def count(self) -> int:
        return int(self._scalar(f"SELECT count(*) FROM {self.name}"))  # noqa: S608

    def latest(self, column: str) -> datetime | None:
        found: datetime | None = self._scalar(f"SELECT max({_ident(column)}) FROM {self.name}")  # noqa: S608
        return found

    def keys_after(self, column: str, after: datetime) -> dict[str, datetime]:
        """Primary keys (as text) of rows whose `column` is later than `after`."""
        query = (
            f"SELECT CAST({self.key} AS VARCHAR), {_ident(column)} FROM {self.name} "  # noqa: S608
            f"WHERE {_ident(column)} > ?"
        )
        return dict(self.duck.execute(query, [after]).fetchall())

    def drop(self) -> None:
        self.duck.execute(f"DROP TABLE {self.name}")

    def _scalar(self, query: str) -> Any:
        found = self.duck.execute(query).fetchone()
        return found[0] if found else None


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _publish(pending: Sequence[tuple[Path, Path]]) -> None:
    for partial, final in pending:
        os.replace(partial, final)
    for directory in sorted({final.parent for _, final in pending}):
        _fsync(directory)


def _remove_leftovers(lake_dir: Path) -> None:
    """Drop what a crashed export left behind; partial files never match a reader's glob."""
    for partial in lake_dir.glob(f"**/*.parquet{PARTIAL_SUFFIX}"):
        partial.unlink(missing_ok=True)
    for work in (lake_dir / STATE_PATH).parent.glob(f"{WORK_PREFIX}*"):
        shutil.rmtree(work, ignore_errors=True)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _exclusive(lock_path: Path) -> Iterator[None]:
    with lock_path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
