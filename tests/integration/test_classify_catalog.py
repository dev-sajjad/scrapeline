"""Site classification in the catalog: persistence, audit log, human lock, stored pages, CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import pytest
from selectolax.lexbor import LexborHTMLParser
from sqlalchemy import Connection, Engine, func, select
from typer.testing import CliRunner

from scrapeline import settings as settings_module
from scrapeline.adapters.base import BaseAdapter
from scrapeline.adapters.rawdoc import ParseResult, RawDoc
from scrapeline.catalog.classify import (
    CLASSIFIER_ACTOR,
    apply_classification,
    override_category,
    stored_pages,
)
from scrapeline.catalog.datasets import resolve_model
from scrapeline.catalog.repository import ensure_site
from scrapeline.catalog.sites import UNCLASSIFIED, Classification, Evidence
from scrapeline.cli import app
from scrapeline.errors import ConfigError
from scrapeline.ops.runs import sync_source
from scrapeline.pipeline.runner import Runner
from scrapeline.registry.config import SourceConfig
from scrapeline.settings import Settings
from scrapeline.store import db
from scrapeline.store import tables as t
from scrapeline.store.raw import FilesystemRawStore
from tests.conftest import public_resolver

pytestmark = pytest.mark.integration

SHOP_HOST = "shop.example.co.uk"
T0 = datetime(2026, 11, 20, tzinfo=UTC)


def source(source_id: str, entry: str) -> SourceConfig:
    return SourceConfig.model_validate(
        {
            "id": source_id,
            "name": source_id,
            "platform": "custom",
            "kind": "html",
            "entry": entry,
            "dataset": "product",
            "legal": {"terms_checked": "2026-11-24", "notes": "Served by httpx.MockTransport."},
            "adapter": "tests.integration.test_classify_catalog:ShopAdapter",
            "politeness": {"rps": 5},
        }
    )


SHOP = source("classify-shop", f"https://{SHOP_HOST}/products")
SHOP_PAGE = (
    '<html lang="en-GB"><head><script type="application/ld+json">'
    '{"@context": "https://schema.org", "@type": "ItemList", "itemListElement": ['
    '{"@type": "Product", "name": "Tea cup"}, {"@type": "Product", "name": "Teapot"}]}'
    "</script></head><body><ul>"
    '<li class="product"><a href="/p/cup">Tea cup</a> £9.99 <button>Add to basket</button></li>'
    '<li class="product"><a href="/p/pot">Teapot</a> £24.00 <button>Add to basket</button></li>'
    '</ul><a href="/basket">Basket</a></body></html>'
)


class ShopAdapter(BaseAdapter):
    """Test-only adapter: one product per `li.product`."""

    def parse(self, raw: RawDoc) -> ParseResult:
        result = ParseResult()
        for node in LexborHTMLParser(raw.text).css("li.product"):
            link = node.css_first("a")
            href = link.attributes.get("href") if link is not None else None
            result.add(
                resolve_model("product"),
                {"url": urljoin(raw.final_url, href or ""), "title": link and link.text()},
            )
        return result


def shop_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/products":
        return httpx.Response(200, html=SHOP_PAGE)
    return httpx.Response(404)


def classification(category: str = "ecommerce", **changes: Any) -> Classification:
    values: dict[str, Any] = {
        "confidence": 0.8,
        "method": "evidence",
        "evidence": (Evidence("schema_org.ecommerce", "Product", 0.6),),
        "tags": (),
        "platform": "shopify",
        "country": "GB",
        "language": "en-GB",
    }
    values.update(changes)
    return Classification(category, **values)


def site_row(conn: Connection, site_id: int) -> Any:
    return conn.execute(select(t.site).where(t.site.c.id == site_id)).one()


def log_rows(conn: Connection) -> list[Any]:
    return list(conn.execute(select(t.classification_log).order_by(t.classification_log.c.id)))


def test_a_first_classification_updates_the_site_logs_and_publishes(clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        site_id = ensure_site(conn, SHOP_HOST)
        outcome = apply_classification(conn, site_id, classification(), "test")
    with clean_db.connect() as conn:
        row = site_row(conn, site_id)
        (log,) = log_rows(conn)
        events = conn.execute(select(t.event).where(t.event.c.kind == "site.classified")).all()
    assert outcome == "changed"
    assert (row.category, row.category_method, row.category_confidence) == (
        "ecommerce",
        "evidence",
        0.8,
    )
    assert (row.platform, row.country, row.language) == ("shopify", "GB", "en-GB")
    assert row.category_evidence == [
        {"signal": "schema_org.ecommerce", "value": "Product", "weight": 0.6}
    ]
    assert row.classified_at is not None
    assert (log.old_category, log.new_category, log.method, log.actor) == (
        "unclassified",
        "ecommerce",
        "evidence",
        "test",
    )
    assert log.evidence[-1] == {"signal": "tags", "value": "", "weight": 0.0}
    assert len(events) == 1 and events[0].data["host"] == SHOP_HOST


def test_the_same_result_refreshes_the_site_without_a_log_row(clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        site_id = ensure_site(conn, SHOP_HOST)
        apply_classification(conn, site_id, classification(), "test")
        first = site_row(conn, site_id).classified_at
    with clean_db.begin() as conn:
        outcome = apply_classification(
            conn, site_id, classification(confidence=0.9, platform=None, country=None), "test"
        )
    with clean_db.connect() as conn:
        row = site_row(conn, site_id)
        logs = log_rows(conn)
    assert outcome == "refreshed" and len(logs) == 1
    assert row.category_confidence == 0.9 and row.classified_at > first
    # Facts this round did not see are kept, not erased.
    assert (row.platform, row.country) == ("shopify", "GB")


@pytest.mark.parametrize(
    "change",
    [
        {"category": "news_media"},
        {"tags": ("jobs",)},
        {"method": "rule"},
    ],
    ids=["category", "tags", "method"],
)
def test_category_tags_or_method_changes_are_logged(
    clean_db: Engine, change: dict[str, Any]
) -> None:
    with clean_db.begin() as conn:
        site_id = ensure_site(conn, SHOP_HOST)
        apply_classification(conn, site_id, classification(), "test")
        outcome = apply_classification(conn, site_id, classification(**change), "test")
        logs = log_rows(conn)
    assert outcome == "changed" and len(logs) == 2
    assert logs[1].old_category == "ecommerce"


def test_reordered_tags_are_not_a_change(clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        site_id = ensure_site(conn, SHOP_HOST)
        apply_classification(conn, site_id, classification(tags=("jobs", "business")), "test")
        outcome = apply_classification(
            conn, site_id, classification(tags=("business", "jobs")), "test"
        )
        assert outcome == "refreshed" and len(log_rows(conn)) == 1


def test_a_human_override_is_logged_and_never_replaced(clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        site_id = ensure_site(conn, SHOP_HOST)
        apply_classification(conn, site_id, classification(tags=("business",)), "test")
        assert (
            override_category(
                conn, f"https://www.{SHOP_HOST}/", "business", "human:ops", "Mostly B2B"
            )
            == site_id
        )
    with clean_db.begin() as conn:
        outcome = apply_classification(conn, site_id, classification("news_media"), "test")
    with clean_db.connect() as conn:
        row = site_row(conn, site_id)
        logs = log_rows(conn)
    assert outcome == "locked"
    assert (row.category, row.category_method, row.category_confidence) == (
        "business",
        "human",
        1.0,
    )
    assert row.tags == [] and row.platform == "shopify"
    assert row.category_evidence == [{"signal": "human", "value": "Mostly B2B", "weight": 1.0}]
    assert [(r.new_category, r.method, r.actor) for r in logs] == [
        ("ecommerce", "evidence", "test"),
        ("business", "human", "human:ops"),
    ]


def test_an_unclassified_result_does_not_replace_an_llm_category(clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        site_id = ensure_site(conn, SHOP_HOST)
        apply_classification(conn, site_id, classification("news_media", method="llm"), "llm")
    nothing = Classification(UNCLASSIFIED, 0.0, "none")
    with clean_db.begin() as conn:
        outcome = apply_classification(conn, site_id, nothing, "test")
    with clean_db.connect() as conn:
        row = site_row(conn, site_id)
        logs = log_rows(conn)
    assert outcome == "kept"
    assert (row.category, row.category_method) == ("news_media", "llm")
    assert [(r.new_category, r.method) for r in logs] == [("news_media", "llm")]
    # A real deterministic opinion still replaces it.
    with clean_db.begin() as conn:
        assert apply_classification(conn, site_id, classification(), "test") == "changed"
        assert site_row(conn, site_id).category_method == "evidence"


def test_every_override_is_logged_even_when_it_repeats(clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        ensure_site(conn, SHOP_HOST)
        override_category(conn, SHOP_HOST, "ecommerce", "human:a", "Sells tea")
        override_category(conn, SHOP_HOST, "ecommerce", "human:b", "Confirmed")
        assert [r.actor for r in log_rows(conn)] == ["human:a", "human:b"]


@pytest.mark.parametrize(
    ("host", "category", "note", "message"),
    [
        ("nowhere.example", "ecommerce", "why", "unknown site"),
        ("https://", "ecommerce", "why", "no host"),
        (SHOP_HOST, "shopping", "why", "unknown category"),
        (SHOP_HOST, "unclassified", "why", "unknown category"),
        (SHOP_HOST, "ecommerce", "  ", "needs a note"),
    ],
)
def test_invalid_overrides_are_refused(
    clean_db: Engine, host: str, category: str, note: str, message: str
) -> None:
    with clean_db.begin() as conn:
        ensure_site(conn, SHOP_HOST)
    with pytest.raises(ConfigError, match=message), clean_db.begin() as conn:
        override_category(conn, host, category, "human:ops", note)


def test_applying_to_an_unknown_site_is_refused(clean_db: Engine) -> None:
    with pytest.raises(ConfigError, match="unknown site id"), clean_db.begin() as conn:
        apply_classification(conn, 12345, classification(), "test")


def _log_fetch(
    conn: Connection,
    store: FilesystemRawStore,
    source_id: str,
    body: str,
    fetched_at: datetime,
    *,
    content_type: str = "text/html; charset=utf-8",
    status: int = 200,
) -> str:
    ref = store.put(source_id, body.encode(), "html" if "html" in content_type else "json", T0)
    conn.execute(
        t.fetch_log.insert().values(
            source_id=source_id,
            url=f"https://{SHOP_HOST}/{ref.sha256[:8]}",
            status=status,
            content_type=content_type,
            raw_sha256=ref.sha256,
            raw_key=ref.key,
            fetched_at=fetched_at,
        )
    )
    return ref.key


def test_stored_pages_are_the_latest_html_pages_of_each_source(
    clean_db: Engine, tmp_path: Path
) -> None:
    store = FilesystemRawStore(tmp_path / "raw")
    other = source("classify-shop-sale", f"https://{SHOP_HOST}/sale")
    with clean_db.begin() as conn:
        sync_source(conn, SHOP)
        sync_source(conn, other)
        site_id = ensure_site(conn, SHOP_HOST)
        at = [T0 + timedelta(hours=h) for h in range(6)]
        _log_fetch(conn, store, SHOP.id, "<p>oldest</p>", at[0])
        _log_fetch(conn, store, SHOP.id, "<p>newer</p>", at[1])
        _log_fetch(conn, store, SHOP.id, "<p>newer</p>", at[2])  # same bytes fetched again
        _log_fetch(conn, store, SHOP.id, "<p>newest</p>", at[3])
        _log_fetch(conn, store, SHOP.id, '{"api": true}', at[4], content_type="application/json")
        _log_fetch(conn, store, SHOP.id, "<p>denied</p>", at[4], status=403)
        missing = _log_fetch(conn, store, SHOP.id, "<p>pruned</p>", at[5])
        _log_fetch(conn, store, other.id, "<p>sale</p>", at[0])
        (store.root / missing).chmod(0o644)
        (store.root / missing).unlink()

        pages = stored_pages(conn, site_id, store, per_source=2)
    assert [(p.source_id, p.text) for p in pages] == [
        ("classify-shop", "<p>newest</p>"),
        ("classify-shop", "<p>newer</p>"),
        ("classify-shop-sale", "<p>sale</p>"),
    ]


@pytest.fixture
def cli(clean_db: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    settings = Settings(_env_file=None, raw_store_dir=tmp_path / "raw")
    monkeypatch.setattr(db, "get_engine", lambda url=None: clean_db)
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    return CliRunner()


def invoke(cli: CliRunner, *args: str) -> Any:
    result = cli.invoke(app, ["site", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_cli_classifies_a_site_from_the_pages_a_run_captured(
    cli: CliRunner, clean_db: Engine, tmp_path: Path
) -> None:
    runner = Runner(
        Settings(_env_file=None, raw_store_dir=tmp_path / "raw"),
        clean_db,
        FilesystemRawStore(tmp_path / "raw"),
        transport=httpx.MockTransport(shop_handler),
        resolver=public_resolver,
        sleep=lambda s: None,
        sources={SHOP.id: SHOP},
        adapter_factory=ShopAdapter,
    )
    assert runner.run(SHOP.id).status == "succeeded"

    first = invoke(cli, "classify", SHOP_HOST)
    assert (first["outcome"], first["category"], first["method"], first["pages"]) == (
        "changed",
        "ecommerce",
        "evidence",
        1,
    )
    assert (first["country"], first["language"]) == ("GB", "en-GB")
    assert invoke(cli, "classify", f"https://www.{SHOP_HOST}/")["outcome"] == "refreshed"

    (listed,) = invoke(cli, "list")
    assert (listed["host"], listed["category"], listed["sources"]) == (
        SHOP_HOST,
        "ecommerce",
        [SHOP.id],
    )
    with clean_db.connect() as conn:
        (log,) = log_rows(conn)
    assert log.actor == CLASSIFIER_ACTOR


def test_cli_override_locks_the_category(cli: CliRunner, clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        ensure_site(conn, "www.camden.gov.uk")
    done = invoke(cli, "override", "camden.gov.uk", "other", "--note", "Test override")
    assert done == {"site_id": 1, "host": "camden.gov.uk", "category": "other", "method": "human"}
    again = invoke(cli, "classify", "camden.gov.uk")
    assert (again["outcome"], again["category"]) == ("locked", "government")
    # The computed result is shown next to what the site row keeps.
    assert (again["stored_category"], again["stored_method"]) == ("other", "human")
    assert invoke(cli, "list", "--category", "other")[0]["method"] == "human"
    with clean_db.connect() as conn:
        assert [r.actor for r in log_rows(conn)] == ["human:cli"]


def test_cli_classify_all_skips_sites_without_pages_or_rules(
    cli: CliRunner, clean_db: Engine
) -> None:
    with clean_db.begin() as conn:
        ensure_site(conn, "camden.gov.uk")
        site_id = ensure_site(conn, "unknown.example.com")
        apply_classification(conn, site_id, classification(), "test")
    results = invoke(cli, "classify", "--all")
    assert [(r["host"], r["outcome"], r["category"]) for r in results] == [
        ("camden.gov.uk", "changed", "government"),
        ("unknown.example.com", "no_pages", "unclassified"),
    ]
    with clean_db.connect() as conn:
        assert site_row(conn, site_id).category == "ecommerce"


def test_cli_dry_run_stores_nothing(cli: CliRunner, clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        ensure_site(conn, "camden.gov.uk")
    result = invoke(cli, "classify", "camden.gov.uk", "--dry-run")
    assert (result["outcome"], result["category"]) == ("dry_run", "government")
    with clean_db.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.classification_log)).scalar() == 0
        assert conn.execute(select(t.site.c.category)).scalar_one() == "unclassified"


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["classify", "nowhere.example"], 1, "unknown site"),
        (["classify"], 2, "either a host or --all"),
        (["classify", "camden.gov.uk", "--all"], 2, "either a host or --all"),
        (["override", "camden.gov.uk", "shopping", "--note", "x"], 1, "unknown category"),
        (["override", "camden.gov.uk", "other"], 2, "--note"),
    ],
)
def test_cli_errors_are_clean(
    cli: CliRunner, clean_db: Engine, args: list[str], code: int, message: str
) -> None:
    with clean_db.begin() as conn:
        ensure_site(conn, "camden.gov.uk")
    result = cli.invoke(app, ["site", *args])
    assert result.exit_code == code
    assert message in result.output
