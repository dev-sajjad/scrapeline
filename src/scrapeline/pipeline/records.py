"""Generic record store: current rows in `data.records`, full history in `data.record_versions`.

Identity is (source_id, dataset, external_id). For every validated record of a run:

    new identity                      -> records row (current_version 1), version 'insert'
    known identity, new content hash  -> records row updated (current_version + 1), version 'update'
    known identity, same content hash -> last_seen_at bumped only, no version
    removed identity seen again       -> removed_at cleared, version 'restored' (with payload)

Rules every implementation must keep:
- Versions are append-only and numbered 1, 2, 3... per record; consumers order by `version`.
- `valid_from` is the observation's fetched_at, but never earlier than the previous version's
  valid_from (reprocessing old raw must not put history out of order). A 'removed' version
  uses the time the run detected the removal (the run's start time).
- Each version stores the dataset's `schema_version` at write time.
- `mark_removed` runs only after a complete incremental run: every page reached, no fetch or
  parse errors, no rejects, and the page limit not hit. Reprocess and backfill runs never
  mark removals. Removed records get removed_at and a 'removed' version with a NULL payload.
- Personal-data fields (FieldDef.pii) are set to None before hashing and storage unless the
  source's `usage.allow_pii` is true, so they never enter the append-only history.

`PostgresRecordStore` applies two rules that follow from these: personal data is kept only
when `allow_pii` comes with a stated purpose (as scrape instructions require), and an
observation fetched before a record was removed (old raw during a reprocess) does not
restore it, because the record was not seen again.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import Connection, and_, bindparam, select, update

from scrapeline.catalog.datasets import DatasetType
from scrapeline.errors import DatasetError
from scrapeline.pipeline.identity import content_hash
from scrapeline.pipeline.load import LoadContext, Provenance, dedupe_latest
from scrapeline.pipeline.models import Record
from scrapeline.registry.config import UsageRights
from scrapeline.store import tables as t

ChangeType = Literal["insert", "update", "removed", "restored"]

_CHUNK = 1000


@dataclass
class RecordLoadStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    restored: int = 0
    removed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "restored": self.restored,
            "removed": self.removed,
        }


class RecordStore(Protocol):
    def load(
        self,
        conn: Connection,
        ctx: LoadContext,
        dataset: DatasetType,
        items: Iterable[tuple[Record, Provenance]],
    ) -> RecordLoadStats: ...

    def mark_removed(
        self, conn: Connection, ctx: LoadContext, dataset: DatasetType, seen: Collection[str]
    ) -> int: ...


@dataclass(frozen=True)
class _Current:
    """The stored state of one identity, with the valid_from of its latest version."""

    id: int
    external_id: str
    content_hash: str
    current_version: int
    removed_at: datetime | None
    valid_from: datetime | None


def keeps_personal_data(usage: UsageRights) -> bool:
    """Personal data is kept only with explicit permission and a stated purpose."""
    return usage.allow_pii and bool(usage.purpose.strip())


def storable(record: Record, dataset: DatasetType, usage: UsageRights) -> Record:
    """The record as it may be stored: personal-data fields blanked unless allowed."""
    if keeps_personal_data(usage):
        return record
    blank = {name: None for name in dataset.pii_fields if getattr(record, name) is not None}
    return record.model_copy(update=blank) if blank else record


def _payload(record: Record) -> dict[str, Any]:
    return record.model_dump(mode="json", exclude={"external_id", "canonical_url"})


def _not_before(value: datetime, floor: datetime | None) -> datetime:
    return value if floor is None or value >= floor else floor


def _chunks[T](values: Sequence[T]) -> Iterable[Sequence[T]]:
    for i in range(0, len(values), _CHUNK):
        yield values[i : i + _CHUNK]


class PostgresRecordStore:
    """`RecordStore` on `data.records` and `data.record_versions` with bulk, chunked SQL.

    Callers serialise writes per source (the runner holds a per-source advisory lock), so
    version numbers are computed from the rows read at the start of a load.
    """

    def load(
        self,
        conn: Connection,
        ctx: LoadContext,
        dataset: DatasetType,
        items: Iterable[tuple[Record, Provenance]],
    ) -> RecordLoadStats:
        grouped = dedupe_latest(items)
        foreign = sorted(set(grouped) - {dataset.name})
        if foreign:
            raise DatasetError(f"record types {foreign} do not belong to dataset {dataset.name}")
        pairs = grouped.get(dataset.name, [])
        for record, _ in pairs:
            if record.schema_version != dataset.version:
                raise DatasetError(
                    f"{dataset.name} record has schema_version {record.schema_version}, "
                    f"expected {dataset.version}"
                )

        stats = RecordLoadStats()
        current = self._current(conn, ctx, dataset, [r.external_id for r, _ in pairs])
        inserts: list[tuple[dict[str, Any], dict[str, Any]]] = []
        changes: list[tuple[dict[str, Any], dict[str, Any]]] = []
        touched: list[int] = []
        for record, prov in pairs:
            kept = storable(record, dataset, ctx.usage)
            digest = content_hash(kept)
            version = {
                "schema_version": dataset.version,
                "payload": _payload(kept),
                "content_hash": digest,
                "run_id": ctx.run_id,
                "scraper_version_id": ctx.scraper_version_id,
                "raw_sha256": prov.raw_sha256,
            }
            row = {
                "canonical_url": kept.canonical_url,
                "payload": version["payload"],
                "content_hash": digest,
                "schema_version": dataset.version,
                "last_seen_at": ctx.now,
                "fetched_at": prov.fetched_at,
                "run_id": ctx.run_id,
                "scraper_version_id": ctx.scraper_version_id,
                "raw_sha256": prov.raw_sha256,
            }
            old = current.get(record.external_id)
            if old is None:
                row.update(
                    source_id=ctx.source_id,
                    dataset=dataset.name,
                    external_id=record.external_id,
                    current_version=1,
                    first_seen_at=ctx.now,
                )
                version.update(version=1, change_type="insert", valid_from=prov.fetched_at)
                inserts.append((row, version))
                stats.inserted += 1
                continue
            if old.removed_at is not None:
                if prov.fetched_at <= old.removed_at:
                    stats.unchanged += 1  # observed before the removal: not seen again
                    continue
                change: ChangeType = "restored"
                stats.restored += 1
            elif old.content_hash == digest:
                touched.append(old.id)
                stats.unchanged += 1
                continue
            else:
                change = "update"
                stats.updated += 1
            next_version = old.current_version + 1
            row.update(id=old.id, current_version=next_version)
            version.update(
                record_id=old.id,
                version=next_version,
                change_type=change,
                valid_from=_not_before(prov.fetched_at, old.valid_from),
            )
            changes.append((row, version))

        self._insert(conn, inserts)
        self._apply(conn, changes)
        for chunk in _chunks(touched):
            conn.execute(
                update(t.records).where(t.records.c.id.in_(chunk)).values(last_seen_at=ctx.now)
            )
        return stats

    def mark_removed(
        self, conn: Connection, ctx: LoadContext, dataset: DatasetType, seen: Collection[str]
    ) -> int:
        """Mark every present record of the source's dataset that is not in `seen` as removed.

        The caller decides whether the run was complete enough to prove the absence.
        """
        seen_ids = set(seen)
        present = conn.execute(
            self._current_query(ctx, dataset).where(t.records.c.removed_at.is_(None))
        ).all()
        detected = ctx.run_started_at or ctx.now
        gone = [_Current(*row) for row in present if row.external_id not in seen_ids]
        stmt = (
            update(t.records)
            .where(t.records.c.id == bindparam("b_id"))
            .values(removed_at=detected, current_version=bindparam("b_version"))
        )
        for chunk in _chunks(gone):
            conn.execute(stmt, [{"b_id": r.id, "b_version": r.current_version + 1} for r in chunk])
            # No payload key: an explicit None would be stored as JSON null, not SQL NULL.
            conn.execute(
                t.record_versions.insert(),
                [
                    {
                        "record_id": r.id,
                        "version": r.current_version + 1,
                        "schema_version": dataset.version,
                        "change_type": "removed",
                        "content_hash": None,
                        "valid_from": _not_before(detected, r.valid_from),
                        "run_id": ctx.run_id,
                        "scraper_version_id": ctx.scraper_version_id,
                        "raw_sha256": None,
                    }
                    for r in chunk
                ],
            )
        return len(gone)

    @staticmethod
    def _current_query(ctx: LoadContext, dataset: DatasetType) -> Any:
        r, v = t.records, t.record_versions
        latest = and_(v.c.record_id == r.c.id, v.c.version == r.c.current_version)
        return (
            select(
                r.c.id,
                r.c.external_id,
                r.c.content_hash,
                r.c.current_version,
                r.c.removed_at,
                v.c.valid_from,
            )
            .select_from(r.outerjoin(v, latest))
            .where(r.c.source_id == ctx.source_id, r.c.dataset == dataset.name)
        )

    def _current(
        self, conn: Connection, ctx: LoadContext, dataset: DatasetType, ids: Sequence[str]
    ) -> dict[str, _Current]:
        found: dict[str, _Current] = {}
        for chunk in _chunks(ids):
            query = self._current_query(ctx, dataset).where(t.records.c.external_id.in_(chunk))
            found.update({row.external_id: _Current(*row) for row in conn.execute(query)})
        return found

    @staticmethod
    def _insert(conn: Connection, inserts: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
        stmt = t.records.insert().returning(t.records.c.id, t.records.c.external_id)
        for chunk in _chunks(inserts):
            ids = {row.external_id: row.id for row in conn.execute(stmt, [r for r, _ in chunk])}
            versions = [v | {"record_id": ids[r["external_id"]]} for r, v in chunk]
            conn.execute(t.record_versions.insert(), versions)

    @staticmethod
    def _apply(conn: Connection, changes: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
        if not changes:
            return
        columns = [c for c in changes[0][0] if c != "id"]
        stmt = (
            update(t.records)
            .where(t.records.c.id == bindparam("b_id"))
            .values({**{c: bindparam(f"b_{c}") for c in columns}, "removed_at": None})
        )
        for chunk in _chunks(changes):
            conn.execute(stmt, [{f"b_{k}": val for k, val in r.items()} for r, _ in chunk])
            conn.execute(t.record_versions.insert(), [v for _, v in chunk])
