"""Declarative engine on dataset types: transforms, pattern, map, locale and rejects."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scrapeline.adapters.declarative import DeclarativeAdapter, SiteSpec, parse_html
from scrapeline.adapters.platforms.legistar import LegistarAdapter
from scrapeline.adapters.rawdoc import RawDoc
from scrapeline.agents import sandbox_harness
from scrapeline.agents.tester import evaluate_spec
from scrapeline.catalog.datasets import DatasetType, FieldDef
from scrapeline.errors import ConfigError, ParseError
from scrapeline.pipeline.locale import Locale
from scrapeline.pipeline.models import Meeting
from scrapeline.pipeline.values import Money
from scrapeline.registry.config import SourceConfig
from scrapeline.settings import Settings

BASE = "https://shop.example/catalogue/"
UK = Locale(language="en-GB", timezone="Europe/London", date_order="dmy", currency="GBP")
DE = Locale(
    language="de-DE",
    timezone="Europe/Berlin",
    date_order="dmy",
    currency="EUR",
    decimal_separator=",",
)

PRODUCT_PAGE = """
<html><body><ol class="row">
<li><article class="product_pod">
  <h3><a href="a-light-in-the-attic_1000/index.html" title="A Light in the Attic">A...</a></h3>
  <p class="star-rating Three"></p>
  <p class="price_color">£51.77</p>
  <p class="availability">
     In stock (22 available)
  </p>
  <span class="sku">SKU: <b>ATT-1000</b></span>
  <span class="reviews">1,204 reviews</span>
</article></li>
<li><article class="product_pod">
  <h3><a href="tipping-the-velvet_999/index.html" title="Tipping the Velvet">Tipping...</a></h3>
  <p class="star-rating One"></p>
  <p class="price_color">£53.74</p><del class="was">£60.00</del>
  <p class="availability">Out of stock</p>
  <span class="sku">SKU: <b>VEL-999</b></span>
</article></li>
<li><article class="product_pod">
  <h3><a href="soumission_998/index.html" title="Soumission">Soumission</a></h3>
  <p class="star-rating Five"></p>
  <p class="price_color">$50.10</p>
  <p class="availability">In stock</p>
</article></li>
</ol><ul class="pager"><li class="next"><a href="page-2.html">next</a></li></ul></body></html>
"""

PRODUCT_SPEC = r"""
schema: product
record: "article.product_pod"
fields:
  url:            {css: "h3 a", attr: href}
  title:          {css: "h3 a", attr: title}
  price:          {css: p.price_color, transform: money}
  list_price:     {css: del.was, optional: true}
  in_stock:       {css: p.availability, transform: bool}
  stock_quantity: {css: p.availability, pattern: '\((\d+) available\)', transform: int,
                   optional: true}
  availability:   {css: p.availability}
  rating:         {css: p.star-rating, attr: class, pattern: 'star-rating (\w+)',
                   map: {One: 1, Two: 2, Three: 3, Four: 4, Five: 5}}
  sku:            {css: span.sku, pattern: 'SKU: (\S+)', optional: true}
  review_count:   {css: span.reviews, transform: number, optional: true, default: "0"}
next_page: {css: "li.next a", attr: href}
"""

LISTING = DatasetType(
    name="listing",
    description="A flat or house to rent (user-defined, not in the registry).",
    fields={
        "url": FieldDef(type="url", key=True),
        "rent": FieldDef(type="money", required=True),
        "available_from": FieldDef(type="date"),
        "bedrooms": FieldDef(type="int"),
        "furnished": FieldDef(type="bool"),
        "listed_at": FieldDef(type="datetime"),
        "kind": FieldDef(type="enum", values=("flat", "house")),
    },
)

LISTING_PAGE = """
<html><body>
<div class="listing"><a class="title" href="/expose/17">Altbau im Zentrum</a>
  <span class="kind">Wohnung</span><span class="rent">1.250,00 € / Monat</span>
  <span class="from">ab 01.03.2026</span><span class="rooms">3 Zimmer</span>
  <span class="furnished">möbliert</span><span class="listed">24.11.2026 18:30</span></div>
<div class="listing"><a class="title" href="/expose/18">Reihenhaus am Park</a>
  <span class="kind">Haus</span><span class="rent">2.100 €</span>
  <span class="from">ab 15.04.2026</span><span class="rooms">5 Zimmer</span>
  <span class="furnished">unmöbliert</span><span class="listed">29.03.2026 03:15</span></div>
</body></html>
"""

LISTING_SPEC = """
schema: listing
record: div.listing
fields:
  url:            {css: a.title, attr: href}
  kind:           {css: .kind, map: {"Wohnung": flat, "Haus": house}}
  rent:           {css: .rent}
  available_from: {css: .from, pattern: "ab (.+)"}
  bedrooms:       {css: .rooms}
  furnished:      {css: .furnished, map: {"möbliert": true, "unmöbliert": false}}
  listed_at:      {css: .listed}
"""


def raw(html: str, url: str = BASE, source_id: str = "t") -> RawDoc:
    content = html.encode()
    return RawDoc(
        source_id=source_id,
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        content=content,
        sha256=RawDoc.hash_bytes(content),
        fetched_at=datetime(2026, 11, 24, tzinfo=UTC),
    )


def by_title(records: list[Any]) -> dict[str, Any]:
    return {r.title: r for r in records}


def test_product_spec_reads_prices_stock_ratings_and_patterns() -> None:
    result = parse_html(SiteSpec.from_yaml(PRODUCT_SPEC), raw(PRODUCT_PAGE), locale=UK)
    books = by_title(result.records)
    attic = books["A Light in the Attic"]
    assert attic.record_type == "product"
    assert attic.external_id == attic.url == BASE + "a-light-in-the-attic_1000/index.html"
    assert attic.canonical_url == attic.url
    assert attic.price == Money(amount=Decimal("51.77"), currency="GBP")
    assert attic.list_price is None
    assert (attic.in_stock, attic.stock_quantity) == (True, 22)
    assert attic.availability == "In stock (22 available)"
    assert (attic.rating, attic.sku, attic.review_count) == (3.0, "ATT-1000", 1204)
    velvet = books["Tipping the Velvet"]
    assert velvet.list_price == Money(amount=Decimal("60.00"), currency="GBP")  # inferred money
    assert (velvet.in_stock, velvet.stock_quantity, velvet.rating) == (False, None, 1.0)
    assert velvet.review_count == 0  # default goes through the transform
    assert [t.url for t in result.next_targets] == [BASE + "page-2.html"]


def test_ambiguous_value_rejects_only_that_record_with_a_reason() -> None:
    result = parse_html(SiteSpec.from_yaml(PRODUCT_SPEC), raw(PRODUCT_PAGE), locale=UK)
    assert len(result.records) == 2
    [reject] = result.rejects
    assert reject.record_type == "product" and reject.data["title"] == "Soumission"
    assert reject.reason.startswith("ambiguous value(s): price:")
    assert "'$' can be USD" in reject.reason and "locale.currency" in reject.reason
    # The same page under a US source reads '$' as USD, but '£' stays GBP.
    us = Locale(language="en-US", date_order="mdy", currency="USD")
    prices = {
        r.title: r.price
        for r in parse_html(SiteSpec.from_yaml(PRODUCT_SPEC), raw(PRODUCT_PAGE), locale=us).records
    }
    assert prices["Soumission"].currency == "USD" and prices["Tipping the Velvet"].currency == "GBP"


def test_unreadable_and_missing_required_fields_are_told_apart() -> None:
    spec = SiteSpec.from_yaml(
        PRODUCT_SPEC.replace("transform: bool}", "transform: bool}\n  brand: {css: .brand}")
    )
    page = PRODUCT_PAGE.replace("Out of stock", "Ask in store")
    result = parse_html(spec, raw(page), locale=Locale(currency="GBP"))
    reasons = sorted(r.reason for r in result.rejects)
    assert any("not found: ['brand']" in r for r in reasons)
    assert any("unreadable: {'in_stock': 'Ask in store'}" in r for r in reasons)


def test_user_defined_dataset_end_to_end_through_parse_html() -> None:
    spec = SiteSpec.from_yaml(LISTING_SPEC, dataset=LISTING)
    result = parse_html(
        spec, raw(LISTING_PAGE, "https://flats.example/suche?page=1"), locale=DE, dataset=LISTING
    )
    assert not result.rejects
    first, second = result.records
    assert first.record_type == "listing" and first.external_id == "https://flats.example/expose/17"
    assert first.rent == Money(amount=Decimal("1250.00"), currency="EUR")
    assert (first.available_from, first.bedrooms, first.furnished, first.kind) == (
        date(2026, 3, 1),
        3,
        True,
        "flat",
    )
    assert first.listed_at == datetime(2026, 11, 24, 17, 30, tzinfo=UTC)  # CET, UTC+1
    assert second.listed_at == datetime(2026, 3, 29, 1, 15, tzinfo=UTC)  # CEST from 02:00
    assert (second.furnished, second.kind, second.rent.amount) == (False, "house", Decimal(2100))
    dumped = first.model_dump(mode="json")
    assert dumped["rent"] == {"amount": "1250.00", "currency": "EUR"}
    assert dumped["listed_at"] == "2026-11-24T17:30:00Z"


def test_spec_validation_follows_the_dataset() -> None:
    with pytest.raises(ConfigError, match="unknown schema 'listing'"):
        SiteSpec.from_yaml(LISTING_SPEC)  # not in the registry and not given
    with pytest.raises(ConfigError, match=r"fields not in listing: \['price'\]"):
        SiteSpec.from_yaml(LISTING_SPEC + "  price: {css: .rent}\n", dataset=LISTING)
    with pytest.raises(ConfigError, match="does not match dataset 'listing'"):
        SiteSpec.from_yaml(PRODUCT_SPEC, dataset=LISTING)
    with pytest.raises(ConfigError, match="fields not in product"):
        SiteSpec.from_yaml("schema: product\nrecord: li\nfields:\n  meeting_date: {css: td}\n")
    canonical = SiteSpec.from_yaml("schema: product\nrecord: li\nfields:\n  external_id: {}\n")
    assert set(canonical.fields) == {"external_id"}


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("{css: td, map: {yes: true}}", "map keys must be quoted text"),
        ("{css: td, pattern: '(unclosed'}", "invalid pattern"),
        ("{css: td, transform: currency}", "transform"),
    ],
)
def test_invalid_field_options_are_refused(field: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        SiteSpec.from_yaml(f"schema: product\nrecord: li\nfields:\n  title: {field}\n")


def test_meeting_specs_keep_the_natural_key_fallback() -> None:
    spec = SiteSpec.from_yaml(
        "schema: Meeting\nrecord: tr\nfields:\n"
        "  title: {css: td.t}\n  body_name: {css: td.b}\n  meeting_date: {css: td.d}\n"
    )
    page = "<table><tr><td class=t>Council</td><td class=b>City Council</td>"
    page += "<td class=d>03/04/2026</td></tr></table>"
    [meeting] = parse_html(spec, raw(page), locale=Locale(date_order="mdy")).records
    assert isinstance(meeting, Meeting) and meeting.external_id.startswith("nk:")
    assert meeting.meeting_date == date(2026, 3, 4)  # inferred date transform
    strict = parse_html(spec, raw(page), locale=Locale())
    assert not strict.records and "meeting_date" in strict.rejects[0].reason


def test_record_selector_still_raises_when_nothing_matches() -> None:
    spec = SiteSpec.from_yaml(PRODUCT_SPEC.replace("article.product_pod", "article.gone"))
    with pytest.raises(ParseError, match="matched 0"):
        parse_html(spec, raw(PRODUCT_PAGE), locale=UK)


def source(**changes: Any) -> SourceConfig:
    base: dict[str, Any] = {
        "id": "fixture-static-list",
        "name": "Static list",
        "platform": "fixture",
        "kind": "html",
        "entry": "http://localhost:8080/static-list/",
        "jurisdiction": {"id": "xx-fixtureville", "name": "Fixtureville", "country": "XX"},
        "legal": {"terms_checked": "2026-11-17", "notes": "fixture"},
        "adapter": "scrapeline.adapters.declarative:DeclarativeAdapter",
    }
    return SourceConfig.model_validate(base | changes)


def test_declarative_adapter_reads_with_its_source_locale() -> None:
    page = (Path(__file__).resolve().parents[1] / "sites/static-list/index.html").read_text()
    mdy = DeclarativeAdapter(source(locale={"date_order": "mdy"})).parse(raw(page))
    assert len(mdy.records) == 12 and not mdy.rejects  # 10 meetings + 2 agenda documents
    strict = DeclarativeAdapter(source()).parse(raw(page))
    [reject] = strict.rejects  # '03/01/2026' needs a date order
    assert reject.data["external_id"] == "M-1007" and "03/01/2026" in reject.reason


def test_declarative_adapter_refuses_a_spec_for_another_dataset() -> None:
    with pytest.raises(ConfigError, match="declares 'product'"):
        DeclarativeAdapter(source(dataset="product"))


def test_legistar_reads_event_dates_with_the_source_locale() -> None:
    events = [{"EventId": 1, "EventBodyName": "Council", "EventDate": "03/04/2026"}]
    content = json.dumps(events).encode()
    doc = raw("", "https://webapi.legistar.com/v1/x/events").model_copy(
        update={"content": content, "kind": "events"}
    )
    legistar = {
        "platform": "legistar",
        "kind": "api",
        "adapter": "scrapeline.adapters.platforms.legistar:LegistarAdapter",
    }
    us = LegistarAdapter(source(**legistar, locale={"date_order": "mdy"})).parse(doc)
    assert us.records[0].meeting_date == date(2026, 3, 4)
    strict = LegistarAdapter(source(**legistar)).parse(doc)
    assert not strict.records and strict.rejects[0].reason.startswith("ambiguous value(s)")


def write_pages(directory: Path, pages: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, html in pages.items():
        (directory / name).write_text(html)
    return directory


def test_local_sandbox_harness_reads_locale_and_dataset(tmp_path: Path) -> None:
    write_pages(tmp_path, {"index.html": LISTING_PAGE})
    spec_input = {
        "spec_yaml": LISTING_SPEC,
        "base_url": "https://flats.example",
        "pages": [{"file": "index.html", "url": "https://flats.example/suche"}],
        "max_pages": 3,
        "locale": DE.model_dump(mode="json"),
        "dataset": LISTING.model_dump(mode="json"),
    }
    (tmp_path / "input.json").write_text(json.dumps(spec_input))
    report = sandbox_harness.run(tmp_path)
    assert not report["errors"] and not report["rejects"] and report["pages_parsed"] == 1
    assert [r["rent"] for r in report["records"]] == [
        {"amount": "1250.00", "currency": "EUR"},
        {"amount": "2100", "currency": "EUR"},
    ]
    spec_input["locale"] = Locale().model_dump(mode="json")
    (tmp_path / "input.json").write_text(json.dumps(spec_input))
    strict = sandbox_harness.run(tmp_path)
    assert not strict["records"] and len(strict["rejects"]) == 2  # dd.mm.yyyy needs an order


def test_tester_self_consistency_on_a_product_spec(tmp_path: Path) -> None:
    pages = write_pages(
        tmp_path / "pages", {"index.html": PRODUCT_PAGE.replace("$50.10", "£50.10")}
    )
    settings = Settings(_env_file=None)
    report = evaluate_spec(
        PRODUCT_SPEC.replace("next_page", "# next_page"),
        pages,
        None,
        settings,
        mode="local",
        locale=UK,
    )
    assert report.passed, report.summary()
    assert report.records == 3 and report.golden["checks"]["no_rejects"]


def test_tester_ships_user_datasets_and_flags_personal_data(tmp_path: Path) -> None:
    pages = write_pages(tmp_path / "pages", {"index.html": LISTING_PAGE})
    settings = Settings(_env_file=None)
    report = evaluate_spec(
        LISTING_SPEC, pages, None, settings, mode="local", locale=DE, dataset=LISTING
    )
    assert report.passed, report.summary()
    assert report.golden["sample_records"][0]["listed_at"] == "2026-11-24T17:30:00Z"
    article = "schema: article\nrecord: li\nfields:\n  url: {css: a, attr: href}\n"
    flagged = evaluate_spec(article + "  author: {css: .by}\n", pages, None, settings, mode="local")
    assert not flagged.passed and "usage.allow_pii" in flagged.security[0]
