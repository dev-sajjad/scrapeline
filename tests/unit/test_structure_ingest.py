"""The ingest stage: parsing raw documents into what a run collected."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from scrapeline.adapters.base import Window
from scrapeline.adapters.rawdoc import ParseResult, RawDoc, Reject, Target
from scrapeline.catalog.datasets import resolve_model
from scrapeline.fetch.client import FetchClient, FetchResponse
from scrapeline.pipeline.ingest import Collected, parse_raw
from scrapeline.pipeline.load import Provenance
from scrapeline.registry.config import SourceConfig

EARLY = datetime(2026, 11, 1, tzinfo=UTC)
LATE = datetime(2026, 11, 20, tzinfo=UTC)


class ScriptedAdapter:
    """Returns a prepared ParseResult per raw URL."""

    source: SourceConfig

    def __init__(self, results: dict[str, ParseResult]) -> None:
        self.results = results

    def discover(self, window: Window) -> Iterator[Target]:
        raise NotImplementedError

    def fetch(self, target: Target, client: FetchClient) -> FetchResponse:
        raise NotImplementedError

    def parse(self, raw: RawDoc) -> ParseResult:
        return self.results[raw.url]


def raw(url: str, content: bytes) -> RawDoc:
    return RawDoc(
        source_id="s",
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        content=content,
        sha256=RawDoc.hash_bytes(content),
        fetched_at=datetime(2026, 11, 24, 8, 0, tzinfo=UTC),
    )


def test_parse_raw_collects_records_and_rejects_with_their_raw() -> None:
    product = resolve_model("product")
    first = ParseResult(
        records=[product.model_validate({"url": "https://s.example/p/1", "title": "One"})],
        rejects=[Reject(record_type="product", data={"title": None}, reason="title: missing")],
        next_targets=[Target(url="https://s.example/?page=2")],
        watermark=LATE,
    )
    second = ParseResult(
        records=[product.model_validate({"url": "https://s.example/p/2", "title": "Two"})],
        watermark=EARLY,
    )
    adapter = ScriptedAdapter({"https://s.example/": first, "https://s.example/?page=2": second})
    page1, page2 = raw("https://s.example/", b"one"), raw("https://s.example/?page=2", b"two")
    collected = Collected()

    assert parse_raw(adapter, page1, collected) == [Target(url="https://s.example/?page=2")]
    assert parse_raw(adapter, page2, collected) == []

    assert [(rec.external_id, prov) for rec, prov in collected.items] == [
        ("https://s.example/p/1", Provenance(page1.sha256, page1.fetched_at)),
        ("https://s.example/p/2", Provenance(page2.sha256, page2.fetched_at)),
    ]
    assert [(rej.reason, sha) for rej, sha in collected.rejects] == [
        ("title: missing", page1.sha256)
    ]
    # An older page never moves the watermark back.
    assert collected.watermark == LATE
    assert collected.fetch_rows == []
