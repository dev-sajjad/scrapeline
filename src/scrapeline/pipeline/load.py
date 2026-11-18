"""Idempotent upsert with lineage and change detection (CLAUDE.md §4.1).

new identity              -> INSERT (first_seen_at = last_seen_at = now)
same identity, new hash   -> UPDATE content + lineage, bump last_seen_at
same identity, same hash  -> bump last_seen_at only
Loads are pydantic-validated records + SQL, never pandas (silent None/NaN/date coercion).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Table, bindparam, select, update

from scrapeline.pipeline.identity import content_hash
from scrapeline.pipeline.models import Record
from scrapeline.store import tables as t

TABLE_FOR: dict[str, Table] = {
    "body": t.bodies,
    "meeting": t.meetings,
    "document": t.documents,
    "agenda_item": t.agenda_items,
}
_CHUNK = 1000


@dataclass(frozen=True)
class Provenance:
    raw_sha256: str | None
    fetched_at: datetime


@dataclass
class LoadStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged}


@dataclass
class LoadContext:
    source_id: str
    jurisdiction_id: str
    run_id: uuid.UUID
    scraper_version_id: int | None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


def _content_columns(record: Record, table: Table) -> dict[str, Any]:
    data = record.model_dump(exclude={"external_id", "canonical_url"})
    row: dict[str, Any] = {}
    for key, value in data.items():
        if key not in table.c:
            raise KeyError(f"{record.record_type}.{key} has no column in {table.name}")
        row[key] = int(value) if isinstance(value, bool) else value
    if "jurisdiction_id" in table.c:
        row["jurisdiction_id"] = None  # filled from context
    return row


def dedupe_latest(
    items: Iterable[tuple[Record, Provenance]],
) -> dict[str, list[tuple[Record, Provenance]]]:
    """Group by record type; within a run the latest fetch of an identity wins."""
    latest: dict[tuple[str, str], tuple[Record, Provenance]] = {}
    for record, prov in items:
        key = (record.record_type, record.external_id)
        current = latest.get(key)
        if current is None or prov.fetched_at >= current[1].fetched_at:
            latest[key] = (record, prov)
    grouped: dict[str, list[tuple[Record, Provenance]]] = {}
    for (rtype, _), pair in latest.items():
        grouped.setdefault(rtype, []).append(pair)
    return grouped


def load_records(
    conn: Connection, ctx: LoadContext, items: Iterable[tuple[Record, Provenance]]
) -> dict[str, LoadStats]:
    stats: dict[str, LoadStats] = {}
    for rtype, pairs in dedupe_latest(items).items():
        stats[rtype] = _load_table(conn, ctx, TABLE_FOR[rtype], pairs)
    return stats


def _existing_hashes(
    conn: Connection, table: Table, source_id: str, ids: Sequence[str]
) -> dict[str, str]:
    found: dict[str, str] = {}
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        rows = conn.execute(
            select(table.c.external_id, table.c.content_hash).where(
                table.c.source_id == source_id, table.c.external_id.in_(chunk)
            )
        )
        found.update({row.external_id: row.content_hash for row in rows})
    return found


def _load_table(
    conn: Connection, ctx: LoadContext, table: Table, pairs: list[tuple[Record, Provenance]]
) -> LoadStats:
    stats = LoadStats()
    existing = _existing_hashes(conn, table, ctx.source_id, [r.external_id for r, _ in pairs])
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    touched: list[str] = []
    for record, prov in pairs:
        digest = content_hash(record)
        old = existing.get(record.external_id)
        if old == digest:
            touched.append(record.external_id)
            continue
        row = _content_columns(record, table)
        if "jurisdiction_id" in row:
            row["jurisdiction_id"] = ctx.jurisdiction_id
        row.update(
            external_id=record.external_id,
            canonical_url=record.canonical_url,
            content_hash=digest,
            schema_version=record.schema_version,
            last_seen_at=ctx.now,
            fetched_at=prov.fetched_at,
            run_id=ctx.run_id,
            scraper_version_id=ctx.scraper_version_id,
            raw_sha256=prov.raw_sha256,
        )
        if old is None:
            row.update(source_id=ctx.source_id, first_seen_at=ctx.now)
            inserts.append(row)
        else:
            updates.append(row)

    if inserts:
        conn.execute(table.insert(), inserts)
        stats.inserted = len(inserts)
    if updates:
        cols = [c for c in updates[0] if c != "external_id"]
        stmt = (
            update(table)
            .where(
                table.c.source_id == ctx.source_id,
                table.c.external_id == bindparam("b_external_id"),
            )
            .values({c: bindparam(f"b_{c}") for c in cols})
        )
        conn.execute(stmt, [{f"b_{k}": v for k, v in row.items()} for row in updates])
        stats.updated = len(updates)
    for i in range(0, len(touched), _CHUNK):
        conn.execute(
            update(table)
            .where(
                table.c.source_id == ctx.source_id,
                table.c.external_id.in_(touched[i : i + _CHUNK]),
            )
            .values(last_seen_at=ctx.now)
        )
    stats.unchanged = len(touched)
    return stats
