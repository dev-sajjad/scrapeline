"""Runner on a generic dataset (record store with history) and on typed civic sources."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import pytest
from selectolax.lexbor import LexborHTMLParser
from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.exc import IntegrityError

from scrapeline.adapters.base import BaseAdapter
from scrapeline.adapters.rawdoc import ParseResult, RawDoc, Target
from scrapeline.catalog.datasets import DatasetType, default_registry, resolve_model
from scrapeline.errors import DatasetError
from scrapeline.ops.events import Event
from scrapeline.pipeline.load import LoadContext, Provenance
from scrapeline.pipeline.models import Record
from scrapeline.pipeline.records import PostgresRecordStore, RecordLoadStats, RecordStore
from scrapeline.pipeline.runner import Runner
from scrapeline.registry import load_sources
from scrapeline.registry.config import SourceConfig, UsageRights
from scrapeline.settings import PROJECT_ROOT, Settings
from scrapeline.store import tables as t
from scrapeline.store.raw import FilesystemRawStore
from tests.conftest import public_resolver

pytestmark = pytest.mark.integration

SHOP = SourceConfig.model_validate(
    {
        "id": "shop-test",
        "name": "Test shop",
        "platform": "custom",
        "kind": "html",
        "entry": "https://shop.example/products",
        "dataset": "product",
        "legal": {"terms_checked": "2026-11-24", "notes": "Served by httpx.MockTransport."},
        "adapter": "tests.integration.test_records_runner:ShopAdapter",
        "politeness": {"rps": 5},
    }
)


NEWS = SourceConfig.model_validate(
    {
        "id": "news-test",
        "name": "Test news",
        "platform": "custom",
        "kind": "html",
        "entry": "https://news.example/articles",
        "dataset": "article",
        "legal": {"terms_checked": "2026-11-24", "notes": "Served by httpx.MockTransport."},
        "adapter": "tests.integration.test_records_runner:NewsAdapter",
        "politeness": {"rps": 5},
    }
)

# The second article has no title, so it is rejected with its byline in the reject data.
NEWS_PAGE = (
    "<html><body>"
    '<article data-author="Jane Example"><a href="/a/1">Budget approved</a></article>'
    '<article data-author="John Private"><a href="/a/2"></a></article>'
    "{more}</body></html>"
)


class ShopAdapter(BaseAdapter):
    """Test-only adapter: one product per `li.product`, the next page via `a.next`."""

    def parse(self, raw: RawDoc) -> ParseResult:
        model = resolve_model("product")
        result = ParseResult()
        tree = LexborHTMLParser(raw.text)
        for node in tree.css("li.product"):
            link = node.css_first("a")
            price = node.attributes.get("data-price")
            result.add(
                model,
                {
                    "url": urljoin(raw.final_url, link.attributes.get("href") or ""),
                    "title": link.text(strip=True) or None,
                    "price": {"amount": price, "currency": "GBP"} if price else None,
                    "in_stock": node.attributes.get("data-stock") == "yes",
                },
            )
        nxt = tree.css_first("a.next")
        if nxt is not None:
            next_url = urljoin(raw.final_url, nxt.attributes.get("href") or "")
            result.next_targets.append(Target(url=next_url))
        return result


class ArticleLeakAdapter(ShopAdapter):
    """Emits a record of a dataset the source does not declare."""

    def parse(self, raw: RawDoc) -> ParseResult:
        result = super().parse(raw)
        article = resolve_model("article")
        result.records.append(article.model_validate({"url": raw.final_url, "title": "News"}))
        return result


class NewsAdapter(BaseAdapter):
    """Test-only adapter: one article per `article`, the byline in `data-author`."""

    def parse(self, raw: RawDoc) -> ParseResult:
        model = resolve_model("article")
        result = ParseResult()
        tree = LexborHTMLParser(raw.text)
        for node in tree.css("article"):
            link = node.css_first("a")
            result.add(
                model,
                {
                    "url": urljoin(raw.final_url, link.attributes.get("href") or ""),
                    "title": link.text(strip=True) or None,
                    "author": node.attributes.get("data-author"),
                },
            )
        nxt = tree.css_first("a.next")
        if nxt is not None:
            next_url = urljoin(raw.final_url, nxt.attributes.get("href") or "")
            result.next_targets.append(Target(url=next_url))
        return result


class StaleProductAdapter(ShopAdapter):
    """Emits product records of a schema version the registry does not have."""

    def parse(self, raw: RawDoc) -> ParseResult:
        result = super().parse(raw)
        stale = default_registry().get("product").model_copy(update={"version": 99})
        model = stale.record_model()
        result.records = [model.model_validate(r.model_dump(mode="json")) for r in result.records]
        return result


class ExplodingStore(PostgresRecordStore):
    """Writes like the real store, then fails, so the whole load must roll back."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def load(
        self,
        conn: Connection,
        ctx: LoadContext,
        dataset: DatasetType,
        items: Iterable[tuple[Record, Provenance]],
    ) -> RecordLoadStats:
        super().load(conn, ctx, dataset, items)
        raise self.error


class FakeShop:
    """A product listing, three per page, served through httpx.MockTransport. Mutable."""

    page_size = 3

    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {
            f"p{i}": {"title": f"Product {i}", "price": f"{i}.99", "stock": True}
            for i in range(1, 6)
        }
        self.override: Callable[[httpx.Request], httpx.Response | None] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.override and (resp := self.override(request)) is not None:
            return resp
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path != "/products":
            return httpx.Response(404)
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, html=self.render(page))

    def render(self, page: int) -> str:
        items = list(self.products.items())
        start = (page - 1) * self.page_size
        rows = "".join(
            f'<li class="product" data-price="{p["price"]}" '
            f'data-stock="{"yes" if p["stock"] else "no"}">'
            f'<a href="/p/{slug}">{p["title"]}</a></li>'
            for slug, p in items[start : start + self.page_size]
        )
        more = start + self.page_size < len(items)
        nxt = f'<a class="next" href="/products?page={page + 1}">Next</a>' if more else ""
        return f"<html><body><ul>{rows}</ul>{nxt}</body></html>"


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


class BrokenBus:
    def publish(self, event: Event) -> None:
        raise RuntimeError("event store down")


@pytest.fixture
def shop() -> FakeShop:
    return FakeShop()


@pytest.fixture
def make_runner(clean_db: Engine, tmp_path: Path, shop: FakeShop) -> Callable[..., Runner]:
    def make(
        cfg: SourceConfig = SHOP,
        *,
        bus: Any = None,
        adapter: type[BaseAdapter] = ShopAdapter,
        store: RecordStore | None = None,
    ) -> Runner:
        return Runner(
            Settings(_env_file=None, raw_store_dir=tmp_path / "raw"),
            clean_db,
            FilesystemRawStore(tmp_path / "raw"),
            transport=httpx.MockTransport(shop.handler),
            resolver=public_resolver,
            sleep=lambda s: None,
            sources={cfg.id: cfg},
            adapter_factory=adapter,
            event_bus=bus,
            record_store=store,
        )

    return make


def product_url(slug: str) -> str:
    return f"https://shop.example/p/{slug}"


def history(engine: Engine, slug: str) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(t.record_versions.c.change_type)
                .join(t.records, t.records.c.id == t.record_versions.c.record_id)
                .where(t.records.c.external_id == product_url(slug))
                .order_by(t.record_versions.c.version)
            ).scalars()
        )


def count(engine: Engine, table: t.Table, *where: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table).where(*where)).scalar_one())


def loaded(**changes: int) -> dict[str, int]:
    return {"inserted": 0, "updated": 0, "unchanged": 0, "restored": 0, "removed": 0} | changes


def run_status(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(select(t.run.c.status).order_by(t.run.c.started_at)).scalars())


def test_generic_run_stores_records_with_history_and_events(
    make_runner: Callable[..., Runner], clean_db: Engine
) -> None:
    summary = make_runner().run(SHOP.id)
    assert summary.status == "succeeded", summary.error
    assert summary.loaded == {"product": loaded(inserted=5)}
    assert (summary.pages, summary.complete, summary.removed) == (2, True, 0)
    assert count(clean_db, t.records, t.records.c.dataset == "product") == 5
    assert count(clean_db, t.record_versions, t.record_versions.c.change_type == "insert") == 5
    assert count(clean_db, t.meetings) == 0
    with clean_db.connect() as conn:
        row = conn.execute(
            select(t.records).where(t.records.c.external_id == product_url("p2"))
        ).one()
        dataset_types = set(conn.execute(select(t.dataset_type.c.name)).scalars())
        checks = set(conn.execute(select(t.check_result.c.check_name)).scalars())
        stats = conn.execute(select(t.run.c.stats)).scalar_one()
        events = conn.execute(select(t.event).order_by(t.event.c.id)).all()
    assert row.payload["price"] == {"amount": "2.99", "currency": "GBP"}
    assert row.raw_sha256 and row.run_id == summary.run_id and row.scraper_version_id
    assert "product" in dataset_types
    assert {"null_rate.title", "null_rate.url"} <= checks
    assert not {"null_rate.location", "null_rate.meeting_date"} & checks
    assert stats["complete"] is True and stats["removed"] == 0
    assert [e.kind for e in events] == [
        "run.started",
        "records.loaded",
        "checks.done",
        "run.finished",
    ]
    assert all(e.run_id == summary.run_id and e.source_id == SHOP.id for e in events)
    assert events[1].data == {"dataset": "product", "storage": "generic", **loaded(inserted=5)}
    assert events[2].data["verdict"] == "pass" and events[2].data["fail"] == 0


def test_rerun_without_changes_adds_no_versions(
    make_runner: Callable[..., Runner], clean_db: Engine
) -> None:
    runner = make_runner()
    runner.run(SHOP.id)
    second = runner.run(SHOP.id)
    assert second.loaded["product"] == loaded(unchanged=5)
    assert count(clean_db, t.record_versions) == 5


def test_changes_removals_and_restores_across_complete_runs(
    make_runner: Callable[..., Runner], shop: FakeShop, clean_db: Engine
) -> None:
    runner = make_runner()
    runner.run(SHOP.id)
    shop.products["p1"]["price"] = "0.49"
    gone = shop.products.pop("p5")
    second = runner.run(SHOP.id)
    assert second.loaded["product"] == loaded(updated=1, unchanged=3, removed=1)
    assert second.complete and second.removed == 1
    assert history(clean_db, "p1") == ["insert", "update"]
    assert history(clean_db, "p5") == ["insert", "removed"]
    shop.products["p5"] = gone
    third = runner.run(SHOP.id)
    assert third.loaded["product"] == loaded(unchanged=4, restored=1)
    assert history(clean_db, "p5") == ["insert", "removed", "restored"]
    assert count(clean_db, t.records, t.records.c.removed_at.is_not(None)) == 0


def _truncated(runner: Callable[..., Runner], shop: FakeShop) -> Any:
    return runner(SHOP.model_copy(update={"max_pages_per_run": 1})).run(SHOP.id)


def _with_reject(runner: Callable[..., Runner], shop: FakeShop) -> Any:
    shop.products["p1"]["title"] = ""
    cfg = SHOP.model_copy(
        update={"quality": SHOP.quality.model_copy(update={"max_reject_rate": 0.5})}
    )
    return runner(cfg).run(SHOP.id)


def _backfill(runner: Callable[..., Runner], shop: FakeShop) -> Any:
    return runner().run(SHOP.id, kind="backfill", since=datetime.now(UTC) - timedelta(days=30))


def _windowed(runner: Callable[..., Runner], shop: FakeShop) -> Any:
    return runner().run(SHOP.id, since=datetime.now(UTC) - timedelta(days=1))


def _reprocess(runner: Callable[..., Runner], shop: FakeShop) -> Any:
    return runner().reprocess(SHOP.id)


def _failed_checks(runner: Callable[..., Runner], shop: FakeShop) -> Any:
    shop.products.clear()
    return runner().run(SHOP.id)


@pytest.mark.parametrize(
    "second_run",
    [_truncated, _with_reject, _backfill, _windowed, _reprocess, _failed_checks],
    ids=["page-limit", "rejects", "backfill", "since-window", "reprocess", "checks-failed"],
)
def test_incomplete_runs_never_mark_removals(
    make_runner: Callable[..., Runner],
    shop: FakeShop,
    clean_db: Engine,
    second_run: Callable[[Callable[..., Runner], FakeShop], Any],
) -> None:
    make_runner().run(SHOP.id)
    shop.products.pop("p5")
    summary = second_run(make_runner, shop)
    assert not summary.complete and summary.removed == 0
    assert count(clean_db, t.record_versions, t.record_versions.c.change_type == "removed") == 0
    assert count(clean_db, t.records, t.records.c.removed_at.is_not(None)) == 0


def test_a_run_with_rejects_still_loads_what_it_saw(
    make_runner: Callable[..., Runner], shop: FakeShop
) -> None:
    make_runner().run(SHOP.id)
    shop.products["p2"]["price"] = "7.77"
    summary = _with_reject(make_runner, shop)
    assert summary.status == "succeeded" and summary.rejects == 1
    assert summary.loaded["product"] == loaded(updated=1, unchanged=3)


def test_a_failing_event_bus_never_fails_the_run(
    make_runner: Callable[..., Runner], clean_db: Engine
) -> None:
    summary = make_runner(bus=BrokenBus()).run(SHOP.id)
    assert summary.status == "succeeded" and summary.loaded["product"]["inserted"] == 5
    assert count(clean_db, t.event) == 0


def test_records_outside_the_sources_dataset_fail_the_run(
    make_runner: Callable[..., Runner], clean_db: Engine
) -> None:
    bus = RecordingBus()
    summary = make_runner(bus=bus, adapter=ArticleLeakAdapter).run(SHOP.id)
    assert summary.status == "failed" and "DatasetError" in (summary.error or "")
    assert count(clean_db, t.records) == 0
    assert count(clean_db, t.incident, t.incident.c.kind == "error") == 1
    assert bus.kinds() == ["run.started", "run.failed"]
    assert bus.events[-1].level == "error"


def test_records_of_another_schema_version_fail_the_run_before_loading(
    make_runner: Callable[..., Runner], clean_db: Engine
) -> None:
    bus = RecordingBus()
    summary = make_runner(bus=bus, adapter=StaleProductAdapter).run(SHOP.id)
    assert summary.status == "failed" and "schema_version [99], expected 1" in (summary.error or "")
    assert run_status(clean_db) == ["failed"]
    assert count(clean_db, t.records) == 0
    assert count(clean_db, t.fetch_log, t.fetch_log.c.raw_key.is_not(None)) == 2
    assert count(clean_db, t.incident, t.incident.c.kind == "error") == 1
    assert bus.kinds() == ["run.started", "run.failed"]


@pytest.mark.parametrize(
    "error",
    [
        DatasetError("store refused the records"),
        IntegrityError("INSERT INTO data.record_versions", {}, Exception("duplicate key")),
    ],
    ids=["dataset-error", "integrity-error"],
)
def test_a_failing_load_rolls_back_but_still_records_the_failed_run(
    make_runner: Callable[..., Runner], clean_db: Engine, error: Exception
) -> None:
    bus = RecordingBus()
    summary = make_runner(bus=bus, store=ExplodingStore(error)).run(SHOP.id)
    assert summary.status == "failed" and type(error).__name__ in (summary.error or "")
    assert summary.loaded == {} and not summary.complete and summary.watermark is None
    assert run_status(clean_db) == ["failed"]
    assert count(clean_db, t.records) == 0 and count(clean_db, t.check_result) == 0
    assert count(clean_db, t.incident, t.incident.c.kind == "error") == 1
    assert bus.kinds() == ["run.started", "run.failed"]
    # The fetch log survives the rollback, so the raw files can still be rebuilt from.
    assert count(clean_db, t.fetch_log, t.fetch_log.c.raw_key.is_not(None)) == 2
    rebuilt = make_runner().reprocess(SHOP.id)
    assert rebuilt.status == "succeeded" and rebuilt.loaded["product"] == loaded(inserted=5)


@pytest.mark.parametrize(
    ("usage", "more", "author"),
    [
        (UsageRights(), "", None),
        (UsageRights(), '<a class="next" href="/gone">Next</a>', None),
        (UsageRights(allow_pii=True, purpose="Media monitoring"), "", "John Private"),
    ],
    ids=["blanked", "blanked-on-a-failed-run", "kept-with-permission-and-purpose"],
)
def test_rejects_keep_personal_data_only_when_the_source_may(
    make_runner: Callable[..., Runner],
    shop: FakeShop,
    clean_db: Engine,
    usage: UsageRights,
    more: str,
    author: str | None,
) -> None:
    page = NEWS_PAGE.format(more=more)
    shop.override = lambda r: httpx.Response(200, html=page) if r.url.path == "/articles" else None
    summary = make_runner(NEWS.model_copy(update={"usage": usage}), adapter=NewsAdapter).run(
        NEWS.id
    )
    # With a broken next link the run fails while crawling, and `_fail` writes the rejects.
    assert ("HTTPStatusError" in (summary.error or "")) == bool(more)
    with clean_db.connect() as conn:
        rejects = list(conn.execute(select(t.rejects.c.record)).scalars())
    assert len(rejects) == 1
    assert rejects[0]["url"] == "https://news.example/a/2" and rejects[0]["title"] is None
    assert rejects[0]["author"] == author
    if author is None:
        assert "John Private" not in json.dumps(rejects)


def test_a_blocked_run_publishes_started_and_failed(
    make_runner: Callable[..., Runner], shop: FakeShop
) -> None:
    shop.override = lambda r: httpx.Response(403) if r.url.path == "/products" else None
    bus = RecordingBus()
    summary = make_runner(bus=bus).run(SHOP.id)
    assert summary.status == "failed"
    assert bus.kinds() == ["run.started", "run.failed"]
    assert bus.events[-1].data["incident_id"] == summary.incident_id


def test_failed_checks_publish_a_failed_run(
    make_runner: Callable[..., Runner], shop: FakeShop
) -> None:
    shop.products.clear()
    bus = RecordingBus()
    summary = make_runner(bus=bus).run(SHOP.id)
    assert summary.status == "failed" and summary.checks["record_count"] == "fail"
    assert bus.kinds() == ["run.started", "records.loaded", "checks.done", "run.failed"]
    assert bus.events[2].level == "error" and bus.events[2].data["failed"] == ["record_count"]


def test_reprocess_of_a_generic_source_is_idempotent(
    make_runner: Callable[..., Runner], clean_db: Engine
) -> None:
    runner = make_runner()
    runner.run(SHOP.id)
    summary = runner.reprocess(SHOP.id)
    assert summary.status == "succeeded"
    assert summary.loaded["product"] == loaded(unchanged=5)
    assert count(clean_db, t.record_versions) == 5


def test_typed_legistar_run_keeps_its_tables_and_publishes_events(
    runner: Runner, clean_db: Engine
) -> None:
    summary = runner.run("legistar-testcity")
    assert summary.status == "succeeded", summary.error
    assert summary.loaded["meeting"] == {"inserted": 25, "updated": 0, "unchanged": 0}
    assert summary.loaded["body"]["inserted"] == 83
    assert summary.removed == 0
    assert count(clean_db, t.records) == 0
    with clean_db.connect() as conn:
        stored = set(conn.execute(select(t.dataset_type.c.name)).scalars())
        events = conn.execute(select(t.event).order_by(t.event.c.id)).all()
    assert "meeting" in stored
    loads = {e.data["dataset"]: e.data for e in events if e.kind == "records.loaded"}
    assert {"meeting", "body"} <= set(loads)
    assert all(d["storage"] == "typed" for d in loads.values())
    assert (events[0].kind, events[-1].kind) == ("run.started", "run.finished")


def test_typed_fixture_site_still_loads_meetings(clean_db: Engine, tmp_path: Path) -> None:
    cfg = load_sources(PROJECT_ROOT / "config/sources")["fixture-static-list"]
    runner = Runner(
        Settings(_env_file=None, raw_store_dir=tmp_path / "raw"),
        clean_db,
        FilesystemRawStore(tmp_path / "raw"),
        sleep=lambda s: None,
        sources={cfg.id: cfg},
    )
    first = runner.run(cfg.id)
    assert first.status == "succeeded", first.error
    assert first.loaded["meeting"]["inserted"] == 25
    second = runner.run(cfg.id)
    assert second.loaded["meeting"] == {"inserted": 0, "updated": 0, "unchanged": 25}
    assert count(clean_db, t.meetings, t.meetings.c.source_id == cfg.id) == 25
    assert count(clean_db, t.records) == 0
