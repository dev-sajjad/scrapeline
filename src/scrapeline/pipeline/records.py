"""Generic record store: current rows in `data.records`, full history in `data.record_versions`.

Identity is (source_id, dataset, external_id). For every validated record of a run:

    new identity                      -> records row (current_version 1), version 'insert'
    known identity, new content hash  -> records row updated (current_version + 1), version 'update'
    known identity, same content hash -> last_seen_at bumped only, no version
    removed identity seen again       -> removed_at cleared, version 'restored' (with payload)

`mark_removed` runs only after a complete crawl (every page reached, no fetch errors): records
of the source and dataset not seen in that run get removed_at = now and a 'removed' version with
a NULL payload. Versions are append-only; `valid_from` is the observation's fetched_at.
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
