"""Ingest: parse raw documents, route the records by storage and load them.

Records of the source's generic dataset go to the record store, which keeps their history;
typed record types (meetings, bodies, ...) go to their own tables. Rejects are stored with
their reason, and without personal data unless the source may keep it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection

from scrapeline.adapters.base import Adapter
from scrapeline.adapters.rawdoc import ParseResult, RawDoc, Reject, Target
from scrapeline.catalog.datasets import DatasetType
from scrapeline.errors import DatasetError
from scrapeline.pipeline.load import TABLE_FOR, LoadContext, LoadStats, Provenance, load_records
from scrapeline.pipeline.models import Record
from scrapeline.pipeline.records import RecordLoadStats, RecordStore, keeps_personal_data
from scrapeline.registry.config import SourceConfig, UsageRights
from scrapeline.store import tables as t

Items = list[tuple[Record, Provenance]]


@dataclass
class Collected:
    """What a run gathered before loading: records, rejects, fetch-log rows and watermark."""

    items: Items = field(default_factory=list)
    rejects: list[tuple[Reject, str | None]] = field(default_factory=list)
    fetch_rows: list[dict[str, Any]] = field(default_factory=list)
    watermark: datetime | None = None

    def absorb(self, result: ParseResult, prov: Provenance) -> None:
        self.items.extend((rec, prov) for rec in result.records)
        self.rejects.extend((rej, prov.raw_sha256) for rej in result.rejects)
        if result.watermark and (self.watermark is None or result.watermark > self.watermark):
            self.watermark = result.watermark


def parse_raw(adapter: Adapter, raw: RawDoc, collected: Collected) -> list[Target]:
    """Parse one raw document into `collected`; return the targets it links to."""
    result = adapter.parse(raw)
    collected.absorb(result, Provenance(raw.sha256, raw.fetched_at))
    return result.next_targets


def split_by_storage(dataset: DatasetType, items: Items) -> tuple[Items, Items]:
    """(records for the generic record store, records for typed tables).

    A record type that is neither the source's generic dataset nor a typed table, or a
    generic record of another schema version, is a parser bug. Both raise DatasetError here,
    before the load transaction starts, rather than being dropped or half-loaded.
    """
    generic: Items = []
    typed: Items = []
    unknown: set[str] = set()
    versions: set[int] = set()
    for rec, prov in items:
        if dataset.storage == "generic" and rec.record_type == dataset.name:
            generic.append((rec, prov))
            versions.add(rec.schema_version)
        elif rec.record_type in TABLE_FOR:
            typed.append((rec, prov))
        else:
            unknown.add(rec.record_type)
    if unknown:
        raise DatasetError(
            f"parser produced {sorted(unknown)} records; the source's dataset is {dataset.name}"
        )
    if versions - {dataset.version}:
        raise DatasetError(
            f"{dataset.name} records have schema_version {sorted(versions)}, "
            f"expected {dataset.version}"
        )
    return generic, typed


def storable_reject(reject: Reject, dataset: DatasetType, usage: UsageRights) -> dict[str, Any]:
    """A reject's data as it may be stored in pipeline.rejects.

    The record store's personal-data rule applies here too: the dataset's personal-data
    fields are blanked unless the source may keep personal data, so a reject never keeps
    what a valid record would not.
    """
    if reject.record_type != dataset.name or keeps_personal_data(usage):
        return reject.data
    return reject.data | {name: None for name in reject.data if name in dataset.pii_fields}


def write_rejects(
    conn: Connection,
    cfg: SourceConfig,
    dataset: DatasetType,
    run_id: uuid.UUID,
    rejects: list[tuple[Reject, str | None]],
) -> None:
    """Store each reject with its reason and the raw document it came from."""
    if not rejects:
        return
    conn.execute(
        t.rejects.insert(),
        [
            {
                "run_id": run_id,
                "source_id": cfg.id,
                "record_type": rej.record_type,
                "raw_sha256": sha,
                "record": storable_reject(rej, dataset, cfg.usage),
                "reason": rej.reason,
            }
            for rej, sha in rejects
        ],
    )


def load_items(
    conn: Connection,
    ctx: LoadContext,
    dataset: DatasetType,
    *,
    generic: Items,
    typed: Items,
    record_store: RecordStore,
) -> tuple[dict[str, LoadStats], RecordLoadStats | None]:
    """Load typed records into their tables, then generic ones into the record store.

    Returns the typed tables' stats and the record store's (None for a typed dataset).
    """
    typed_stats = load_records(conn, ctx, typed)
    if dataset.storage != "generic":
        return typed_stats, None
    return typed_stats, record_store.load(conn, ctx, dataset, generic)
