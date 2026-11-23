"""shop-sim fixture: generated pages are current, and each day parses to its golden rows."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scrapeline.adapters.base import load_adapter
from scrapeline.adapters.rawdoc import ParseResult, RawDoc
from scrapeline.quality.golden import compare, load_golden
from scrapeline.registry.config import SourceConfig, load_source_file

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "tests" / "sites" / "shop-sim"
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
CONFIG = ROOT / "config" / "sources" / "shop-sim.yaml"
BASE = "http://fixtures.test"
DAYS = (1, 2, 3)


def build_function() -> Callable[[Path, Path], list[Path]]:
    script = runpy.run_path(str(ROOT / "scripts" / "build_shop_sim.py"))
    build: Callable[[Path, Path], list[Path]] = script["build"]
    return build


def source_for(day: int) -> SourceConfig:
    return load_source_file(CONFIG, {"FIXTURE_BASE_URL": BASE, "SHOP_SIM_DAY": str(day)})


def parse_day(day: int) -> list[ParseResult]:
    adapter = load_adapter(source_for(day))
    results = []
    for name in ("index.html", "page-2.html"):
        content = (SITE / f"day-{day}" / name).read_bytes()
        url = f"{BASE}/shop-sim/day-{day}/" + ("" if name == "index.html" else name)
        raw = RawDoc(
            source_id="shop-sim",
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            content=content,
            sha256=RawDoc.hash_bytes(content),
            fetched_at=datetime(2026, 11, 24, tzinfo=UTC),
        )
        results.append(adapter.parse(raw))
    return results


def test_committed_fixture_matches_the_build_script(tmp_path: Path) -> None:
    written = build_function()(tmp_path / "sites", tmp_path / "golden")
    assert len(written) == 9
    for path in written:
        if path.is_relative_to(tmp_path / "sites"):
            committed = ROOT / "tests" / "sites" / path.relative_to(tmp_path / "sites")
        else:
            committed = GOLDEN / path.name
        assert path.read_bytes() == committed.read_bytes(), f"{committed} is stale; rebuild it"


@pytest.mark.parametrize("day", DAYS)
def test_each_day_parses_to_its_golden_rows(day: int) -> None:
    first, second = parse_day(day)
    assert first.rejects == second.rejects == []
    assert [t.url for t in first.next_targets] == [f"{BASE}/shop-sim/day-{day}/page-2.html"]
    assert second.next_targets == []
    records = first.records + second.records
    golden = load_golden(GOLDEN / f"shop-sim-day-{day}.json", BASE)
    report = compare(records, golden)
    assert report.passed, report.as_dict()
    assert report.field_accuracy == 1.0


def test_identities_and_urls_are_the_same_on_every_day() -> None:
    records = [r for day in DAYS for result in parse_day(day) for r in result.records]
    assert all(r.external_id == r.sku and r.canonical_url == r.url for r in records)
    urls = {day: {r.url for result in parse_day(day) for r in result.records} for day in DAYS}
    assert len(urls[1]) == 20 and urls[2] == urls[1] - {f"{BASE}/shop-sim/products/desk-lamp.html"}
    assert urls[3] == urls[1] | {f"{BASE}/shop-sim/products/bird-feeder.html"}


def test_thousands_separators_are_read_under_the_british_locale() -> None:
    chair = next(r for result in parse_day(2) for r in result.records if r.sku == "SIM-0008")
    assert str(chair.price.amount) == "1099.00" and chair.price.currency == "GBP"


def test_source_is_an_active_manual_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOP_SIM_DAY", raising=False)
    cfg = load_source_file(CONFIG, {"FIXTURE_BASE_URL": BASE})
    assert cfg.entry == f"{BASE}/shop-sim/day-1/" and cfg.options["golden"] == "shop-sim-day-1"
    assert (cfg.status, cfg.cadence, cfg.dataset_name) == ("active", "manual", "product")
    assert cfg.terms_reviewed
    assert (cfg.locale.language, cfg.locale.timezone, cfg.locale.currency) == (
        "en-GB",
        "Europe/London",
        "GBP",
    )
    assert source_for(3).entry == f"{BASE}/shop-sim/day-3/"
