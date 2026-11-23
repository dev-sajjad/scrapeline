"""Pure rules behind the record store: crawl completeness, routing and personal data."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from scrapeline.adapters.rawdoc import Reject
from scrapeline.catalog.datasets import default_registry, resolve_model
from scrapeline.errors import DatasetError
from scrapeline.pipeline.load import Provenance
from scrapeline.pipeline.records import keeps_personal_data, storable
from scrapeline.pipeline.runner import (
    _failed_fetches,
    is_complete_crawl,
    split_by_storage,
    storable_reject,
)
from scrapeline.registry.config import UsageRights

COMPLETE: dict[str, Any] = {
    "windowed": False,
    "truncated": False,
    "failed_fetches": 0,
    "rejects": 0,
    "verdict": "pass",
}


def test_a_full_incremental_crawl_is_complete() -> None:
    assert is_complete_crawl("incremental", **COMPLETE)
    assert is_complete_crawl("incremental", **(COMPLETE | {"verdict": "warn"}))


@pytest.mark.parametrize(
    ("kind", "change"),
    [
        ("backfill", {}),
        ("reprocess", {}),
        ("incremental", {"windowed": True}),
        ("incremental", {"truncated": True}),
        ("incremental", {"failed_fetches": 1}),
        ("incremental", {"rejects": 1}),
        ("incremental", {"verdict": "fail"}),
    ],
    ids=["backfill", "reprocess", "windowed", "page-limit", "fetch-error", "rejects", "checks"],
)
def test_anything_less_is_not_complete(kind: str, change: dict[str, Any]) -> None:
    assert not is_complete_crawl(kind, **(COMPLETE | change))


def _row(url: str, status: int | None, *, error: str | None = None, ok: bool = False) -> dict:
    return {"url": url, "status": status, "error": error, "raw_key": "key" if ok else None}


def test_a_retried_fetch_that_succeeded_is_not_a_failure() -> None:
    rows = [
        _row("https://a/1", 503),
        _row("https://a/1", 200, ok=True),
        _row("https://a/2", 301),
        _row("https://a/3", 200, ok=True),
    ]
    assert _failed_fetches(rows) == 0
    rows += [_row("https://a/4", None, error="ConnectError()"), _row("https://a/5", 500)]
    assert _failed_fetches(rows) == 2


def test_personal_data_needs_permission_and_a_purpose() -> None:
    assert not keeps_personal_data(UsageRights())
    assert not keeps_personal_data(UsageRights(allow_pii=True, purpose="  "))
    assert keeps_personal_data(UsageRights(allow_pii=True, purpose="Research"))

    article = resolve_model("article").model_validate(
        {"url": "https://news.example/a", "title": "A", "author": "Jane Example"}
    )
    dataset = default_registry().get("article")
    assert storable(article, dataset, UsageRights()).author is None  # type: ignore[attr-defined]
    allowed = UsageRights(allow_pii=True, purpose="Research")
    assert storable(article, dataset, allowed) is article


def test_generic_records_of_another_schema_version_are_refused_before_loading() -> None:
    product = default_registry().get("product")
    prov = Provenance("0" * 64, datetime.now(UTC))
    current = product.record_model().model_validate({"url": "https://s.example/1", "title": "A"})
    stale_model = product.model_copy(update={"version": 2}).record_model()
    stale = stale_model.model_validate({"url": "https://s.example/2", "title": "B"})
    assert split_by_storage(product, [(current, prov)]) == ([(current, prov)], [])
    with pytest.raises(DatasetError, match=r"schema_version \[1, 2\], expected 1"):
        split_by_storage(product, [(current, prov), (stale, prov)])


def test_rejects_of_the_dataset_lose_personal_data_unless_allowed() -> None:
    article = default_registry().get("article")
    data = {"url": "https://news.example/a", "title": None, "author": "John Private"}
    reject = Reject(record_type="article", data=data, reason="title: missing")
    assert storable_reject(reject, article, UsageRights()) == data | {"author": None}
    assert reject.data["author"] == "John Private"  # the parse result itself is not changed
    allowed = UsageRights(allow_pii=True, purpose="Research")
    assert storable_reject(reject, article, allowed) == data
    without_byline = Reject(record_type="article", data={"title": None}, reason="title: missing")
    assert storable_reject(without_byline, article, UsageRights()) == {"title": None}
