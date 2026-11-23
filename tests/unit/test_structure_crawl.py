"""The crawl stage: the fetch loop, fetch-log rows and reading raw back."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

import httpx
import pytest
from selectolax.lexbor import LexborHTMLParser

from scrapeline.adapters.base import BaseAdapter, Window
from scrapeline.adapters.rawdoc import ParseResult, RawDoc, Target
from scrapeline.catalog.datasets import resolve_model
from scrapeline.errors import HTTPStatusError
from scrapeline.fetch.client import FetchClient, FetchEvent
from scrapeline.pipeline.crawl import (
    Crawler,
    failed_fetches,
    fetch_client,
    fetch_log_rows,
    raw_doc_from_log,
)
from scrapeline.pipeline.finalise import RunSummary
from scrapeline.pipeline.ingest import Collected
from scrapeline.registry.config import SourceConfig
from scrapeline.settings import Settings
from scrapeline.store.raw import FilesystemRawStore
from tests.conftest import public_resolver

RUN_ID = uuid.UUID(int=7)
AT = datetime(2026, 11, 24, 9, 30, tzinfo=UTC)

SITE = SourceConfig.model_validate(
    {
        "id": "linked-pages",
        "name": "Linked pages",
        "platform": "custom",
        "kind": "html",
        "entry": "https://pages.example/a",
        "dataset": "product",
        "legal": {"terms_checked": "2026-11-24", "notes": "Served by httpx.MockTransport."},
        "adapter": "tests.unit.test_structure_crawl:LinkAdapter",
        "politeness": {"rps": 5, "respect_robots": False},
    }
)

# Page path -> the paths it links to. /a links back to itself.
PAGES = {
    "/a": ["/b", "/a", "/c"],
    "/b": ["/c"],
    "/c": [],
}


class LinkAdapter(BaseAdapter):
    """One product per page (its URL), plus a target per link."""

    def parse(self, raw: RawDoc) -> ParseResult:
        result = ParseResult()
        result.add(resolve_model("product"), {"url": raw.final_url, "title": "Page"})
        for link in LexborHTMLParser(raw.text).css("a"):
            href = link.attributes.get("href") or ""
            result.next_targets.append(Target(url=urljoin(raw.final_url, href)))
        return result


def serve(pages: dict[str, list[str]], requested: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path not in pages:
            return httpx.Response(404)
        links = "".join(f'<a href="{href}">x</a>' for href in pages[request.url.path])
        return httpx.Response(200, html=f"<html><body>{links}</body></html>")

    return httpx.MockTransport(handler)


@dataclass
class Crawl:
    crawler: Crawler
    client: FetchClient
    events: list[FetchEvent]
    requested: list[str]
    raw_store: FilesystemRawStore
    summary: RunSummary
    collected: Collected

    def run(self) -> None:
        with self.client:
            try:
                self.crawler.crawl(Window(), self.summary, self.collected)
            finally:
                self.crawler.close()


def start(tmp_path: Path, pages: dict[str, list[str]], *, max_pages: int = 50) -> Crawl:
    cfg = SITE.model_copy(update={"max_pages_per_run": max_pages})
    requested: list[str] = []
    events: list[FetchEvent] = []
    client = fetch_client(
        Settings(_env_file=None, raw_store_dir=tmp_path / "raw"),
        cfg,
        events,
        transport=serve(pages, requested),
        resolver=public_resolver,
        sleep=lambda s: None,
    )
    raw_store = FilesystemRawStore(tmp_path / "raw")
    crawler = Crawler(
        cfg, LinkAdapter(cfg), client, raw_store, events, screenshot_dir=tmp_path / "shots"
    )
    summary = RunSummary(run_id=RUN_ID, source_id=cfg.id, kind="incremental")
    return Crawl(crawler, client, events, requested, raw_store, summary, Collected())


def test_crawl_fetches_each_linked_page_once_and_ties_records_to_their_raw(
    tmp_path: Path,
) -> None:
    crawl = start(tmp_path, PAGES)
    crawl.run()
    assert crawl.requested == ["/a", "/b", "/c"]
    assert (crawl.summary.pages, crawl.summary.truncated) == (3, False)
    assert crawl.events == []  # every attempt became a fetch-log row
    rows = crawl.collected.fetch_rows
    stored = {row["url"]: row for row in rows if row["raw_key"]}
    assert sorted(stored) == [f"https://pages.example/{p}" for p in ("a", "b", "c")]
    assert all(row["run_id"] == RUN_ID and row["source_id"] == SITE.id for row in rows)
    assert len(crawl.collected.items) == 3
    for record, prov in crawl.collected.items:
        row = stored[record.external_id]
        assert (prov.raw_sha256, prov.fetched_at) == (row["raw_sha256"], row["fetched_at"])
        assert RawDoc.hash_bytes(crawl.raw_store.get(row["raw_key"])) == row["raw_sha256"]
    assert failed_fetches(rows) == 0


def test_crawl_stops_at_the_page_limit(tmp_path: Path) -> None:
    crawl = start(tmp_path, PAGES, max_pages=2)
    crawl.run()
    assert crawl.requested == ["/a", "/b"]
    assert (crawl.summary.pages, crawl.summary.truncated) == (2, True)
    assert len(crawl.collected.items) == 2


def test_a_failed_crawl_still_hands_over_its_fetch_attempts(tmp_path: Path) -> None:
    crawl = start(tmp_path, PAGES | {"/b": ["/broken"]})
    with pytest.raises(HTTPStatusError):
        crawl.run()
    assert crawl.events == []
    assert crawl.summary.pages == 3
    broken = [r for r in crawl.collected.fetch_rows if r["url"].endswith("/broken")]
    assert broken and all(r["status"] == 404 and r["raw_key"] is None for r in broken)
    assert failed_fetches(crawl.collected.fetch_rows) == 1


def event(url: str, status: int | None, attempt: int, error: str | None = None) -> FetchEvent:
    return FetchEvent(url, status, 12, 0 if status is None else 512, attempt, AT, error)


def test_fetch_log_rows_put_the_raw_on_the_last_attempt_only() -> None:
    raw = RawDoc(
        source_id="s",
        url="https://s.example/p",
        final_url="https://s.example/p?page=1",
        status=200,
        content_type="text/html",
        content=b"<html></html>",
        sha256=RawDoc.hash_bytes(b"<html></html>"),
        fetched_at=datetime(2026, 11, 24, 9, 31, tzinfo=UTC),
        kind="listing",
        meta={"page": 1},
    )
    events = [event(raw.url, 503, 1), event(raw.url, 200, 2)]
    retry, success = fetch_log_rows("s", RUN_ID, events, success=(raw, "s/key.html"))
    assert (retry["status"], retry["attempt"], retry["fetched_at"]) == (503, 1, AT)
    assert retry["raw_key"] is None and retry["meta"] == {}
    assert (success["raw_key"], success["raw_sha256"]) == ("s/key.html", raw.sha256)
    assert (success["final_url"], success["kind"], success["meta"]) == (
        raw.final_url,
        "listing",
        {"page": 1},
    )
    assert success["fetched_at"] == raw.fetched_at
    # One executemany insert takes its columns from the first row, so all rows share them.
    assert retry.keys() == success.keys()


def test_fetch_log_rows_without_a_stored_raw_leave_the_raw_columns_empty() -> None:
    rows = fetch_log_rows("s", RUN_ID, [event("https://s.example/x", None, 1, "ConnectError()")])
    assert len(rows) == 1 and rows[0]["error"] == "ConnectError()"
    raw_columns = ("final_url", "content_type", "kind", "raw_sha256", "raw_key")
    assert all(rows[0][column] is None for column in raw_columns)
    assert fetch_log_rows("s", RUN_ID, []) == []


def test_raw_doc_from_log_reads_the_stored_bytes_back(tmp_path: Path) -> None:
    store = FilesystemRawStore(tmp_path)
    ref = store.put("s", b"<p>hi</p>", "html", AT)
    row = SimpleNamespace(
        url="https://s.example/p",
        final_url=None,
        status=200,
        content_type="text/html",
        raw_key=ref.key,
        raw_sha256=ref.sha256,
        fetched_at=AT,
        kind=None,
        meta=None,
    )
    raw = raw_doc_from_log("s", row, store)  # type: ignore[arg-type]
    assert (raw.content, raw.sha256, raw.fetched_at) == (b"<p>hi</p>", ref.sha256, AT)
    # A row without redirect, kind or meta reads back with their defaults.
    assert (raw.final_url, raw.kind, raw.meta) == ("https://s.example/p", "page", {})


def test_fetch_client_takes_politeness_from_the_source_and_reports_attempts(
    tmp_path: Path,
) -> None:
    settings = Settings(_env_file=None, raw_store_dir=tmp_path, http_max_retries=2)
    events: list[FetchEvent] = []
    with fetch_client(
        settings,
        SITE,
        events,
        transport=serve(PAGES, []),
        resolver=public_resolver,
        sleep=lambda s: None,
    ) as client:
        assert client.user_agent == settings.crawler_user_agent
        assert client.max_retries == 2
        assert client.respect_robots is False and client.robots is None
        assert client.policy.check("https://pages.example/a") == "https://pages.example/a"
        client.get("https://pages.example/a")
    assert [(e.url, e.status) for e in events] == [("https://pages.example/a", 200)]
