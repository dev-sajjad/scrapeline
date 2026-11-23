"""Lake export from PostgreSQL to Parquet, read back with DuckDB as dbt would."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pytest
from sqlalchemy import Engine, func, select, text, update
from typer.testing import CliRunner

from scrapeline import settings as settings_module
from scrapeline.analytics import export as export_module
from scrapeline.analytics.export import ParquetLakeExporter
from scrapeline.analytics.layout import OVERLAP, STATE_PATH, TABLES
from scrapeline.catalog.datasets import (
    DatasetRegistry,
    DatasetType,
    FieldDef,
    default_registry,
    resolve_model,
)
from scrapeline.cli import app
from scrapeline.errors import DatasetError
from scrapeline.ops import runs
from scrapeline.pipeline.load import LoadContext, Provenance
from scrapeline.pipeline.models import Record
from scrapeline.pipeline.records import PostgresRecordStore
from scrapeline.quality.incidents import open_incident
from scrapeline.registry.config import SourceConfig, UsageRights
from scrapeline.settings import Settings
from scrapeline.store import db
from scrapeline.store import tables as t

pytestmark = pytest.mark.integration


def source(source_id: str, entry: str, dataset: str, **extra: Any) -> SourceConfig:
    return SourceConfig.model_validate(
        {
            "id": source_id,
            "name": source_id,
            "platform": "custom",
            "kind": "html",
            "entry": entry,
            "dataset": dataset,
            "legal": {"terms_checked": "2026-11-24", "notes": "test"},
            "adapter": "tests.integration.test_lake_export:none",
            **extra,
        }
    )


SHOP = source("shop-lake", "https://shop.example/products", "product", cadence="hourly")
NEWS = source("news-lake", "https://news.example/articles", "article", cadence="weekly")
ALLOWED = UsageRights(purpose="Media monitoring", allow_pii=True)
PRODUCT = default_registry().get("product")
ARTICLE = default_registry().get("article")
STORE = PostgresRecordStore()
DAY = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
APPEND = [name for name, table in TABLES.items() if table.mode == "append"]
SNAPSHOTS = sorted(table.path for table in TABLES.values() if table.mode == "snapshot")


def _clear_agent_runs(engine: Engine) -> None:
    # clean_db leaves agent runs alone, and other tests count them.
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE ops.agent_run CASCADE"))


@pytest.fixture
def engine(clean_db: Engine) -> Iterator[Engine]:
    _clear_agent_runs(clean_db)
    with clean_db.begin() as conn:
        runs.sync_source(conn, SHOP)
        runs.sync_source(conn, NEWS)
    yield clean_db
    _clear_agent_runs(clean_db)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    return tmp_path / "lake"


@pytest.fixture
def exporter(engine: Engine, lake: Path) -> ParquetLakeExporter:
    return ParquetLakeExporter(engine, lake, default_registry())


def url(slug: str) -> str:
    return f"https://shop.example/p/{slug}"


def product(slug: str, price: str = "10.00") -> Record:
    return resolve_model("product").model_validate(
        {"url": url(slug), "title": slug.title(), "price": {"amount": price, "currency": "GBP"}}
    )


def article(slug: str, author: str | None = None) -> Record:
    return resolve_model("article").model_validate(
        {"url": f"https://news.example/a/{slug}", "title": slug.title(), "author": author}
    )


def new_run(
    engine: Engine, cfg: SourceConfig = SHOP, usage: UsageRights | None = None
) -> LoadContext:
    with engine.begin() as conn:
        version_id = runs.scraper_version(conn, cfg.id, cfg.adapter, "code", "human")
        run_id = runs.start_run(
            conn,
            source_id=cfg.id,
            kind="incremental",
            scraper_version_id=version_id,
            since=None,
            until=None,
        )
    return LoadContext(cfg.id, None, run_id, version_id, usage=usage or cfg.usage)


def load(
    engine: Engine, ctx: LoadContext, dataset: DatasetType, *items: tuple[Record, datetime]
) -> None:
    with engine.begin() as conn:
        STORE.load(
            conn,
            ctx,
            dataset,
            [(rec, Provenance(f"sha-{i}", when)) for i, (rec, when) in enumerate(items)],
        )


def finish(engine: Engine, ctx: LoadContext) -> None:
    with engine.begin() as conn:
        runs.finish_run(conn, ctx.run_id, status="succeeded", stats={"records": 2}, error=None)


def log_fetch(engine: Engine, ctx: LoadContext, fetched_at: datetime) -> int:
    with engine.begin() as conn:
        fetch_id: int = conn.execute(
            t.fetch_log.insert()
            .values(
                run_id=ctx.run_id,
                source_id=ctx.source_id,
                url="https://Shop.Example:8443/products?page=2",
                status=200,
                attempt=1,
                elapsed_ms=120,
                nbytes=2048,
                kind="page",
                fetched_at=fetched_at,
            )
            .returning(t.fetch_log.c.id)
        ).scalar_one()
    return fetch_id


def populate(engine: Engine) -> LoadContext:
    """One row or more in every exported table; returns the finished shop run."""
    shop = new_run(engine)
    load(engine, shop, PRODUCT, (product("a"), DAY), (product("b"), DAY))
    log_fetch(engine, shop, DAY)
    finish(engine, shop)
    news = new_run(engine, NEWS)  # still running: not exported
    load(engine, news, ARTICLE, (article("story", author="Jane Example"), DAY))
    with engine.begin() as conn:
        conn.execute(
            t.check_result.insert().values(
                run_id=shop.run_id,
                source_id=SHOP.id,
                check_name="record_count",
                status="pass",
                value=2.0,
                threshold=">=1",
            )
        )
        agent = {
            "role": "repair",
            "source_id": SHOP.id,
            "model": "claude-test",
            "prompt_version": "repairer.v1",
            "attempt": 1,
            "outcome": "passed",
            "input_tokens": 1200,
            "output_tokens": 300,
            "cost_usd": 0.0125,
            "started_at": DAY,
        }
        conn.execute(
            t.agent_run.insert(),
            [
                agent | {"id": uuid.uuid4(), "finished_at": DAY + timedelta(minutes=2)},
                agent | {"id": uuid.uuid4(), "finished_at": None, "outcome": "error"},
            ],
        )
        site_id = conn.execute(
            select(t.sources.c.site_id).where(t.sources.c.id == SHOP.id)
        ).scalar_one()
        conn.execute(
            t.classification_log.insert().values(
                site_id=site_id,
                old_category="unclassified",
                new_category="ecommerce",
                confidence=0.9,
                method="rule",
                taxonomy_version=1,
                actor="classifier",
            )
        )
        open_incident(conn, source_id=SHOP.id, kind="drift", summary="selector changed")
    return shop


def read(lake: Path, name: str, columns: str = "*") -> list[dict[str, Any]]:
    """Rows of one lake table, read the way dbt reads them."""
    pattern = lake / TABLES[name].glob
    if not list(lake.glob(TABLES[name].glob)):
        return []
    with duckdb.connect() as duck:
        relation = duck.sql(
            f"SELECT {columns} FROM read_parquet('{pattern}', hive_partitioning = true)"
        )
        return [dict(zip(relation.columns, row, strict=True)) for row in relation.fetchall()]


def lake_files(lake: Path) -> list[str]:
    """Every file under the lake except the export state."""
    return sorted(
        p.relative_to(lake).as_posix()
        for p in lake.rglob("*")
        if p.is_file() and not p.relative_to(lake).as_posix().startswith("_state/")
    )


def db_count(engine: Engine, table: Any, *where: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table).where(*where)).scalar())


def test_every_table_is_written_with_the_declared_columns_and_types(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path
) -> None:
    populate(engine)
    report = exporter.export()

    with duckdb.connect() as duck:
        for name, table in TABLES.items():
            described = duck.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{lake / table.glob}', "
                "hive_partitioning = false)"
            ).fetchall()
            expected = [
                (c.name, duck.execute(f"SELECT typeof(CAST(NULL AS {c.type}))").fetchall()[0][0])
                for c in table.columns
            ]
            assert [(row[0], row[1]) for row in described] == expected, name

    assert report.rows == {
        "record_versions": 3,
        "runs": 1,
        "check_results": 1,
        "fetch_log": 1,
        "agent_runs": 1,
        "classification_log": 1,
        "incidents": 1,
        "sources": 2,
        "sites": 2,
        "site_categories": db_count(engine, t.site_category),
        "dataset_types": len(list(default_registry())),
    }
    assert sorted(report.files) == lake_files(lake)
    assert all(report.export_id in path for path in report.files if not path.startswith("snap"))

    versions = {row["external_id"]: row for row in read(lake, "record_versions")}
    assert set(versions) == {url("a"), url("b"), "https://news.example/a/story"}
    first = versions[url("a")]
    assert (first["dataset"], first["dataset_version"], first["version"]) == ("product", 1, 1)
    assert (first["change_type"], first["valid_from"], first["raw_sha256"]) == (
        "insert",
        DAY,
        "sha-0",
    )
    assert json.loads(first["payload"])["price"] == {"amount": "10.00", "currency": "GBP"}
    with engine.connect() as conn:
        stored = conn.execute(
            select(t.record_versions.c.id, t.record_versions.c.created_at)
            .join(t.records, t.records.c.id == t.record_versions.c.record_id)
            .where(t.records.c.external_id == url("a"))
        ).one()
    assert (first["version_id"], first["created_at"]) == (stored.id, stored.created_at)

    (run,) = read(lake, "runs")
    shop_run = db_count(
        engine, t.run, t.run.c.source_id == SHOP.id, t.run.c.finished_at.is_not(None)
    )
    assert shop_run == 1 and run["source_id"] == SHOP.id and run["status"] == "succeeded"
    assert json.loads(run["stats"]) == {"records": 2}
    (fetch,) = read(lake, "fetch_log")
    assert (fetch["host"], fetch["status"], fetch["fetched_at"]) == ("shop.example", 200, DAY)
    (agent,) = read(lake, "agent_runs")
    assert (agent["outcome"], agent["cost_usd"], agent["input_tokens"]) == ("passed", 0.0125, 1200)
    sources = {row["id"]: row for row in read(lake, "sources")}
    assert {sid: row["cadence"] for sid, row in sources.items()} == {
        SHOP.id: "hourly",
        NEWS.id: "weekly",
    }
    assert sources[SHOP.id]["dataset"] == "product" and sources[SHOP.id]["site_id"] is not None
    (incident,) = read(lake, "incidents")
    assert (incident["kind"], incident["status"]) == ("drift", "open")
    product_type = next(
        row
        for row in read(lake, "dataset_types")
        if (row["name"], row["version"]) == ("product", 1)
    )
    assert json.loads(product_type["key_fields"]) == ["url"]
    assert json.loads(product_type["fields"])["price"]["type"] == "money"


def test_files_are_partitioned_by_dataset_and_utc_date(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path
) -> None:
    evening = datetime(2026, 11, 20, 23, 30, tzinfo=ZoneInfo("America/New_York"))  # 21st in UTC
    shop = new_run(engine)
    load(engine, shop, PRODUCT, (product("a"), evening), (product("b"), DAY))
    load(engine, new_run(engine, NEWS), ARTICLE, (article("story"), DAY))
    log_fetch(engine, shop, evening)
    log_fetch(engine, shop, DAY)

    report = exporter.export(tables=["record_versions", "fetch_log"])

    eid = report.export_id
    assert sorted(report.files) == [
        f"fetch_log/date=2026-11-20/{eid}.parquet",
        f"fetch_log/date=2026-11-21/{eid}.parquet",
        f"record_versions/dataset=article/date=2026-11-20/{eid}.parquet",
        f"record_versions/dataset=product/date=2026-11-20/{eid}.parquet",
        f"record_versions/dataset=product/date=2026-11-21/{eid}.parquet",
    ]
    assert lake_files(lake) == sorted(report.files)
    partitions = {
        (row["dataset"], row["date"], row["external_id"])
        for row in read(lake, "record_versions", "dataset, date, external_id")
    }
    assert partitions == {
        ("product", date(2026, 11, 21), url("a")),
        ("product", date(2026, 11, 20), url("b")),
        ("article", date(2026, 11, 20), "https://news.example/a/story"),
    }


def test_a_second_export_without_changes_writes_no_new_files(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path
) -> None:
    populate(engine)
    exporter.export()
    before = lake_files(lake)

    again = exporter.export()

    assert {name: again.rows[name] for name in APPEND} == dict.fromkeys(APPEND, 0)
    assert sorted(again.files) == SNAPSHOTS
    assert lake_files(lake) == before
    # Overlap tables re-read their last hour; nothing read twice is written twice.
    assert len(read(lake, "runs")) == 1
    assert len(read(lake, "fetch_log")) == db_count(engine, t.fetch_log)
    assert len(read(lake, "agent_runs")) == 1


def test_new_versions_are_appended_by_the_next_export(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path
) -> None:
    load(engine, new_run(engine), PRODUCT, (product("a"), DAY))
    first = exporter.export(tables=["record_versions"])
    load(
        engine,
        new_run(engine),
        PRODUCT,
        (product("a", price="12.50"), DAY + timedelta(hours=1)),
        (product("c"), DAY + timedelta(hours=1)),
    )

    second = exporter.export(tables=["record_versions"])

    assert (first.rows["record_versions"], second.rows["record_versions"]) == (1, 2)
    assert set(first.files).isdisjoint(second.files)
    exported = sorted(row["version_id"] for row in read(lake, "record_versions"))
    with engine.connect() as conn:
        stored = sorted(conn.execute(select(t.record_versions.c.id)).scalars())
    assert exported == stored
    history = sorted(
        (row["version"], row["change_type"])
        for row in read(lake, "record_versions")
        if row["external_id"] == url("a")
    )
    assert history == [(1, "insert"), (2, "update")]


def test_a_version_committed_late_by_an_older_transaction_is_not_lost(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path
) -> None:
    slow_run, quick_run = new_run(engine), new_run(engine)
    with engine.connect() as slow, slow.begin():
        STORE.load(slow, slow_run, PRODUCT, [(product("late"), Provenance("sha-late", DAY))])
        # Starts after the slow transaction and commits before the export.
        load(engine, quick_run, PRODUCT, (product("quick"), DAY))
        first = exporter.export(tables=["record_versions"])
    second = exporter.export(tables=["record_versions"])

    # Both rows are stamped after the slow transaction's start, so the first cutoff
    # (just before that start) holds them back until they are surely committed.
    assert (first.rows["record_versions"], second.rows["record_versions"]) == (0, 2)
    exported = sorted(row["external_id"] for row in read(lake, "record_versions"))
    assert exported == [url("late"), url("quick")]


def test_a_fetch_older_than_the_overlap_that_commits_late_is_still_exported(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path
) -> None:
    ctx = new_run(engine)
    now = datetime.now(UTC)
    log_fetch(engine, ctx, now - timedelta(minutes=1))
    exporter.export(tables=["fetch_log"])
    # A run longer than the overlap writes its first fetch at the end: hours old, committed now.
    log_fetch(engine, ctx, now - OVERLAP - timedelta(hours=2))
    late = exporter.export(tables=["fetch_log"])

    assert late.rows["fetch_log"] == 1
    with engine.connect() as conn:
        stored = sorted(conn.execute(select(t.fetch_log.c.id)).scalars())
    assert sorted(row["id"] for row in read(lake, "fetch_log")) == stored


def test_a_fetch_logged_late_within_the_overlap_is_exported_once(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path
) -> None:
    ctx = new_run(engine)
    now = datetime.now(UTC)
    log_fetch(engine, ctx, now - timedelta(minutes=5))
    first = exporter.export(tables=["fetch_log"])
    # Fetched before the exported watermark, committed after the export (end of a long run).
    log_fetch(engine, ctx, now - timedelta(minutes=40))
    second = exporter.export(tables=["fetch_log"])
    third = exporter.export(tables=["fetch_log"])

    assert [r.rows["fetch_log"] for r in (first, second, third)] == [1, 1, 0]
    with engine.connect() as conn:
        stored = sorted(conn.execute(select(t.fetch_log.c.id)).scalars())
    assert sorted(row["id"] for row in read(lake, "fetch_log")) == stored


@pytest.mark.parametrize(
    ("stored_with", "exported_with", "kept"),
    [
        (UsageRights(), UsageRights(), False),
        (UsageRights(allow_pii=True), UsageRights(allow_pii=True), False),
        (ALLOWED, ALLOWED, True),
        (ALLOWED, UsageRights(), False),
    ],
    ids=["default", "no-purpose", "allowed", "revoked-before-export"],
)
def test_personal_data_fields_are_dropped_unless_the_source_keeps_them(
    engine: Engine,
    exporter: ParquetLakeExporter,
    lake: Path,
    stored_with: UsageRights,
    exported_with: UsageRights,
    kept: bool,
) -> None:
    with engine.begin() as conn:
        runs.sync_source(conn, NEWS.model_copy(update={"usage": stored_with}))
    ctx = new_run(engine, NEWS, usage=stored_with)
    load(engine, ctx, ARTICLE, (article("story", author="Jane Example"), DAY))
    with engine.begin() as conn:
        runs.sync_source(conn, NEWS.model_copy(update={"usage": exported_with}))

    exporter.export(tables=["record_versions"])

    (row,) = read(lake, "record_versions")
    payload = json.loads(row["payload"])
    assert payload["title"] == "Story"
    if kept:
        assert payload["author"] == "Jane Example"
    else:
        assert "author" not in payload
        assert not any(b"Jane Example" in p.read_bytes() for p in lake.rglob("*.parquet"))


def test_snapshot_tables_are_replaced_on_every_export(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path
) -> None:
    with engine.begin() as conn:
        first = open_incident(conn, source_id=SHOP.id, kind="drift", summary="first")
    exporter.export()
    with engine.begin() as conn:
        conn.execute(
            update(t.incident)
            .where(t.incident.c.id == first)
            .values(status="closed", closed_at=func.now())
        )
        open_incident(conn, source_id=NEWS.id, kind="blocked", summary="second")
        conn.execute(
            update(t.site)
            .where(t.site.c.host == "shop.example")
            .values(category="ecommerce", category_method="rule", category_confidence=0.9)
        )

    exporter.export()

    assert [p for p in lake_files(lake) if p.startswith("snapshots/")] == SNAPSHOTS
    incidents = {row["summary"]: row["status"] for row in read(lake, "incidents")}
    assert incidents == {"first": "closed", "second": "open"}
    sites = {row["host"]: row["category"] for row in read(lake, "sites")}
    assert sites == {"shop.example": "ecommerce", "news.example": "unclassified"}
    assert not list(lake.rglob("*.tmp"))


def test_sources_without_a_stored_cadence_get_the_default(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE sources SET config = config - 'cadence' WHERE id = :id"), {"id": NEWS.id}
        )
    exporter.export(tables=["sources"])
    cadence = {row["id"]: row["cadence"] for row in read(lake, "sources")}
    assert cadence == {SHOP.id: "hourly", NEWS.id: "daily"}


def test_an_unknown_dataset_fails_the_export_until_it_is_registered(
    engine: Engine, lake: Path
) -> None:
    gadget = DatasetType(
        name="gadget",
        description="test",
        fields={"sku": FieldDef(type="text", key=True), "owner": FieldDef(type="text", pii=True)},
    )
    item = gadget.record_model().model_validate({"sku": "g-1", "owner": "Jane Example"})
    load(engine, new_run(engine), gadget, (item, DAY))

    with pytest.raises(DatasetError, match="gadget"):
        ParquetLakeExporter(engine, lake, default_registry()).export()
    assert lake_files(lake) == [] and not (lake / STATE_PATH).exists()

    registry = DatasetRegistry([*default_registry(), gadget])
    report = ParquetLakeExporter(engine, lake, registry).export(tables=["record_versions"])
    assert report.rows["record_versions"] == 1
    (row,) = read(lake, "record_versions")
    assert json.loads(row["payload"]) == {"sku": "g-1"}


def test_a_failed_export_publishes_nothing(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(engine)

    def unreadable(self: ParquetLakeExporter) -> list[dict[str, Any]]:
        raise DatasetError("catalog unreadable")

    # dataset_types is exported last, after every other table has staged its files.
    monkeypatch.setattr(ParquetLakeExporter, "_dataset_types", unreadable)
    with pytest.raises(DatasetError, match="catalog unreadable"):
        exporter.export()
    assert lake_files(lake) == [] and not (lake / STATE_PATH).exists()


def test_an_export_that_crashes_before_saving_its_state_is_retried_without_loss(
    engine: Engine, exporter: ParquetLakeExporter, lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(engine)

    def crash(*args: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr(export_module, "save_state", crash)
        with pytest.raises(OSError, match="disk full"):
            exporter.export()
    assert not (lake / STATE_PATH).exists()

    exporter.export()

    # The retry writes the same rows again; readers dedupe on the primary key.
    for name in APPEND:
        key = TABLES[name].columns[0].name
        rows = read(lake, name, key)
        assert len(rows) == 2 * len({row[key] for row in rows}) > 0, name
    assert exporter.export().rows["record_versions"] == 0


def test_the_cli_exports_selected_tables_and_reports_status(
    engine: Engine, lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(engine)
    monkeypatch.setattr(db, "get_engine", lambda url=None: engine)
    monkeypatch.setattr(
        settings_module, "get_settings", lambda: Settings(_env_file=None, lake_dir=lake)
    )

    result = CliRunner().invoke(app, ["lake", "export", "--table", "runs", "--table", "incidents"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["rows"] == {"runs": 1, "incidents": 1}
    assert sorted(report["files"]) == lake_files(lake)
    status = CliRunner().invoke(app, ["lake", "status"])
    assert status.exit_code == 0, status.output
    tables = json.loads(status.stdout)["tables"]
    assert tables["runs"]["files"] == tables["runs"]["rows"] == 1
    assert tables["runs"]["last_export"]["export_id"] == report["export_id"]
    assert tables["runs"]["watermark"] is not None and tables["runs"]["recent_keys"] == 1
    assert tables["incidents"]["files"] == tables["incidents"]["rows"] == 1
    assert tables["record_versions"] == {"mode": "append", "files": 0, "rows": 0}
