"""How data is stored: the raw store, the database, the Parquet lake and the dbt warehouse.

Raw responses are hostile content. Their text is shown escaped inside <pre>, and downloads are
served as attachments, so scraped HTML never renders on the panel's origin.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import (
    Connection,
    Integer,
    cast,
    column,
    func,
    literal,
    null,
    select,
    table,
    union_all,
)

from scrapeline.analytics.export import lake_status
from scrapeline.errors import ConfigError
from scrapeline.pipeline.load import TABLE_FOR
from scrapeline.serve.panel.deps import EngineDep, SettingsDep, render
from scrapeline.settings import Settings
from scrapeline.store import tables as t
from scrapeline.store.raw import FilesystemRawStore

router = APIRouter()

KEY_LAYOUT = "<source>/<yyyy>/<mm>/<dd>/<sha256>.<ext>"
SCHEMAS = ("public", "raw", "ops", "pipeline", "catalog", "data", "evals")
SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_EXT = re.compile(r"[a-z0-9]{1,10}")
PREVIEW_BYTES = 64 * 1024
MAX_ROWS = 500
RECENT_FILES = 20
FETCHES_SHOWN = 50
CITING_SHOWN = 10

TEXT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/javascript",
        "application/rss+xml",
        "application/atom+xml",
    }
)
TEXT_EXTENSIONS = frozenset({"html", "json", "txt", "xml", "csv"})

_pg_namespace = table("pg_namespace", column("oid"), column("nspname"), schema="pg_catalog")
_pg_class = table(
    "pg_class",
    column("oid"),
    column("relname"),
    column("relnamespace"),
    column("relkind"),
    column("reltuples"),
    schema="pg_catalog",
)


@dataclass(frozen=True)
class Preview:
    """What the raw page shows of the bytes: decoded text, or why there is none."""

    text: str | None = None
    size: int | None = None
    truncated: bool = False
    problem: str | None = None


def is_text(content_type: str | None, key: str) -> bool:
    """Whether a raw object is text, by its content type or else by its key's extension."""
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        return base.startswith("text/") or base in TEXT_TYPES or base.endswith(("+json", "+xml"))
    return Path(key).suffix.lstrip(".").lower() in TEXT_EXTENSIONS


def decode_preview(data: bytes) -> tuple[str, bool]:
    """The first PREVIEW_BYTES as UTF-8 text, undecodable bytes replaced; and if it was cut."""
    return data[:PREVIEW_BYTES].decode("utf-8", errors="replace"), len(data) > PREVIEW_BYTES


def resolve_raw(root: Path, key: str, sha256: str) -> Path | None:
    """The file of a stored raw key; None when the key names another object or leaves the root."""
    root = root.resolve()
    if Path(key).stem != sha256:
        return None
    path = (root / key).resolve()
    return path if path.is_relative_to(root) else None


def _check_sha(sha256: str) -> None:
    if SHA256.fullmatch(sha256) is None:
        raise HTTPException(404, "Not a raw object id: expected 64 lowercase hex characters.")


def _raw_store_by_source(conn: Connection) -> list[Any]:
    """Raw files and their bytes per source, with the last fetch of any kind.

    A file is a distinct raw key, not a distinct hash: the key holds the fetch date, so the
    same bytes fetched on another day are a second file on disk.
    """
    f = t.fetch_log
    objects = (
        select(
            f.c.source_id,
            f.c.raw_key,
            func.max(f.c.nbytes).label("nbytes"),
        )
        .where(f.c.raw_key.is_not(None))
        .group_by(f.c.source_id, f.c.raw_key)
        .subquery()
    )
    stored = (
        select(
            objects.c.source_id,
            func.count().label("files"),
            func.coalesce(func.sum(objects.c.nbytes), 0).label("bytes"),
        )
        .group_by(objects.c.source_id)
        .subquery()
    )
    fetches = (
        select(
            f.c.source_id,
            func.count().label("fetches"),
            func.max(f.c.fetched_at).label("last_fetch"),
        )
        .group_by(f.c.source_id)
        .subquery()
    )
    query = (
        select(
            fetches.c.source_id,
            fetches.c.fetches,
            fetches.c.last_fetch,
            func.coalesce(stored.c.files, 0).label("files"),
            func.coalesce(stored.c.bytes, 0).label("bytes"),
        )
        .select_from(fetches.outerjoin(stored, stored.c.source_id == fetches.c.source_id))
        .order_by(fetches.c.source_id)
        .limit(MAX_ROWS)
    )
    return list(conn.execute(query))


def _recent_raw(conn: Connection) -> list[Any]:
    f = t.fetch_log
    query = (
        select(
            f.c.raw_sha256,
            f.c.source_id,
            f.c.url,
            f.c.content_type,
            f.c.nbytes,
            f.c.fetched_at,
        )
        .where(f.c.raw_sha256.is_not(None))
        .order_by(f.c.fetched_at.desc(), f.c.id.desc())
        .limit(RECENT_FILES)
    )
    return list(conn.execute(query))


def _database_tables(conn: Connection) -> list[Any]:
    """Estimated rows (planner statistics) and total size of every table in the panel's schemas."""
    c, n = _pg_class, _pg_namespace
    query = (
        select(
            n.c.nspname.label("schema"),
            c.c.relname.label("name"),
            c.c.reltuples.label("estimated_rows"),
            func.pg_total_relation_size(c.c.oid).label("bytes"),
        )
        .select_from(c.join(n, n.c.oid == c.c.relnamespace))
        .where(c.c.relkind.in_(["r", "p"]), n.c.nspname.in_(SCHEMAS))
        .order_by(n.c.nspname, c.c.relname)
        .limit(MAX_ROWS)
    )
    return list(conn.execute(query))


def _lake(settings: Settings) -> tuple[list[dict[str, Any]], str | None]:
    """Per lake table: mode, files, rows, last export and watermark, with times parsed."""
    try:
        status = lake_status(settings.lake_dir)
    except (ConfigError, duckdb.Error, OSError) as exc:
        return [], str(exc)
    rows = []
    for name, entry in sorted(status["tables"].items()):
        last = entry.get("last_export") or {}
        rows.append(
            {
                "name": name,
                "mode": entry["mode"],
                "files": entry["files"],
                "rows": entry["rows"],
                "export_id": last.get("export_id"),
                "exported_at": _parse_time(last.get("at")),
                "exported_rows": last.get("rows"),
                "watermark": _parse_time(entry.get("watermark")),
            }
        )
    return rows, None


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _file_info(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"bytes": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime, UTC)}


@router.get("/storage", response_class=HTMLResponse)
def storage(request: Request, engine: EngineDep, settings: SettingsDep) -> HTMLResponse:
    with engine.connect() as conn:
        by_source = _raw_store_by_source(conn)
        recent = _recent_raw(conn)
        db_tables = _database_tables(conn)
    lake_tables, lake_problem = _lake(settings)
    exported = any(row["files"] or row["exported_at"] for row in lake_tables)
    return render(
        request,
        "storage/index.html",
        {
            "title": "Storage",
            "raw_root": settings.raw_store_dir,
            "raw_root_exists": settings.raw_store_dir.is_dir(),
            "key_layout": KEY_LAYOUT,
            "by_source": by_source,
            "raw_totals": {
                "files": sum(row.files for row in by_source),
                "bytes": sum(row.bytes for row in by_source),
            },
            "recent": recent,
            "db_tables": db_tables,
            "db_bytes": sum(row.bytes for row in db_tables),
            "lake_dir": settings.lake_dir,
            "lake_tables": lake_tables,
            "lake_exported": exported,
            "lake_problem": lake_problem,
            "warehouse_path": settings.warehouse_path,
            "warehouse": _file_info(settings.warehouse_path),
        },
    )


def _citing(conn: Connection, sha256: str) -> tuple[dict[str, int], list[Any]]:
    """How many rows cite a raw object, per table, and a few of them to link to."""
    r, v = t.records, t.record_versions
    counts = [
        select(literal("records").label("name"), func.count().label("rows")).where(
            r.c.raw_sha256 == sha256
        ),
        select(literal("record_versions").label("name"), func.count().label("rows")).where(
            v.c.raw_sha256 == sha256
        ),
        *(
            select(literal(name).label("name"), func.count().label("rows")).where(
                typed.c.raw_sha256 == sha256
            )
            for name, typed in TABLE_FOR.items()
        ),
    ]
    totals = {row.name: row.rows for row in conn.execute(union_all(*counts))}
    examples = [
        select(
            r.c.dataset.label("dataset"),
            r.c.id,
            r.c.external_id,
            cast(null(), Integer).label("version"),
        )
        .where(r.c.raw_sha256 == sha256)
        .order_by(r.c.id)
        .limit(CITING_SHOWN),
        select(r.c.dataset, r.c.id, r.c.external_id, v.c.version)
        .select_from(v.join(r, r.c.id == v.c.record_id))
        .where(v.c.raw_sha256 == sha256)
        .order_by(v.c.record_id, v.c.version)
        .limit(CITING_SHOWN),
        *(
            select(literal(name), typed.c.id, typed.c.external_id, cast(null(), Integer))
            .where(typed.c.raw_sha256 == sha256)
            .order_by(typed.c.id)
            .limit(CITING_SHOWN)
            for name, typed in TABLE_FOR.items()
        ),
    ]
    links = list(conn.execute(union_all(*(q.subquery().select() for q in examples))))
    return totals, links


def _preview(settings: Settings, sha256: str, fetches: Sequence[Any]) -> Preview:
    keys = list(dict.fromkeys(row.raw_key for row in fetches if row.raw_key))
    if not keys:
        return Preview(problem="No raw key was recorded for this object.")
    for key in keys:
        path = resolve_raw(settings.raw_store_dir, key, sha256)
        if path is None or not path.is_file():
            continue
        content_type = next((row.content_type for row in fetches if row.raw_key == key), None)
        size = path.stat().st_size
        if not is_text(content_type, key):
            return Preview(size=size)
        try:
            data = FilesystemRawStore(settings.raw_store_dir).get(key)
        except ValueError as exc:
            return Preview(size=size, problem=str(exc))
        text, truncated = decode_preview(data)
        return Preview(text=text, size=size, truncated=truncated)
    return Preview(problem=f"The file is not in the raw store at {settings.raw_store_dir}.")


def _fetches(conn: Connection, sha256: str) -> tuple[list[Any], int]:
    f = t.fetch_log
    rows = list(
        conn.execute(
            select(
                f.c.id,
                f.c.run_id,
                f.c.source_id,
                f.c.url,
                f.c.final_url,
                f.c.status,
                f.c.content_type,
                f.c.nbytes,
                f.c.raw_key,
                f.c.fetched_at,
            )
            .where(f.c.raw_sha256 == sha256)
            .order_by(f.c.fetched_at.desc(), f.c.id.desc())
            .limit(FETCHES_SHOWN)
        )
    )
    total = 0
    if rows:
        total = conn.execute(
            select(func.count()).select_from(f).where(f.c.raw_sha256 == sha256)
        ).scalar_one()
    return rows, total


@router.get("/storage/raw/{sha256}", response_class=HTMLResponse)
def raw_object(
    request: Request, sha256: str, engine: EngineDep, settings: SettingsDep
) -> HTMLResponse:
    _check_sha(sha256)
    with engine.connect() as conn:
        fetches, total = _fetches(conn, sha256)
        if not fetches:
            raise HTTPException(404, "No fetch in the log stored this raw object.")
        counts, citing = _citing(conn, sha256)
    return render(
        request,
        "storage/raw.html",
        {
            "title": f"Raw {sha256[:12]}",
            "sha256": sha256,
            "fetches": fetches,
            "total_fetches": total,
            "counts": counts,
            "citing": citing,
            "preview": _preview(settings, sha256, fetches),
            "preview_limit": PREVIEW_BYTES,
        },
    )


@router.get("/storage/raw/{sha256}/download")
def raw_download(sha256: str, engine: EngineDep, settings: SettingsDep) -> Response:
    """The stored bytes as an attachment; never rendered by the browser on this origin."""
    _check_sha(sha256)
    with engine.connect() as conn:
        fetches, _ = _fetches(conn, sha256)
    keys = list(dict.fromkeys(row.raw_key for row in fetches if row.raw_key))
    if not keys:
        raise HTTPException(404, "No fetch in the log stored this raw object.")
    for key in keys:
        path = resolve_raw(settings.raw_store_dir, key, sha256)
        if path is None or not path.is_file():
            continue
        try:
            data = FilesystemRawStore(settings.raw_store_dir).get(key)
        except ValueError as exc:
            raise HTTPException(500, f"The raw object failed its integrity check: {exc}") from exc
        ext = Path(key).suffix.lstrip(".").lower()
        filename = f"{sha256}.{ext if SAFE_EXT.fullmatch(ext) else 'bin'}"
        return Response(
            data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )
    raise HTTPException(404, "The raw file is not in the raw store.")
