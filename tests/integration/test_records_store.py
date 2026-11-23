"""PostgresRecordStore: current rows, append-only history, removals and personal data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest
from sqlalchemy import Engine, Text, cast, func, select

from scrapeline.catalog.datasets import DatasetType, FieldDef, default_registry, resolve_model
from scrapeline.errors import DatasetError
from scrapeline.ops import runs
from scrapeline.pipeline import records as records_module
from scrapeline.pipeline.identity import content_hash
from scrapeline.pipeline.load import LoadContext, Provenance
from scrapeline.pipeline.models import Record
from scrapeline.pipeline.records import PostgresRecordStore, RecordLoadStats
from scrapeline.registry.config import SourceConfig, UsageRights
from scrapeline.store import tables as t

pytestmark = pytest.mark.integration

SOURCE = SourceConfig.model_validate(
    {
        "id": "shop-store",
        "name": "Store test shop",
        "platform": "custom",
        "kind": "html",
        "entry": "https://shop.example/products",
        "dataset": "product",
        "legal": {"terms_checked": "2026-11-24", "notes": "test"},
        "adapter": "tests.integration.test_records_store:none",
    }
)
PRODUCT = default_registry().get("product")
ARTICLE = default_registry().get("article")
STORE = PostgresRecordStore()
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def product(slug: str, price: str = "10.00", **extra: Any) -> Record:
    return resolve_model("product").model_validate(
        {
            "url": f"https://shop.example/p/{slug}",
            "title": slug.title(),
            "price": {"amount": price, "currency": "GBP"},
            **extra,
        }
    )


def url(slug: str) -> str:
    return f"https://shop.example/p/{slug}"


@pytest.fixture
def engine(clean_db: Engine) -> Engine:
    with clean_db.begin() as conn:
        runs.sync_source(conn, SOURCE)
    return clean_db


def new_run(
    engine: Engine,
    *,
    started_at: datetime | None = None,
    usage: UsageRights | None = None,
) -> LoadContext:
    """A run row plus the load context the runner would build for it."""
    with engine.begin() as conn:
        version_id = runs.scraper_version(conn, SOURCE.id, SOURCE.adapter, "code", "human")
        run_id = runs.start_run(
            conn,
            source_id=SOURCE.id,
            kind="incremental",
            scraper_version_id=version_id,
            since=None,
            until=None,
        )
        db_started = conn.execute(select(t.run.c.started_at).where(t.run.c.id == run_id)).scalar()
    return LoadContext(
        SOURCE.id,
        None,
        run_id,
        version_id,
        run_started_at=started_at or db_started,
        usage=usage or UsageRights(),
    )


def load(
    engine: Engine,
    ctx: LoadContext,
    *items: tuple[Record, datetime],
    dataset: DatasetType = PRODUCT,
) -> RecordLoadStats:
    with engine.begin() as conn:
        return STORE.load(
            conn,
            ctx,
            dataset,
            [(rec, Provenance(f"sha-{i}", when)) for i, (rec, when) in enumerate(items)],
        )


def remove_missing(engine: Engine, ctx: LoadContext, *seen: str) -> int:
    with engine.begin() as conn:
        return STORE.mark_removed(conn, ctx, PRODUCT, {url(s) for s in seen})


def row(engine: Engine, external_id: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(select(t.records).where(t.records.c.external_id == external_id)).one()


def versions(engine: Engine, external_id: str) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(
                    t.record_versions,
                    t.record_versions.c.payload.is_(None).label("payload_is_null"),
                )
                .join(t.records, t.records.c.id == t.record_versions.c.record_id)
                .where(t.records.c.external_id == external_id)
                .order_by(t.record_versions.c.version)
            )
        )


def all_versions(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(select(t.record_versions).order_by("id"))]


def test_new_identities_get_a_row_with_lineage_and_an_insert_version(engine: Engine) -> None:
    ctx = new_run(engine)
    stats = load(engine, ctx, (product("a"), at(1)), (product("b"), at(1)))
    assert stats.as_dict() == {
        "inserted": 2,
        "updated": 0,
        "unchanged": 0,
        "restored": 0,
        "removed": 0,
    }
    a = row(engine, url("a"))
    expected_payload = product("a").model_dump(
        mode="json", exclude={"external_id", "canonical_url"}
    )
    assert a.payload == expected_payload
    assert a.payload["price"] == {"amount": "10.00", "currency": "GBP"}
    assert (a.source_id, a.dataset, a.canonical_url) == (SOURCE.id, "product", url("a"))
    assert a.content_hash == content_hash(product("a"))
    assert (a.current_version, a.schema_version, a.removed_at) == (1, 1, None)
    assert a.first_seen_at == a.last_seen_at == ctx.now
    assert a.fetched_at == at(1)
    assert (a.run_id, a.scraper_version_id) == (ctx.run_id, ctx.scraper_version_id)
    assert a.raw_sha256 == "sha-0"
    (v,) = versions(engine, url("a"))
    assert (v.version, v.change_type, v.schema_version) == (1, "insert", 1)
    assert (v.payload, v.content_hash, v.valid_from) == (expected_payload, a.content_hash, at(1))
    assert (v.run_id, v.raw_sha256) == (ctx.run_id, "sha-0")


def test_same_content_only_bumps_last_seen(engine: Engine) -> None:
    first = new_run(engine)
    load(engine, first, (product("a"), at(1)))
    second = new_run(engine)
    stats = load(engine, second, (product("a"), at(2)))
    assert (stats.unchanged, stats.inserted, stats.updated) == (1, 0, 0)
    a = row(engine, url("a"))
    assert a.last_seen_at == second.now and a.first_seen_at == first.now
    assert (a.run_id, a.fetched_at, a.current_version) == (first.run_id, at(1), 1)
    assert len(versions(engine, url("a"))) == 1


def test_changed_content_updates_the_row_and_adds_a_version(engine: Engine) -> None:
    first = new_run(engine)
    load(engine, first, (product("a"), at(1)))
    second = new_run(engine)
    stats = load(engine, second, (product("a", price="12.50"), at(2)))
    assert stats.updated == 1
    a = row(engine, url("a"))
    assert a.payload["price"]["amount"] == "12.50" and a.current_version == 2
    assert a.first_seen_at == first.now and a.run_id == second.run_id
    v1, v2 = versions(engine, url("a"))
    assert (v1.change_type, v1.payload["price"]["amount"], v1.valid_from) == (
        "insert",
        "10.00",
        at(1),
    )
    assert (v2.version, v2.change_type, v2.valid_from) == (2, "update", at(2))
    assert v2.content_hash == a.content_hash != v1.content_hash


def test_latest_fetch_of_an_identity_wins_within_a_run(engine: Engine) -> None:
    ctx = new_run(engine)
    stats = load(engine, ctx, (product("a", price="11.00"), at(2)), (product("a"), at(1)))
    assert stats.inserted == 1
    assert row(engine, url("a")).payload["price"]["amount"] == "11.00"
    assert [v.valid_from for v in versions(engine, url("a"))] == [at(2)]


def test_records_missing_from_a_complete_run_are_marked_removed(engine: Engine) -> None:
    load(engine, new_run(engine), (product("a"), at(1)), (product("b"), at(1)))
    second = new_run(engine)
    load(engine, second, (product("a"), at(2)))
    assert remove_missing(engine, second, "a") == 1
    b = row(engine, url("b"))
    assert b.removed_at == second.run_started_at and b.current_version == 2
    assert b.payload["title"] == "B"  # the row keeps the last known content
    _, removed = versions(engine, url("b"))
    assert (removed.version, removed.change_type, removed.content_hash) == (2, "removed", None)
    assert removed.payload_is_null and removed.valid_from == second.run_started_at
    assert removed.run_id == second.run_id and removed.raw_sha256 is None
    assert row(engine, url("a")).removed_at is None
    assert remove_missing(engine, new_run(engine), "a") == 0  # already removed


def test_a_removed_record_seen_again_is_restored(engine: Engine) -> None:
    load(engine, new_run(engine), (product("a"), at(1)), (product("b"), at(1)))
    second = new_run(engine)
    load(engine, second, (product("a"), at(2)))
    remove_missing(engine, second, "a")
    third = new_run(engine, started_at=datetime.now(UTC) + timedelta(seconds=1))
    later = datetime.now(UTC) + timedelta(minutes=1)
    stats = load(engine, third, (product("a"), later), (product("b"), later))
    assert (stats.restored, stats.unchanged) == (1, 1)
    b = row(engine, url("b"))
    assert b.removed_at is None and b.current_version == 3 and b.last_seen_at == third.now
    assert [v.change_type for v in versions(engine, url("b"))] == ["insert", "removed", "restored"]
    restored = versions(engine, url("b"))[-1]
    assert restored.payload["title"] == "B" and restored.content_hash == b.content_hash


def test_an_observation_older_than_the_removal_does_not_restore(engine: Engine) -> None:
    load(engine, new_run(engine), (product("a"), at(1)), (product("b"), at(1)))
    second = new_run(engine)
    remove_missing(engine, second, "a")
    stats = load(engine, new_run(engine), (product("b", price="1.00"), at(1)))
    assert (stats.restored, stats.updated, stats.unchanged) == (0, 0, 1)
    assert row(engine, url("b")).removed_at is not None
    assert [v.change_type for v in versions(engine, url("b"))] == ["insert", "removed"]


def test_valid_from_never_moves_backwards(engine: Engine) -> None:
    load(engine, new_run(engine), (product("a"), at(5)))
    # Reprocessing older raw with a changed parser: the change is dated no earlier than v1.
    stats = load(engine, new_run(engine), (product("a", price="9.00"), at(1)))
    assert stats.updated == 1
    # A removal detected by a run whose start is before the last version keeps the order.
    assert remove_missing(engine, new_run(engine, started_at=at(3))) == 1
    history = versions(engine, url("a"))
    assert [v.change_type for v in history] == ["insert", "update", "removed"]
    assert [v.valid_from for v in history] == [at(5), at(5), at(5)]


def test_versions_are_append_only_and_numbered_per_record(engine: Engine) -> None:
    snapshots: list[list[tuple[Any, ...]]] = []

    def step(
        ctx: LoadContext, *items: tuple[Record, datetime], seen: tuple[str, ...] | None
    ) -> None:
        load(engine, ctx, *items)
        if seen is not None:
            remove_missing(engine, ctx, *seen)
        snapshots.append(all_versions(engine))

    step(new_run(engine), (product("a"), at(1)), (product("b"), at(1)), seen=None)
    step(new_run(engine), (product("a", price="11.00"), at(2)), seen=("a",))
    now = datetime.now(UTC)
    step(new_run(engine, started_at=now), (product("b"), now + timedelta(minutes=1)), seen=None)
    step(new_run(engine), (product("b", price="3.00"), now + timedelta(minutes=2)), seen=None)

    for before, after in pairwise(snapshots):
        assert after[: len(before)] == before  # earlier rows never change
    history = versions(engine, url("b"))
    assert [v.version for v in history] == [1, 2, 3, 4]
    assert [v.change_type for v in history] == ["insert", "removed", "restored", "update"]
    valid_from = [v.valid_from for v in history]
    assert valid_from == sorted(valid_from)
    assert row(engine, url("b")).current_version == 4


def test_each_version_stores_the_dataset_version_at_write_time(engine: Engine) -> None:
    fields = {"sku": FieldDef(type="text", key=True), "price": FieldDef(type="money")}
    v1 = DatasetType(name="gadget", version=1, description="test", fields=fields)
    v2 = DatasetType(
        name="gadget",
        version=2,
        description="test",
        fields={**fields, "colour": FieldDef(type="text")},
    )

    def gadget(ds: DatasetType, price: str) -> Record:
        return ds.record_model().model_validate(
            {"sku": "g-1", "price": {"amount": price, "currency": "EUR"}}
        )

    load(engine, new_run(engine), (gadget(v1, "5.00"), at(1)), dataset=v1)
    # A version bump without a content change is not a change.
    assert load(engine, new_run(engine), (gadget(v2, "5.00"), at(2)), dataset=v2).unchanged == 1
    assert load(engine, new_run(engine), (gadget(v2, "6.00"), at(3)), dataset=v2).updated == 1
    assert [v.schema_version for v in versions(engine, "g-1")] == [1, 2]
    with pytest.raises(DatasetError, match="schema_version"):
        load(engine, new_run(engine), (gadget(v1, "7.00"), at(4)), dataset=v2)


def test_records_of_another_dataset_are_refused(engine: Engine) -> None:
    article = resolve_model("article").model_validate({"url": url("x"), "title": "News"})
    with pytest.raises(DatasetError, match="article"):
        load(engine, new_run(engine), (article, at(1)))


@pytest.mark.parametrize(
    ("usage", "kept"),
    [
        (UsageRights(), False),
        (UsageRights(allow_pii=True), False),  # permission without a purpose is not enough
        (UsageRights(purpose="Media monitoring", allow_pii=True), True),
    ],
    ids=["default", "no-purpose", "allowed"],
)
def test_personal_data_is_blanked_unless_allowed(
    engine: Engine, usage: UsageRights, kept: bool
) -> None:
    article = resolve_model("article").model_validate(
        {"url": url("story"), "title": "Story", "author": "Jane Example"}
    )
    load(engine, new_run(engine, usage=usage), (article, at(1)), dataset=ARTICLE)
    stored = row(engine, url("story"))
    (version,) = versions(engine, url("story"))
    assert (
        stored.payload["author"] == version.payload["author"] == ("Jane Example" if kept else None)
    )
    blanked = article.model_copy(update={"author": None})
    assert stored.content_hash == content_hash(article if kept else blanked)
    with engine.connect() as conn:
        leaks = conn.execute(
            select(func.count())
            .select_from(t.record_versions)
            .where(cast(t.record_versions.c.payload, Text).contains("Jane Example"))
        ).scalar_one()
    assert leaks == (1 if kept else 0)


def test_large_loads_are_chunked(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(records_module, "_CHUNK", 2)
    slugs = ["a", "b", "c", "d", "e"]
    assert load(engine, new_run(engine), *((product(s), at(1)) for s in slugs)).inserted == 5
    second = new_run(engine)
    items = [(product(s, price="20.00" if s in {"a", "b", "c"} else "10.00"), at(2)) for s in slugs]
    stats = load(engine, second, *items)
    assert (stats.updated, stats.unchanged) == (3, 2)
    assert remove_missing(engine, second, "a") == 4
    with engine.connect() as conn:
        counts = dict(
            conn.execute(
                select(t.record_versions.c.change_type, func.count()).group_by(
                    t.record_versions.c.change_type
                )
            ).all()
        )
    assert counts == {"insert": 5, "update": 3, "removed": 4}
