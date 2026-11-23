"""Quality checks follow the source's dataset type."""

from __future__ import annotations

from datetime import date
from typing import Any

from scrapeline.adapters.rawdoc import Reject
from scrapeline.catalog.datasets import default_registry, resolve_model
from scrapeline.pipeline.models import Meeting, Record
from scrapeline.quality.checks import null_rate_limits, run_checks, worst
from scrapeline.registry.config import QualityConfig

PRODUCT = default_registry().get("product")


def product(slug: str, **extra: Any) -> Record:
    return resolve_model("product").model_validate(
        {"url": f"https://shop.example/p/{slug}", "title": slug.title(), **extra}
    )


def by_name(results: list[Any]) -> dict[str, Any]:
    return {r.name: r for r in results}


def test_meeting_null_rates_do_not_apply_to_other_datasets() -> None:
    results = by_name(
        run_checks([product("a"), product("b")], [], QualityConfig(), dataset=PRODUCT)
    )
    assert set(results) == {
        "record_count",
        "null_rate.title",
        "null_rate.url",
        "duplicate_rate",
        "reject_rate",
    }
    assert results["null_rate.url"].threshold == "<= 0.0"
    assert worst(list(results.values())) == "pass"


def test_a_source_null_rate_map_is_checked_for_fields_the_dataset_has() -> None:
    cfg = QualityConfig(max_null_rate={"price": 0.0, "location": 0.0})
    results = by_name(run_checks([product("a")], [], cfg, dataset="product"))
    assert results["null_rate.price"].status == "fail" and results["null_rate.price"].value == 1.0
    assert "null_rate.location" not in results  # not a product field
    assert null_rate_limits(cfg, PRODUCT) == {"price": 0.0, "location": 0.0}


def test_meeting_checks_are_unchanged() -> None:
    meetings = [
        Meeting(external_id=str(i), title="Council", meeting_date=date(2026, 1, 1))
        for i in range(3)
    ]
    results = by_name(run_checks(meetings, [], QualityConfig()))
    assert {"null_rate.title", "null_rate.meeting_date", "null_rate.location"} <= set(results)
    assert results["null_rate.location"].status == "warn"
    assert results["record_count"].value == 3.0


def test_only_the_datasets_records_and_rejects_are_counted() -> None:
    meeting = Meeting(external_id="m", title="Council", meeting_date=date(2026, 1, 1))
    rejects = [
        Reject(record_type="meeting", data={}, reason="other dataset"),
        Reject(record_type="product", data={}, reason="title: missing"),
    ]
    cfg = QualityConfig(max_reject_rate=0.5)
    results = by_name(run_checks([product("a"), meeting], rejects, cfg, dataset=PRODUCT))
    assert results["record_count"].value == 1.0
    assert results["reject_rate"].details["rejects"] == 1
    assert results["reject_rate"].value == 0.5
