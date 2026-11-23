"""Run one source end to end: discover -> fetch -> raw -> parse -> validate -> load -> checks.

Deterministic, no LLM. Failures are recorded (ops.run, raw.fetch_log, ops.incident), never hidden.
The Runner holds the source lock and orders the stages: `crawl` fetches pages into the raw
store, `ingest` parses and loads what they hold, and `finalise` checks the result, settles
incidents and the watermark, and records and publishes the run.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from sqlalchemy import Connection, Engine

from scrapeline.adapters.base import Adapter, Window, adapter_code_hash, load_adapter
from scrapeline.adapters.rawdoc import RawDoc
from scrapeline.catalog.datasets import DatasetType, default_registry
from scrapeline.catalog.repository import ensure_dataset_type
from scrapeline.errors import ConfigError, ScrapelineError
from scrapeline.fetch.client import FetchClient, FetchEvent
from scrapeline.fetch.urlpolicy import Resolver, system_resolver
from scrapeline.ops import runs
from scrapeline.ops.events import EventBus, PostgresEventBus
from scrapeline.ops.logging import bind_context, get_logger
from scrapeline.pipeline.crawl import (
    Crawler,
    build_fetch_client,
    latest_run_raw_rows,
    raw_doc_from_log,
    stored_raw_rows,
)
from scrapeline.pipeline.crawl import failed_fetches as _failed_fetches
from scrapeline.pipeline.finalise import Finaliser, RunSummary, is_complete_crawl
from scrapeline.pipeline.ingest import (
    Collected,
    Items,
    parse_raw,
    split_by_storage,
    storable_reject,
)
from scrapeline.pipeline.records import PostgresRecordStore, RecordStore
from scrapeline.registry.config import SourceConfig, load_sources
from scrapeline.settings import Settings
from scrapeline.store.raw import RawStore

if TYPE_CHECKING:
    import httpx

# The pipeline's entry point: callers import these from here, wherever a stage defines them.
__all__ = [
    "Items",
    "RunKind",
    "RunSummary",
    "Runner",
    "_failed_fetches",
    "is_complete_crawl",
    "split_by_storage",
    "storable_reject",
]

log = get_logger(__name__)

RunKind = Literal["incremental", "backfill", "reprocess"]


class Runner:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        raw_store: RawStore,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
        sleep: Callable[[float], None] = time.sleep,
        sources: dict[str, SourceConfig] | None = None,
        adapter_factory: Callable[[SourceConfig], Adapter] = load_adapter,
        event_bus: EventBus | None = None,
        record_store: RecordStore | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.raw_store = raw_store
        self.transport = transport
        self.resolver = resolver
        self.sleep = sleep
        self._sources = sources
        self.adapter_factory = adapter_factory
        self.event_bus: EventBus = event_bus if event_bus is not None else PostgresEventBus(engine)
        self.record_store: RecordStore = record_store or PostgresRecordStore()

    def source(self, source_id: str) -> SourceConfig:
        sources = self._sources or load_sources(self.settings.sources_dir)
        if source_id not in sources:
            raise ConfigError(f"unknown source '{source_id}'")
        return sources[source_id]

    def run(
        self,
        source_id: str,
        *,
        kind: RunKind = "incremental",
        since: datetime | None = None,
        until: datetime | None = None,
        force: bool = False,
    ) -> RunSummary:
        cfg = self.source(source_id)
        adapter = self.adapter_factory(cfg)
        with self._source_lock(cfg):
            return self._run_locked(cfg, adapter, kind, since, until, force)

    def reprocess(self, source_id: str, *, since: datetime | None = None) -> RunSummary:
        """Rebuild normalised rows from stored raw (after a parser or schema change)."""
        cfg = self.source(source_id)
        adapter = self.adapter_factory(cfg)
        with self._source_lock(cfg):
            return self._reprocess_locked(cfg, adapter, since)

    def latest_raw_docs(self, source_id: str) -> list[RawDoc]:
        """Raw documents of the latest succeeded run (offline input for verify/tester)."""
        cfg = self.source(source_id)
        with self.engine.connect() as conn:
            rows = latest_run_raw_rows(conn, cfg.id)
        return [raw_doc_from_log(cfg.id, row, self.raw_store) for row in rows]

    @contextmanager
    def _source_lock(self, cfg: SourceConfig) -> Iterator[None]:
        """Hold the source's advisory lock: one writer per source across processes.

        Runs and reprocessing both take it, because the record store numbers versions from
        the rows it reads at the start of a load.
        """
        with self.engine.connect() as lock_conn:
            if not runs.try_source_lock(lock_conn, cfg.id):
                raise ConfigError(f"another run of {cfg.id} is in progress")
            try:
                yield
            finally:
                runs.release_source_lock(lock_conn, cfg.id)
                lock_conn.commit()

    def _prepare(self, conn: Connection, cfg: SourceConfig, *, force: bool) -> DatasetType:
        """Sync the source row and its dataset type; return the dataset its records belong to."""
        dataset = default_registry().get(cfg.dataset_name)
        status = runs.sync_source(conn, cfg)
        if status in ("retired", "broken") and not force:
            raise ConfigError(f"source {cfg.id} is {status}; use --force to run anyway")
        ensure_dataset_type(conn, dataset)
        return dataset

    def _client(self, cfg: SourceConfig, events: list[FetchEvent]) -> FetchClient:
        return build_fetch_client(
            self.settings,
            cfg,
            events,
            transport=self.transport,
            resolver=self.resolver,
            sleep=self.sleep,
        )

    def _finaliser(self) -> Finaliser:
        return Finaliser(self.engine, self.record_store, self.event_bus)

    def _run_locked(
        self,
        cfg: SourceConfig,
        adapter: Adapter,
        kind: RunKind,
        since: datetime | None,
        until: datetime | None,
        force: bool,
    ) -> RunSummary:
        with self.engine.begin() as conn:
            dataset = self._prepare(conn, cfg, force=force)
            version_id = runs.scraper_version(
                conn, cfg.id, cfg.adapter, adapter_code_hash(adapter), cfg.owner
            )
            stored = runs.get_watermark(conn, cfg.id)
            if kind == "incremental" and since is None and stored is not None:
                since = stored - timedelta(hours=cfg.watermark.overlap_hours)
            run_id = runs.start_run(
                conn,
                source_id=cfg.id,
                kind=kind,
                scraper_version_id=version_id,
                since=since,
                until=until,
            )
        window = Window(since=since, until=until, backfill=kind == "backfill")
        zero_is_ok = kind == "incremental" and stored is not None
        summary = RunSummary(run_id=run_id, source_id=cfg.id, kind=kind)
        window_data = {
            "kind": kind,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        }
        finaliser = self._finaliser()
        finaliser.emit(summary, "run.started", f"{kind} run", window_data)
        collected = Collected()
        events: list[FetchEvent] = []
        screenshot_dir = self.settings.raw_store_dir.parent / "screenshots"

        with (
            bind_context(run_id=str(run_id), source_id=cfg.id),
            self._client(cfg, events) as client,
            closing(
                Crawler(cfg, adapter, client, self.raw_store, events, screenshot_dir=screenshot_dir)
            ) as crawler,
        ):
            log.info("run.start", kind=kind, since=str(since) if since else None)
            try:
                crawler.crawl(window, summary, collected)
            except ScrapelineError as exc:
                return finaliser.fail(cfg, dataset, summary, collected, exc)
            return finaliser.finish(
                cfg,
                dataset,
                summary,
                collected,
                version_id,
                zero_is_ok=zero_is_ok,
                advance_watermark=kind == "incremental" and not summary.truncated,
                windowed=since is not None or until is not None,
            )

    def _reprocess_locked(
        self, cfg: SourceConfig, adapter: Adapter, since: datetime | None
    ) -> RunSummary:
        with self.engine.begin() as conn:
            dataset = self._prepare(conn, cfg, force=True)
            version_id = runs.scraper_version(
                conn, cfg.id, cfg.adapter, adapter_code_hash(adapter), cfg.owner
            )
            run_id = runs.start_run(
                conn,
                source_id=cfg.id,
                kind="reprocess",
                scraper_version_id=version_id,
                since=since,
                until=None,
            )
            log_rows = stored_raw_rows(conn, cfg.id, since)
        summary = RunSummary(run_id=run_id, source_id=cfg.id, kind="reprocess")
        finaliser = self._finaliser()
        finaliser.emit(summary, "run.started", "reprocess from raw", {"kind": "reprocess"})
        collected = Collected()
        seen: set[str] = set()
        with bind_context(run_id=str(run_id), source_id=cfg.id):
            try:
                for row in log_rows:
                    if row.raw_sha256 in seen:
                        continue
                    seen.add(row.raw_sha256)
                    parse_raw(adapter, raw_doc_from_log(cfg.id, row, self.raw_store), collected)
                    summary.pages += 1
            except ScrapelineError as exc:
                return finaliser.fail(cfg, dataset, summary, collected, exc)
            return finaliser.finish(
                cfg,
                dataset,
                summary,
                collected,
                version_id,
                zero_is_ok=False,
                advance_watermark=False,
                windowed=since is not None,
            )
