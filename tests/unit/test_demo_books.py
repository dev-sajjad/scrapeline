"""books.toscrape.com: frozen real catalogue pages parse offline to the golden rows."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scrapeline.adapters.base import load_adapter
from scrapeline.adapters.rawdoc import ParseResult, RawDoc
from scrapeline.catalog.datasets import default_registry
from scrapeline.pipeline.values import Money
from scrapeline.quality.golden import compare, load_golden
from scrapeline.registry.config import UNREVIEWED_TERMS, SourceConfig, load_source_file

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = ROOT / "tests" / "snapshots" / "toscrape"
GOLDEN = ROOT / "tests" / "fixtures" / "golden" / "books-toscrape.json"
MANIFEST: dict[str, Any] = json.loads((SNAPSHOT_DIR / "manifest.json").read_text())
PAGES: list[dict[str, str]] = MANIFEST["pages"]


@pytest.fixture(scope="module")
def source() -> SourceConfig:
    return load_source_file(ROOT / "config" / "sources" / "books-toscrape.yaml")


def snapshot(page: dict[str, str]) -> RawDoc:
    content = (SNAPSHOT_DIR / page["file"]).read_bytes()
    return RawDoc(
        source_id="books-toscrape",
        url=page["url"],
        final_url=page["url"],
        status=200,
        content_type="text/html; charset=utf-8",
        content=content,
        sha256=RawDoc.hash_bytes(content),
        fetched_at=datetime.fromisoformat(MANIFEST["captured_at"]),
    )


@pytest.fixture(scope="module")
def parsed(source: SourceConfig) -> list[ParseResult]:
    adapter = load_adapter(source)
    return [adapter.parse(snapshot(page)) for page in PAGES]


def test_snapshots_are_the_frozen_captures() -> None:
    for page in PAGES:
        content = (SNAPSHOT_DIR / page["file"]).read_bytes()
        assert RawDoc.hash_bytes(content) == page["sha256"], page["file"]


def test_snapshots_parse_to_the_golden_rows(parsed: list[ParseResult]) -> None:
    records = [rec for result in parsed for rec in result.records]
    assert [r for result in parsed for r in result.rejects] == []
    report = compare(records, load_golden(GOLDEN, base_url=""))
    assert report.passed, report.as_dict()
    assert (report.expected, report.actual, report.matched) == (60, 60, 60)
    assert report.field_accuracy == 1.0


def test_one_record_reads_every_field_as_its_type(parsed: list[ParseResult]) -> None:
    attic = parsed[0].records[0]
    assert attic.record_type == "product"
    assert attic.title == "A Light in the Attic"  # the link text is truncated, the title is not
    assert attic.price == Money(amount=Decimal("51.77"), currency="GBP")
    assert (attic.in_stock, attic.availability, attic.rating) == (True, "In stock", 3.0)
    assert attic.external_id == attic.canonical_url == attic.url
    # Page 2 links are relative to /catalogue/; they resolve to the same URL shape as page 1.
    assert all(r.url.startswith("https://books.toscrape.com/catalogue/") for r in parsed[1].records)
    assert all(
        r.image_url.startswith("https://books.toscrape.com/media/cache/") for r in parsed[1].records
    )


def test_each_page_links_to_the_next(parsed: list[ParseResult]) -> None:
    following = [[t.url for t in result.next_targets] for result in parsed]
    assert following == [
        ["https://books.toscrape.com/catalogue/page-2.html"],
        ["https://books.toscrape.com/catalogue/page-3.html"],
        ["https://books.toscrape.com/catalogue/page-4.html"],
    ]


def test_source_is_a_reviewed_draft_under_a_british_locale(source: SourceConfig) -> None:
    assert source.status == "draft" and source.dataset_name == "product"
    assert source.legal.terms_checked != UNREVIEWED_TERMS and "robots.txt" in source.legal.notes
    assert (source.locale.language, source.locale.timezone, source.locale.currency) == (
        "en-GB",
        "Europe/London",
        "GBP",
    )
    assert source.hosts == ["books.toscrape.com"] and source.politeness.rps <= 1
    # 50 catalogue pages must fit in one run, or removals could never be detected.
    assert source.pagination == "next-link" and source.max_pages_per_run >= 50
    fields = set(default_registry().get("product").fields)
    assert set(source.quality.max_null_rate) <= fields
