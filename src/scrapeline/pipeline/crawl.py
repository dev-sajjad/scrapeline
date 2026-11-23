"""Crawl: discover a source's pages, fetch or render each one, store it as raw and parse it.

Every response is stored in the raw store before it is parsed. Every HTTP attempt becomes a
raw.fetch_log row, and the attempt that produced a stored page carries its raw key, which is
how reprocessing finds the raw again.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, Row, select

from scrapeline.adapters.base import Adapter, Window
from scrapeline.adapters.rawdoc import RawDoc, Target
from scrapeline.errors import ConfigError
from scrapeline.fetch.browser import BrowserPool
from scrapeline.fetch.client import CircuitBreaker, FetchClient, FetchEvent, FetchResponse
from scrapeline.fetch.limiter import HostRateLimiter
from scrapeline.fetch.urlpolicy import Resolver, URLPolicy
from scrapeline.ops.logging import get_logger
from scrapeline.pipeline.ingest import Collected, parse_raw
from scrapeline.registry.config import SourceConfig
from scrapeline.settings import Settings
from scrapeline.store import tables as t
from scrapeline.store.raw import RawStore, ext_for

if TYPE_CHECKING:
    import httpx

    from scrapeline.pipeline.finalise import RunSummary

log = get_logger(__name__)


def build_fetch_client(
    settings: Settings,
    cfg: SourceConfig,
    events: list[FetchEvent],
    *,
    transport: httpx.BaseTransport | None,
    resolver: Resolver,
    sleep: Callable[[float], None],
) -> FetchClient:
    """A FetchClient with the source's politeness that appends every attempt to `events`."""
    limiter = HostRateLimiter(cfg.politeness.rps, sleep=sleep)
    policy = URLPolicy.for_hosts(cfg.hosts, private_hosts=settings.private_hosts, resolver=resolver)
    return FetchClient(
        policy=policy,
        limiter=limiter,
        user_agent=settings.crawler_user_agent,
        respect_robots=cfg.politeness.respect_robots,
        timeout_s=settings.http_timeout_s,
        max_retries=settings.http_max_retries,
        max_redirects=settings.http_max_redirects,
        breaker=CircuitBreaker(settings.circuit_failure_threshold, settings.circuit_reset_s),
        transport=transport,
        on_event=events.append,
        sleep=sleep,
    )


class Crawler:
    """Crawls one source for one run.

    Render targets go through a BrowserPool that shares the fetch client's URL policy,
    limiter and robots cache. The pool starts on the first render target; close() stops it.
    """

    def __init__(
        self,
        cfg: SourceConfig,
        adapter: Adapter,
        client: FetchClient,
        raw_store: RawStore,
        events: list[FetchEvent],
        *,
        screenshot_dir: Path,
    ) -> None:
        self.cfg = cfg
        self.adapter = adapter
        self.client = client
        self.raw_store = raw_store
        self.events = events
        self.screenshot_dir = screenshot_dir
        self._browser: BrowserPool | None = None

    def crawl(self, window: Window, summary: RunSummary, collected: Collected) -> None:
        """Fetch, store and parse every page the adapter leads to, up to the page limit.

        Every fetch attempt ends up in `collected.fetch_rows`, also when crawling fails.
        """
        try:
            self._crawl(window, summary, collected)
        finally:
            self._drain_events(summary.run_id, collected)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def _crawl(self, window: Window, summary: RunSummary, collected: Collected) -> None:
        queue: deque[Target] = deque(self.adapter.discover(window))
        seen: set[str] = set()
        while queue:
            if summary.pages >= self.cfg.max_pages_per_run:
                summary.truncated = True
                log.warning(
                    "run.truncated", max_pages=self.cfg.max_pages_per_run, pending=len(queue)
                )
                break
            target = queue.popleft()
            if target.url in seen:
                continue
            seen.add(target.url)
            if target.render:
                response = self._render(target)
            else:
                response = self.adapter.fetch(target, self.client)
            summary.pages += 1
            ref = self.raw_store.put(
                self.cfg.id,
                response.content,
                ext_for(response.content_type, response.final_url),
                response.fetched_at,
            )
            raw = RawDoc(
                source_id=self.cfg.id,
                url=target.url,
                final_url=response.final_url,
                status=response.status,
                content_type=response.content_type,
                content=response.content,
                sha256=ref.sha256,
                fetched_at=response.fetched_at,
                kind=target.kind,
                meta=target.meta,
            )
            # The last attempt event is the successful one: attach raw provenance to it.
            self._drain_events(summary.run_id, collected, success=(raw, ref.key))
            queue.extend(parse_raw(self.adapter, raw, collected))

    def _render(self, target: Target) -> FetchResponse:
        """Render through the shared-policy BrowserPool; the DOM snapshot becomes the raw doc."""
        if self._browser is None:
            self._browser = BrowserPool(
                policy=self.client.policy,
                limiter=self.client.limiter,
                user_agent=self.client.user_agent,
                robots=self.client.robots,
                on_event=self.events.append,
                screenshot_dir=self.screenshot_dir,
            )
        opts = getattr(self.adapter, "render_options", None)
        response = self._browser.render(
            target.url,
            wait_for=getattr(opts, "wait_for", None),
            click_until_gone=getattr(opts, "click_until_gone", None),
            max_clicks=getattr(opts, "max_clicks", 10),
        )
        # Synthetic final event = the rendered snapshot; it carries the raw provenance.
        self.events.append(
            FetchEvent(
                target.url,
                response.status,
                response.elapsed_ms,
                len(response.content),
                1,
                response.fetched_at,
                None,
            )
        )
        return response

    def _drain_events(
        self,
        run_id: uuid.UUID,
        collected: Collected,
        success: tuple[RawDoc, str] | None = None,
    ) -> None:
        collected.fetch_rows.extend(fetch_log_rows(self.cfg.id, run_id, self.events, success))
        self.events.clear()


def fetch_log_rows(
    source_id: str,
    run_id: uuid.UUID,
    events: Sequence[FetchEvent],
    success: tuple[RawDoc, str] | None = None,
) -> list[dict[str, Any]]:
    """raw.fetch_log rows for these attempts.

    With `success` (the stored raw document and its key), the last attempt is the one that
    produced it and carries its provenance. Every row carries every column, because one
    executemany insert takes its columns from the first row.
    """
    rows: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        row: dict[str, Any] = {
            "run_id": run_id,
            "source_id": source_id,
            "url": ev.url,
            "status": ev.status,
            "attempt": ev.attempt,
            "elapsed_ms": ev.elapsed_ms,
            "nbytes": ev.nbytes,
            "error": ev.error,
            "fetched_at": ev.fetched_at,
            "meta": {},
            "final_url": None,
            "content_type": None,
            "kind": None,
            "raw_sha256": None,
            "raw_key": None,
        }
        if success is not None and i == len(events) - 1:
            raw, key = success
            row.update(
                final_url=raw.final_url,
                content_type=raw.content_type,
                kind=raw.kind,
                raw_sha256=raw.sha256,
                raw_key=key,
                meta=raw.meta,
                fetched_at=raw.fetched_at,
            )
        rows.append(row)
    return rows


def failed_fetches(rows: list[dict[str, Any]]) -> int:
    """URLs that were never fetched; a failed attempt followed by a successful retry is fine."""
    fetched = {r["url"] for r in rows if r.get("raw_key")}
    failed = {r["url"] for r in rows if r["error"] or (r["status"] or 0) >= 400}
    return len(failed - fetched)


def write_fetch_log(conn: Connection, rows: list[dict[str, Any]]) -> None:
    if rows:
        conn.execute(t.fetch_log.insert(), rows)


def stored_raw_rows(
    conn: Connection, source_id: str, since: datetime | None = None
) -> Sequence[Row[Any]]:
    """Fetch-log rows of every successful response the source has in the raw store."""
    query = (
        select(t.fetch_log)
        .where(
            t.fetch_log.c.source_id == source_id,
            t.fetch_log.c.raw_key.is_not(None),
            t.fetch_log.c.status.between(200, 299),
        )
        .order_by(t.fetch_log.c.fetched_at, t.fetch_log.c.id)
    )
    if since is not None:
        query = query.where(t.fetch_log.c.fetched_at >= since)
    return conn.execute(query).fetchall()


def latest_run_raw_rows(conn: Connection, source_id: str) -> Sequence[Row[Any]]:
    """Fetch-log rows of the raw documents the source's latest succeeded crawl stored."""
    run_id = conn.execute(
        select(t.run.c.id)
        .where(
            t.run.c.source_id == source_id,
            t.run.c.status == "succeeded",
            t.run.c.kind != "reprocess",
        )
        .order_by(t.run.c.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run_id is None:
        raise ConfigError(f"no succeeded run for {source_id}; run it first")
    return conn.execute(
        select(t.fetch_log)
        .where(t.fetch_log.c.run_id == run_id, t.fetch_log.c.raw_key.is_not(None))
        .order_by(t.fetch_log.c.id)
    ).fetchall()


def raw_doc_from_log(source_id: str, row: Row[Any], raw_store: RawStore) -> RawDoc:
    """The raw document a fetch-log row points to, read back from the raw store."""
    return RawDoc(
        source_id=source_id,
        url=row.url,
        final_url=row.final_url or row.url,
        status=row.status,
        content_type=row.content_type,
        content=raw_store.get(row.raw_key),
        sha256=row.raw_sha256,
        fetched_at=row.fetched_at,
        kind=row.kind or "page",
        meta=row.meta or {},
    )
