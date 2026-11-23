"""Contracts shared across the platform: locale, sites, instructions, events, lake layout."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scrapeline.analytics.layout import TABLES
from scrapeline.catalog.sites import (
    CATEGORY_CODES,
    SITE_CATEGORIES,
    UNCLASSIFIED,
    Classification,
    site_host,
)
from scrapeline.errors import ConfigError
from scrapeline.ops.events import Event
from scrapeline.pipeline.locale import Locale
from scrapeline.registry import load_sources
from scrapeline.registry.config import SourceConfig
from scrapeline.registry.instruction import ScrapeInstruction

MIGRATION_0005 = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/0005_catalog_generic_records_and_events.py"
)


def test_locale_defaults_are_strict_and_validated() -> None:
    assert Locale().date_order is None and Locale().tzinfo is None
    assert Locale(timezone="Europe/London").tzinfo.key == "Europe/London"
    with pytest.raises(ValidationError, match="timezone"):
        Locale(timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        Locale(currency="pounds")


def test_site_taxonomy_is_unique_and_matches_the_migration_seed() -> None:
    codes = [c.code for c in SITE_CATEGORIES]
    assert len(codes) == len(set(codes)) and UNCLASSIFIED in codes
    migration = MIGRATION_0005.read_text()
    for code in codes:
        assert f'("{code}",' in migration


def test_classification_rejects_unknown_categories_and_confidence() -> None:
    assert (
        Classification("ecommerce", 0.9, "evidence", tags=("business",)).category in CATEGORY_CODES
    )
    with pytest.raises(ValueError, match="category"):
        Classification("shopping", 0.9, "rule")
    with pytest.raises(ValueError, match="confidence"):
        Classification("ecommerce", 1.5, "rule")


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("https://www.Example.co.uk/path?q=1", "example.co.uk"),
        ("books.toscrape.com", "books.toscrape.com"),
        ("http://data.gov.uk.", "data.gov.uk"),
        ("books.toscrape.com/catalogue", "books.toscrape.com"),
        ("example.com:8080", "example.com"),
        ("https://www.Bücher.de/", "xn--bcher-kva.de"),
    ],
)
def test_site_host(value: str, host: str) -> None:
    assert site_host(value) == host


MINIMAL = """
name: Books catalogue
start_urls: [https://books.toscrape.com/]
collect: "Every book: title, price, stock status, rating, link"
usage: {purpose: Price tracking demo}
"""


def test_minimal_instruction() -> None:
    ins = ScrapeInstruction.from_yaml(MINIMAL)
    assert ins.source_id == "books-catalogue"
    assert ins.hosts == ["books.toscrape.com"]
    assert ins.needs_dataset_proposal
    assert ins.crawl.pagination == "auto" and ins.locale.date_order is None


def test_instruction_field_shorthand_and_personal_data_rule() -> None:
    ins = ScrapeInstruction.from_yaml(
        MINIMAL + "dataset: product\nfields:\n  url: {type: url, key: true}\n  price: money\n"
    )
    assert ins.fields is not None and ins.fields["price"].type == "money"
    assert not ins.needs_dataset_proposal
    with pytest.raises(ConfigError, match="personal data"):
        ScrapeInstruction.from_yaml(
            MINIMAL + "fields:\n  url: {type: url, key: true}\n  author: {type: text, pii: true}\n"
        )


def test_personal_data_in_an_existing_dataset_needs_permission() -> None:
    with pytest.raises(ConfigError, match="personal data"):
        ScrapeInstruction.from_yaml(MINIMAL + "dataset: article\n")
    narrowed = MINIMAL + "dataset: article\nfields: {url: url, title: text}\n"
    assert ScrapeInstruction.from_yaml(narrowed).existing_dataset is not None


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("dataset: product\nfields: {price: text}\n", "not a text field"),
        ("dataset: product\nfields: {colour: text}\n", "not a text field"),
        ("fields: {title: text}\n", "key: true"),
        ("dataset: listing\n", "define its fields"),
    ],
)
def test_instruction_fields_must_fit_the_dataset(extra: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        ScrapeInstruction.from_yaml(MINIMAL + extra)


@pytest.mark.parametrize("name", ["日本のニュース", "Новости", "A"])
def test_names_without_a_usable_slug_get_a_stable_hash_id(name: str) -> None:
    ins = ScrapeInstruction.from_yaml(MINIMAL.replace("Books catalogue", name))
    assert ins.source_id.startswith("src-") and len(ins.source_id) == 12
    assert (
        ins.source_id
        == ScrapeInstruction.from_yaml(MINIMAL.replace("Books catalogue", name)).source_id
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("https://books.toscrape.com/", "ftp://books.toscrape.com/"),
        ("usage:", "fields: {external_id: text}\nusage:"),
        ("usage:", "unknown_key: 1\nusage:"),
    ],
    ids=["non-http-url", "reserved-field", "unknown-key"],
)
def test_invalid_instructions_are_refused(old: str, new: str) -> None:
    with pytest.raises(ConfigError):
        ScrapeInstruction.from_yaml(MINIMAL.replace(old, new))


def test_existing_sources_still_load_and_name_their_dataset() -> None:
    sources = load_sources(Path(__file__).resolve().parents[2] / "config/sources")
    assert sources and all(cfg.dataset_name == "meeting" for cfg in sources.values())


def test_generic_sources_need_no_jurisdiction_but_civic_ones_do() -> None:
    base = {
        "id": "books",
        "name": "Books",
        "platform": "custom",
        "kind": "html",
        "entry": "https://books.toscrape.com/",
        "legal": {"terms_checked": "2026-11-24", "notes": "Practice site."},
        "adapter": "scrapeline.adapters.declarative:DeclarativeAdapter",
    }
    assert SourceConfig.model_validate({**base, "dataset": "product"}).dataset_name == "product"
    with pytest.raises(ValidationError, match="jurisdiction"):
        SourceConfig.model_validate(base)


def test_event_kinds_are_dotted() -> None:
    assert Event(kind="run.started").level == "info"
    with pytest.raises(ValidationError):
        Event(kind="started")
    with pytest.raises(ValidationError):
        Event(kind="run.started", source_id="x" * 101)


def test_lake_layout_paths_are_consistent() -> None:
    for table in TABLES.values():
        names = [c.name for c in table.columns]
        assert len(names) == len(set(names)), table.name
        if table.mode == "append":
            assert table.path.startswith(f"{table.name}/") and "{export_id}" in table.path
            assert all(f"{p}={{{p}}}" in table.path for p in table.partition_by)
        else:
            assert table.path.startswith("snapshots/") and not table.partition_by
