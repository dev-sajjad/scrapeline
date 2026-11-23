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
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import Connection

from scrapeline.catalog.datasets import DatasetType
from scrapeline.pipeline.load import LoadContext, Provenance
from scrapeline.pipeline.models import Record

ChangeType = Literal["insert", "update", "removed", "restored"]


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
