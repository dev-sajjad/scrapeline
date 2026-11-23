"""Write a small, deterministic lake so the dbt project builds and tests without Postgres.

    uv run --extra analytics python scripts/make_sample_lake.py data/lake

Five days (2026-11-01 to 2026-11-05) of three sources: a simulated shop whose prices and
stock change, with a product removed and later restored; books.toscrape.com, with a run that
fails its quality checks; and a Legistar city, whose meetings load into typed tables. One shop
run is blocked with 429s and one books run by a challenge page served with HTTP 200. A
generation eval logs an agent run under a case id that is not a registered source. Runs follow the runner's rules (a run that fails its checks still
loads; only a complete crawl removes records) and product payloads are validated against the
`product` dataset type. Files follow `analytics/layout.py` and come from two exports that
overlap the way the exporter's late-commit rules allow, so staging has duplicates to remove.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from scrapeline.analytics.layout import OVERLAP, TABLES, LakeTable
from scrapeline.catalog.datasets import DatasetRegistry, DatasetType
from scrapeline.catalog.sites import SITE_CATEGORIES, TAXONOMY_VERSION
from scrapeline.pipeline.identity import content_hash

Row = dict[str, Any]

NAMESPACE = uuid.UUID("5f0c4e8e-2a59-4c33-9d63-0d7f6f1e7a10")
MAX_REJECT_RATE = 0.05
# Two exports: the first at noon on day 3, the second after the last run.
EXPORTS = (
    ("20261103T120000Z", datetime(2026, 11, 3, 12, tzinfo=UTC)),
    ("20261106T120000Z", datetime(2026, 11, 6, 12, tzinfo=UTC)),
)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    platform: str
    dataset: str
    site_id: int
    host: str
    scraper_version_id: int


SHOP = Source("shop-sim", "Simulated shop", "custom", "product", 1, "shop.example.com", 11)
BOOKS = Source(
    "books-toscrape", "Books to Scrape", "custom", "product", 2, "books.toscrape.com", 12
)
CITY = Source(
    "legistar-seattle", "Seattle City Council", "legistar", "meeting", 3, "webapi.legistar.com", 13
)
EVAL_CASE = "gen-g01-static-table"


@dataclass(frozen=True)
class Fetch:
    status: int | None  # None: transport error
    elapsed_ms: int
    error: str | None = None
    attempt: int = 1

    @property
    def failed(self) -> bool:
        return self.error is not None or (self.status or 0) >= 400


@dataclass
class _Identity:
    record_id: int
    version: int
    content_hash: str
    removed: bool = False


@dataclass
class SampleLake:
    """Rows per lake table, built the way the runner and the record store write them."""

    product: DatasetType
    rows: dict[str, list[Row]] = field(default_factory=lambda: defaultdict(list))
    _ids: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _records: dict[tuple[str, str], _Identity] = field(default_factory=dict)

    def next_id(self, table: str) -> int:
        self._ids[table] += 1
        return self._ids[table]

    def run(
        self,
        source: Source,
        started: datetime,
        *,
        products: Sequence[Row] = (),
        typed_loads: dict[str, dict[str, int]] | None = None,
        fetches: Sequence[Fetch] = (Fetch(200, 150),),
        rejects: int = 0,
        error: str | None = None,
    ) -> None:
        """One incremental run. With `error` it stops before loading, like a blocked fetch."""
        run_id = str(uuid.uuid5(NAMESPACE, f"{source.id}/{started.isoformat()}"))
        for second, fetch in enumerate(fetches, start=1):
            self.rows["fetch_log"].append(
                {
                    "id": self.next_id("fetch_log"),
                    "run_id": run_id,
                    "source_id": source.id,
                    "host": source.host,
                    "status": fetch.status,
                    "attempt": fetch.attempt,
                    "elapsed_ms": fetch.elapsed_ms,
                    "nbytes": 0 if fetch.status is None else 4096,
                    "kind": "page",
                    "fetched_at": started + timedelta(seconds=second),
                    "error": fetch.error,
                }
            )
        loaded: dict[str, dict[str, int]] = {}
        checks: dict[str, str] = {}
        status = "failed"
        complete = False
        if error is None:
            records = len(products) + sum(
                n for counts in (typed_loads or {}).values() for n in counts.values()
            )
            reject_rate = rejects / (records + rejects) if records + rejects else 0.0
            checks = {
                "record_count": self._check(run_id, source, started, "record_count", records),
                "reject_rate": self._check(run_id, source, started, "reject_rate", reject_rate),
            }
            status = "failed" if "fail" in checks.values() else "succeeded"
            complete = status == "succeeded" and rejects == 0 and not any(f.failed for f in fetches)
            loaded = dict(typed_loads or {})
            if products:
                observed_at = started + timedelta(seconds=len(fetches))
                loaded[source.dataset] = self._load(
                    source, run_id, started, observed_at, products, complete
                )
        stats = {
            "run_id": run_id,
            "source_id": source.id,
            "kind": "incremental",
            "status": status,
            "pages": sum(1 for f in fetches if not f.failed),
            "records": {
                name: sum(n for change, n in counts.items() if change != "removed")
                for name, counts in loaded.items()
            },
            "loaded": loaded,
            "rejects": rejects,
            "checks": checks,
            "watermark": None,
            "truncated": False,
            "complete": complete and bool(products),
            "removed": sum(counts.get("removed", 0) for counts in loaded.values()),
            "error": error,
            "incident_id": None,
        }
        self.rows["runs"].append(
            {
                "run_id": run_id,
                "source_id": source.id,
                "kind": "incremental",
                "status": status,
                "started_at": started,
                "finished_at": started + timedelta(minutes=1),
                "scraper_version_id": source.scraper_version_id,
                "stats": stats,
                "error": error,
            }
        )

    def _check(
        self, run_id: str, source: Source, started: datetime, name: str, value: float
    ) -> str:
        if name == "record_count":
            status, threshold = ("pass" if value > 0 else "fail"), ">=1"
        else:
            status, threshold = ("pass" if value <= MAX_REJECT_RATE else "fail"), "<=0.05"
        self.rows["check_results"].append(
            {
                "id": self.next_id("check_results"),
                "run_id": run_id,
                "source_id": source.id,
                "check_name": name,
                "status": status,
                "value": value,
                "threshold": threshold,
                "created_at": started + timedelta(seconds=50),
            }
        )
        return status

    def _load(
        self,
        source: Source,
        run_id: str,
        started: datetime,
        observed_at: datetime,
        products: Sequence[Row],
        complete: bool,
    ) -> dict[str, int]:
        counts = dict.fromkeys(("inserted", "updated", "unchanged", "restored", "removed"), 0)
        model = self.product.record_model()
        seen: set[str] = set()
        for fields in products:
            record = model.model_validate(fields)
            seen.add(record.external_id)
            digest = content_hash(record)
            payload = record.model_dump(mode="json", exclude={"external_id", "canonical_url"})
            state = self._records.get((source.id, record.external_id))
            if state is None:
                state = _Identity(self.next_id("records"), 0, digest)
                self._records[(source.id, record.external_id)] = state
                change, counter = "insert", "inserted"
            elif state.removed:
                change, counter = "restored", "restored"
            elif state.content_hash == digest:
                counts["unchanged"] += 1
                continue
            else:
                change, counter = "update", "updated"
            counts[counter] += 1
            state.content_hash, state.removed = digest, False
            self._version(source, run_id, record.external_id, state, change, observed_at, payload)
        if complete:
            for (source_id, external_id), state in self._records.items():
                if source_id == source.id and not state.removed and external_id not in seen:
                    state.removed = True
                    counts["removed"] += 1
                    # A removal is dated by the run that detected it.
                    self._version(source, run_id, external_id, state, "removed", started, None)
        return counts

    def _version(
        self,
        source: Source,
        run_id: str,
        external_id: str,
        state: _Identity,
        change: str,
        valid_from: datetime,
        payload: Row | None,
    ) -> None:
        state.version += 1
        observed = payload is not None
        self.rows["record_versions"].append(
            {
                "version_id": self.next_id("record_versions"),
                "record_id": state.record_id,
                "source_id": source.id,
                "dataset": self.product.name,
                "dataset_version": self.product.version,
                "external_id": external_id,
                "version": state.version,
                "change_type": change,
                "valid_from": valid_from,
                "content_hash": state.content_hash if observed else None,
                "payload": payload,
                "run_id": run_id,
                "raw_sha256": (
                    hashlib.sha256(f"{run_id}/{external_id}".encode()).hexdigest()
                    if observed
                    else None
                ),
                "scraper_version_id": source.scraper_version_id,
                "created_at": valid_from + timedelta(seconds=30),
            }
        )


def product(host: str, slug: str, title: str, price: str, quantity: int) -> Row:
    return {
        "url": f"https://{host}/p/{slug}",
        "title": title,
        "price": {"amount": price, "currency": "GBP"},
        "in_stock": quantity > 0,
        "stock_quantity": quantity,
        "availability": "In stock" if quantity > 0 else "Out of stock",
    }


def build(registry: DatasetRegistry) -> SampleLake:
    lake = SampleLake(product=registry.get("product"))
    pages = [Fetch(200, 120), Fetch(200, 180)]

    def shop(slug: str, title: str, price: str, quantity: int) -> Row:
        return product(SHOP.host, slug, title, price, quantity)

    def books(slug: str, title: str, price: str, quantity: int) -> Row:
        return product(BOOKS.host, slug, title, price, quantity)

    lake.run(
        SHOP,
        at(1, 6),
        products=[
            shop("kettle", "Electric kettle", "20.00", 12),
            shop("toaster", "Two-slice toaster", "35.00", 5),
            shop("blender", "Glass blender", "50.00", 3),
        ],
        fetches=pages,
    )
    lake.run(
        SHOP,
        at(2, 6),
        products=[
            shop("kettle", "Electric kettle", "20.00", 12),
            shop("toaster", "Two-slice toaster", "35.00", 0),
            shop("blender", "Glass blender", "50.00", 3),
            shop("mixer", "Stand mixer", "80.00", 7),
        ],
        fetches=[Fetch(200, 110), Fetch(200, 190)],
    )
    # The blender is missing from a complete crawl, so it is removed.
    lake.run(
        SHOP,
        at(3, 6),
        products=[
            shop("kettle", "Electric kettle", "18.00", 12),
            shop("toaster", "Two-slice toaster", "35.00", 0),
            shop("mixer", "Stand mixer", "80.00", 7),
        ],
        fetches=[Fetch(200, 100), Fetch(200, 200)],
    )
    lake.run(
        SHOP,
        at(4, 6),
        fetches=[Fetch(429, 40), Fetch(429, 45, attempt=2), Fetch(429, 50, attempt=3)],
        error="BlockedError: rate_limited (429): https://shop.example.com/",
    )
    lake.run(
        SHOP,
        at(4, 14),
        products=[
            shop("kettle", "Electric kettle", "18.00", 12),
            shop("toaster", "Two-slice toaster", "30.00", 4),
            shop("mixer", "Stand mixer", "80.00", 7),
        ],
        fetches=[Fetch(200, 150), Fetch(200, 250)],
    )
    # The blender is back, cheaper: restored.
    lake.run(
        SHOP,
        at(5, 6),
        products=[
            shop("kettle", "Electric kettle", "19.80", 12),
            shop("toaster", "Two-slice toaster", "30.00", 4),
            shop("blender", "Glass blender", "45.00", 2),
            shop("mixer", "Stand mixer", "80.00", 7),
        ],
        fetches=[Fetch(200, 130), Fetch(200, 170)],
    )

    lake.run(
        BOOKS,
        at(1, 7),
        products=[
            books("a-light-in-the-attic", "A Light in the Attic", "51.77", 22),
            books("tipping-the-velvet", "Tipping the Velvet", "53.74", 20),
        ],
    )
    # A page times out and a book is rejected: the run fails its checks but still loads what
    # it parsed, and being incomplete it removes nothing.
    lake.run(
        BOOKS,
        at(2, 7),
        products=[books("a-light-in-the-attic", "A Light in the Attic", "51.77", 22)],
        fetches=[Fetch(200, 300), Fetch(None, 10000, "ReadTimeout('timed out')")],
        rejects=1,
    )
    lake.run(
        BOOKS,
        at(4, 7),
        products=[
            books("a-light-in-the-attic", "A Light in the Attic", "47.00", 22),
            books("tipping-the-velvet", "Tipping the Velvet", "53.74", 20),
        ],
    )

    # A challenge page comes back as HTTP 200; only the run's error says it was blocked.
    lake.run(
        BOOKS,
        at(5, 7),
        fetches=[Fetch(200, 90)],
        error="BlockedError: challenge page: https://books.toscrape.com/",
    )

    lake.run(
        CITY,
        at(1, 8),
        typed_loads={
            "body": {"inserted": 3, "updated": 0, "unchanged": 0},
            "meeting": {"inserted": 10, "updated": 0, "unchanged": 0},
        },
        fetches=[Fetch(200, 400), Fetch(200, 600)],
    )
    # Finishes inside the first export's overlap window, so both exports carry it.
    lake.run(
        CITY,
        at(3, 11, 20),
        typed_loads={
            "body": {"inserted": 0, "updated": 0, "unchanged": 3},
            "meeting": {"inserted": 2, "updated": 1, "unchanged": 9},
        },
        fetches=[Fetch(200, 500), Fetch(200, 700)],
    )

    # The failed books run gets two repair attempts.
    for attempt, (outcome, cost) in enumerate((("failed", 0.18), ("passed", 0.42)), start=1):
        lake.rows["agent_runs"].append(
            {
                "id": str(uuid.uuid5(NAMESPACE, f"agent/{attempt}")),
                "role": "repair",
                "source_id": BOOKS.id,
                "model": "sample-model",
                "prompt_version": "repairer.v1",
                "attempt": attempt,
                "outcome": outcome,
                "input_tokens": 12000 * attempt,
                "output_tokens": 1500 * attempt,
                "cost_usd": cost,
                "started_at": at(2, 7, 10 * attempt),
                "finished_at": at(2, 7, 10 * attempt + 5),
            }
        )
    # `scrapeline eval` logs generation cases under `gen-<case>`, which is not a source.
    lake.rows["agent_runs"].append(
        {
            "id": str(uuid.uuid5(NAMESPACE, "agent/eval")),
            "role": "generate",
            "source_id": EVAL_CASE,
            "model": "sample-model",
            "prompt_version": "generator.v1",
            "attempt": 1,
            "outcome": "passed",
            "input_tokens": 30000,
            "output_tokens": 4000,
            "cost_usd": 0.9,
            "started_at": at(3, 9),
            "finished_at": at(3, 9, 5),
        }
    )
    _add_snapshots(lake, registry)
    return lake


def _add_snapshots(lake: SampleLake, registry: DatasetRegistry) -> None:
    registered = at(1, 0) - timedelta(days=1)
    classified = registered + timedelta(hours=12)
    lake.rows["sources"] = [
        {
            "id": s.id,
            "name": s.name,
            "platform": s.platform,
            "dataset": s.dataset,
            "site_id": s.site_id,
            "status": "active",
            "cadence": "daily",
            "created_at": registered,
        }
        for s in (SHOP, BOOKS, CITY)
    ]
    sites = (
        (SHOP, "example.com", "ecommerce", 0.9, "evidence", "GB", None),
        (BOOKS, "toscrape.com", "ecommerce", 1.0, "human", "GB", None),
        (CITY, "legistar.com", "government", 0.95, "rule", "US", "legistar"),
    )
    for source, domain, category, confidence, method, country, platform in sites:
        lake.rows["sites"].append(
            {
                "id": source.site_id,
                "host": source.host,
                "registered_domain": domain,
                "category": category,
                "category_confidence": confidence,
                "category_method": method,
                "tags": [],
                "country": country,
                "language": "en",
                "platform": platform,
                "classified_at": classified,
            }
        )
        lake.rows["classification_log"].append(
            {
                "id": lake.next_id("classification_log"),
                "site_id": source.site_id,
                "old_category": "unclassified",
                "new_category": category,
                "confidence": confidence,
                "method": method,
                "taxonomy_version": TAXONOMY_VERSION,
                "actor": "owner" if method == "human" else "classifier",
                "created_at": classified,
            }
        )
    lake.rows["site_categories"] = [
        {
            "code": c.code,
            "label": c.label,
            "description": c.description,
            "taxonomy_version": TAXONOMY_VERSION,
        }
        for c in SITE_CATEGORIES
    ]
    lake.rows["dataset_types"] = [
        {
            "name": ds.name,
            "version": ds.version,
            "pack": ds.pack,
            "storage": ds.storage,
            "description": ds.description,
            "fields": {name: f.model_dump(mode="json") for name, f in ds.fields.items()},
            "key_fields": list(ds.key_fields),
        }
        for ds in registry
    ]
    lake.rows["incidents"] = [
        {
            "id": 1,
            "source_id": BOOKS.id,
            "kind": "quality",
            "status": "closed",
            "summary": "quality checks failed: reject_rate",
            "opened_at": at(2, 7, 1),
            "closed_at": at(4, 7, 1),
        },
        {
            "id": 2,
            "source_id": SHOP.id,
            "kind": "blocked",
            "status": "closed",
            "summary": "BlockedError: rate_limited (429): https://shop.example.com/",
            "opened_at": at(4, 6, 1),
            "closed_at": at(4, 14, 1),
        },
        {
            "id": 3,
            "source_id": BOOKS.id,
            "kind": "blocked",
            "status": "open",
            "summary": "BlockedError: challenge page: https://books.toscrape.com/",
            "opened_at": at(5, 7, 1),
            "closed_at": None,
        },
    ]


def exported(
    table: LakeTable, rows: Iterable[Row], after: datetime | None, until: datetime
) -> list[Row]:
    """The rows of an append table one export selects: (after, until], widened by the overlap."""
    if table.watermark is None:
        raise ValueError(f"{table.name} has no watermark")
    lower = after
    if after is not None and table.late_commits == "overlap":
        lower = after - OVERLAP
    return [
        row
        for row in rows
        if (lower is None or row[table.watermark] > lower) and row[table.watermark] <= until
    ]


def write(lake: SampleLake, root: Path) -> list[Path]:
    con = duckdb.connect()
    con.execute("set TimeZone = 'UTC'")
    written: list[Path] = []
    appends = [t for t in TABLES.values() if t.mode == "append"]
    after: datetime | None = None
    for export_id, until in EXPORTS:
        for table in appends:
            files: dict[str, list[Row]] = defaultdict(list)
            for row in exported(table, lake.rows[table.name], after, until):
                files[_partition_path(table, row, export_id)].append(row)
            for path, rows in sorted(files.items()):
                written.append(_write_file(con, table, rows, root / path))
        after = until
    for table in TABLES.values():
        if table.mode == "snapshot":
            written.append(_write_file(con, table, lake.rows[table.name], root / table.path))
    return written


def _partition_path(table: LakeTable, row: Row, export_id: str) -> str:
    if table.partition_time is None:
        raise ValueError(f"{table.name} has no partition time")
    values = {name: row[name] for name in table.partition_by if name != "date"}
    values["date"] = row[table.partition_time].date().isoformat()
    return table.path.format(**values, export_id=export_id)


def _write_file(
    con: duckdb.DuckDBPyConnection, table: LakeTable, rows: list[Row], path: Path
) -> Path:
    con.execute(
        "create or replace temp table staged ("
        + ", ".join(f'"{c.name}" {c.type}' for c in table.columns)
        + ")"
    )
    values = [
        [
            json.dumps(row[c.name]) if c.type == "JSON" and row[c.name] is not None else row[c.name]
            for c in table.columns
        ]
        for row in rows
    ]
    if values:
        placeholders = ", ".join("?" for _ in table.columns)
        con.executemany(f"insert into staged values ({placeholders})", values)
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"copy staged to '{path}' (format parquet)")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("lake_dir", type=Path, help="directory to write; must be new or empty")
    args = parser.parse_args(argv)
    root: Path = args.lake_dir
    if root.exists() and any(root.iterdir()):
        print(f"{root} is not empty; the sample lake is only written to a new directory")
        return 1
    files = write(build(DatasetRegistry.load(())), root)
    print(f"wrote {len(files)} files to {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
