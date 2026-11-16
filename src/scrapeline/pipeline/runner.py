"""Run one source end to end: discover -> fetch -> raw -> parse -> validate -> load -> checks.

Deterministic, no LLM. Failures are recorded (ops.run, raw.fetch_log, ops.incident), never hidden.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from sqlalchemy import Engine, select

from scrapeline.adapters.base import Adapter, Window, adapter_code_hash, load_adapter
from scrapeline.adapters.rawdoc import ParseResult, RawDoc, Reject, Target
from scrapeline.errors import (
    BlockedError,
    CircuitOpenError,
    ConfigError,
    FetchError,
    ParseError,
    RobotsDisallowedError,
    ScrapelineError,
)
from scrapeline.fetch.browser import BrowserPool
from scrapeline.fetch.client import CircuitBreaker, FetchClient, FetchEvent, FetchResponse
from scrapeline.fetch.limiter import HostRateLimiter
from scrapeline.fetch.urlpolicy import Resolver, URLPolicy, system_resolver
from scrapeline.ops import runs
from scrapeline.ops.logging import bind_context, get_logger
from scrapeline.pipeline.load import LoadContext, Provenance, dedupe_latest, load_records
from scrapeline.pipeline.models import SCHEMAS, Record
from scrapeline.quality.checks import CheckResult, run_checks, worst
from scrapeline.quality.incidents import close_incidents, open_incident
from scrapeline.registry.config import SourceConfig, load_sources
from scrapeline.settings import Settings
from scrapeline.store import tables as t
from scrapeline.store.raw import RawStore, ext_for

log = get_logger(__name__)

RunKind = Literal["incremental", "backfill", "reprocess"]


@dataclass
class RunSummary:
    run_id: uuid.UUID
    source_id: str
    kind: str
    status: str = "running"
    pages: int = 0
    records: dict[str, int] = field(default_factory=dict)
    loaded: dict[str, dict[str, int]] = field(default_factory=dict)
    rejects: int = 0
    checks: dict[str, str] = field(default_factory=dict)
    watermark: str | None = None
    truncated: bool = False
    error: str | None = None
    incident_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in self.__dict__.items()}


@dataclass
class _Collected:
    items: list[tuple[Record, Provenance]] = field(default_factory=list)
    rejects: list[tuple[Reject, str | None]] = field(default_factory=list)
    fetch_rows: list[dict[str, Any]] = field(default_factory=list)
    watermark: datetime | None = None

    def absorb(self, result: ParseResult, prov: Provenance) -> None:
        self.items.extend((rec, prov) for rec in result.records)
        self.rejects.extend((rej, prov.raw_sha256) for rej in result.rejects)
        if result.watermark and (self.watermark is None or result.watermark > self.watermark):
            self.watermark = result.watermark


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
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.raw_store = raw_store
        self.transport = transport
        self.resolver = resolver
        self.sleep = sleep
        self._sources = sources
        self._browser: BrowserPool | None = None

    # -- public -----------------------------------------------------------------------------

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
        adapter = load_adapter(cfg)
        with self.engine.connect() as lock_conn:
            if not runs.try_source_lock(lock_conn, cfg.id):
                raise ConfigError(f"another run of {cfg.id} is in progress")
            try:
                return self._run_locked(cfg, adapter, kind, since, until, force)
            finally:
                if self._browser is not None:
                    self._browser.close()
                    self._browser = None
                runs.release_source_lock(lock_conn, cfg.id)
                lock_conn.commit()

    def reprocess(self, source_id: str, *, since: datetime | None = None) -> RunSummary:
        """Rebuild normalised rows from stored raw (after a parser or schema change)."""
        cfg = self.source(source_id)
        adapter = load_adapter(cfg)
        with self.engine.begin() as conn:
            self._prepare(conn, cfg, force=True)
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
            query = (
                select(t.fetch_log)
                .where(
                    t.fetch_log.c.source_id == cfg.id,
                    t.fetch_log.c.raw_key.is_not(None),
                    t.fetch_log.c.status.between(200, 299),
                )
                .order_by(t.fetch_log.c.fetched_at, t.fetch_log.c.id)
            )
            if since is not None:
                query = query.where(t.fetch_log.c.fetched_at >= since)
            log_rows = conn.execute(query).fetchall()
        summary = RunSummary(run_id=run_id, source_id=cfg.id, kind="reprocess")
        collected = _Collected()
        seen: set[str] = set()
        with bind_context(run_id=str(run_id), source_id=cfg.id):
            try:
                for row in log_rows:
                    if row.raw_sha256 in seen:
                        continue
                    seen.add(row.raw_sha256)
                    raw = RawDoc(
                        source_id=cfg.id,
                        url=row.url,
                        final_url=row.final_url or row.url,
                        status=row.status,
                        content_type=row.content_type,
                        content=self.raw_store.get(row.raw_key),
                        sha256=row.raw_sha256,
                        fetched_at=row.fetched_at,
                        kind=row.kind or "page",
                        meta=row.meta or {},
                    )
                    collected.absorb(adapter.parse(raw), Provenance(row.raw_sha256, row.fetched_at))
                    summary.pages += 1
            except ScrapelineError as exc:
                return self._fail(cfg, summary, collected, exc)
            return self._finish(
                cfg, summary, collected, version_id, zero_is_ok=False, advance_watermark=False
            )

    def latest_raw_docs(self, source_id: str) -> list[RawDoc]:
        """Raw documents of the latest succeeded run (offline input for verify/tester)."""
        cfg = self.source(source_id)
        with self.engine.connect() as conn:
            run_id = conn.execute(
                select(t.run.c.id)
                .where(
                    t.run.c.source_id == cfg.id,
                    t.run.c.status == "succeeded",
                    t.run.c.kind != "reprocess",
                )
                .order_by(t.run.c.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if run_id is None:
                raise ConfigError(f"no succeeded run for {cfg.id}; run it first")
            rows = conn.execute(
                select(t.fetch_log)
                .where(t.fetch_log.c.run_id == run_id, t.fetch_log.c.raw_key.is_not(None))
                .order_by(t.fetch_log.c.id)
            ).fetchall()
        return [
            RawDoc(
                source_id=cfg.id,
                url=r.url,
                final_url=r.final_url or r.url,
                status=r.status,
                content_type=r.content_type,
                content=self.raw_store.get(r.raw_key),
                sha256=r.raw_sha256,
                fetched_at=r.fetched_at,
                kind=r.kind or "page",
                meta=r.meta or {},
            )
            for r in rows
        ]

    # -- internals --------------------------------------------------------------------------

    def _prepare(self, conn: Any, cfg: SourceConfig, *, force: bool) -> None:
        status = runs.sync_source(conn, cfg)
        if status in ("retired", "broken") and not force:
            raise ConfigError(f"source {cfg.id} is {status}; use --force to run anyway")

    def _client(self, cfg: SourceConfig, events: list[FetchEvent]) -> FetchClient:
        limiter = HostRateLimiter(cfg.politeness.rps, sleep=self.sleep)
        policy = URLPolicy.for_hosts(
            cfg.hosts, private_hosts=self.settings.private_hosts, resolver=self.resolver
        )
        return FetchClient(
            policy=policy,
            limiter=limiter,
            user_agent=self.settings.crawler_user_agent,
            respect_robots=cfg.politeness.respect_robots,
            timeout_s=self.settings.http_timeout_s,
            max_retries=self.settings.http_max_retries,
            max_redirects=self.settings.http_max_redirects,
            breaker=CircuitBreaker(
                self.settings.circuit_failure_threshold, self.settings.circuit_reset_s
            ),
            transport=self.transport,
            on_event=events.append,
            sleep=self.sleep,
        )

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
            self._prepare(conn, cfg, force=force)
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
        collected = _Collected()
        events: list[FetchEvent] = []

        with (
            bind_context(run_id=str(run_id), source_id=cfg.id),
            self._client(cfg, events) as client,
        ):
            log.info("run.start", kind=kind, since=str(since) if since else None)
            try:
                self._crawl(cfg, adapter, client, window, summary, collected, events)
            except ScrapelineError as exc:
                self._drain_events(cfg, run_id, events, collected)
                return self._fail(cfg, summary, collected, exc)
            self._drain_events(cfg, run_id, events, collected)
            return self._finish(
                cfg,
                summary,
                collected,
                version_id,
                zero_is_ok=zero_is_ok,
                advance_watermark=kind == "incremental" and not summary.truncated,
            )

    def _crawl(
        self,
        cfg: SourceConfig,
        adapter: Adapter,
        client: FetchClient,
        window: Window,
        summary: RunSummary,
        collected: _Collected,
        events: list[FetchEvent],
    ) -> None:
        queue: deque[Target] = deque(adapter.discover(window))
        seen: set[str] = set()
        while queue:
            if summary.pages >= cfg.max_pages_per_run:
                summary.truncated = True
                log.warning("run.truncated", max_pages=cfg.max_pages_per_run, pending=len(queue))
                break
            target = queue.popleft()
            if target.url in seen:
                continue
            seen.add(target.url)
            if target.render:
                response = self._render(cfg, adapter, client, target, events)
            else:
                response = adapter.fetch(target, client)
            summary.pages += 1
            ref = self.raw_store.put(
                cfg.id,
                response.content,
                ext_for(response.content_type, response.final_url),
                response.fetched_at,
            )
            raw = RawDoc(
                source_id=cfg.id,
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
            self._drain_events(cfg, summary.run_id, events, collected, success=(raw, ref.key))
            result = adapter.parse(raw)
            collected.absorb(result, Provenance(ref.sha256, response.fetched_at))
            queue.extend(result.next_targets)

    def _render(
        self,
        cfg: SourceConfig,
        adapter: Adapter,
        client: FetchClient,
        target: Target,
        events: list[FetchEvent],
    ) -> FetchResponse:
        """Render through the shared-policy BrowserPool; the DOM snapshot becomes the raw doc."""
        if self._browser is None:
            self._browser = BrowserPool(
                policy=client.policy,
                limiter=client.limiter,
                user_agent=client.user_agent,
                robots=client.robots,
                on_event=events.append,
                screenshot_dir=self.settings.raw_store_dir.parent / "screenshots",
            )
        opts = getattr(adapter, "render_options", None)
        response = self._browser.render(
            target.url,
            wait_for=getattr(opts, "wait_for", None),
            click_until_gone=getattr(opts, "click_until_gone", None),
            max_clicks=getattr(opts, "max_clicks", 10),
        )
        # Synthetic final event = the rendered snapshot; it carries the raw provenance.
        events.append(
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
        cfg: SourceConfig,
        run_id: uuid.UUID,
        events: list[FetchEvent],
        collected: _Collected,
        success: tuple[RawDoc, str] | None = None,
    ) -> None:
        for i, ev in enumerate(events):
            row: dict[str, Any] = {
                "run_id": run_id,
                "source_id": cfg.id,
                "url": ev.url,
                "status": ev.status,
                "attempt": ev.attempt,
                "elapsed_ms": ev.elapsed_ms,
                "nbytes": ev.nbytes,
                "error": ev.error,
                "fetched_at": ev.fetched_at,
                "meta": {},
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
            collected.fetch_rows.append(row)
        events.clear()

    def _write_common(
        self, conn: Any, cfg: SourceConfig, run_id: uuid.UUID, collected: _Collected
    ) -> None:
        if collected.fetch_rows:
            rows = [
                {
                    "final_url": None,
                    "content_type": None,
                    "kind": None,
                    "raw_sha256": None,
                    "raw_key": None,
                }
                | r
                for r in collected.fetch_rows
            ]
            conn.execute(t.fetch_log.insert(), rows)
        if collected.rejects:
            conn.execute(
                t.rejects.insert(),
                [
                    {
                        "run_id": run_id,
                        "source_id": cfg.id,
                        "record_type": rej.record_type,
                        "raw_sha256": sha,
                        "record": rej.data,
                        "reason": rej.reason,
                    }
                    for rej, sha in collected.rejects
                ],
            )

    def _finish(
        self,
        cfg: SourceConfig,
        summary: RunSummary,
        collected: _Collected,
        version_id: int,
        *,
        zero_is_ok: bool,
        advance_watermark: bool,
    ) -> RunSummary:
        latest = dedupe_latest(collected.items)
        records = [rec for pairs in latest.values() for rec, _ in pairs]
        rejects = [rej for rej, _ in collected.rejects]
        per_raw = Counter(
            (prov.raw_sha256, rec.record_type, rec.external_id) for rec, prov in collected.items
        )
        primary = SCHEMAS[cfg.schema_].record_type
        dup_within_raw = sum(n - 1 for (_, rtype, _), n in per_raw.items() if rtype == primary)
        checks: list[CheckResult] = run_checks(
            records,
            rejects,
            cfg.quality,
            schema=cfg.schema_,
            zero_is_ok=zero_is_ok,
            duplicates_within_raw=dup_within_raw,
        )
        verdict = worst(checks)
        for rec in records:
            summary.records[rec.record_type] = summary.records.get(rec.record_type, 0) + 1
        summary.rejects = len(rejects)
        summary.checks = {c.name: c.status for c in checks}
        summary.status = "failed" if verdict == "fail" else "succeeded"

        with self.engine.begin() as conn:
            self._write_common(conn, cfg, summary.run_id, collected)
            ctx = LoadContext(cfg.id, cfg.jurisdiction.id, summary.run_id, version_id)
            stats = load_records(conn, ctx, collected.items)
            summary.loaded = {k: v.as_dict() for k, v in stats.items()}
            conn.execute(
                t.check_result.insert(),
                [
                    {
                        "run_id": summary.run_id,
                        "source_id": cfg.id,
                        "check_name": c.name,
                        "status": c.status,
                        "value": c.value,
                        "threshold": c.threshold,
                        "details": c.details,
                    }
                    for c in checks
                ],
            )
            if verdict == "fail":
                failed = [c.name for c in checks if c.status == "fail"]
                summary.incident_id = open_incident(
                    conn,
                    source_id=cfg.id,
                    kind="quality",
                    run_id=summary.run_id,
                    summary=f"quality checks failed: {', '.join(failed)}",
                    details={"checks": summary.checks},
                )
            else:
                close_incidents(conn, source_id=cfg.id, kind="quality", note="checks passing")
                close_incidents(conn, source_id=cfg.id, kind="blocked", note="fetch succeeded")
                if advance_watermark and collected.watermark is not None:
                    runs.advance_watermark(conn, cfg.id, collected.watermark)
                    summary.watermark = collected.watermark.isoformat()
            runs.finish_run(
                conn, summary.run_id, status=summary.status, stats=summary.as_dict(), error=None
            )
        log.info("run.finish", **{k: v for k, v in summary.as_dict().items() if k != "run_id"})
        return summary

    def _fail(
        self, cfg: SourceConfig, summary: RunSummary, collected: _Collected, exc: Exception
    ) -> RunSummary:
        summary.status = "failed"
        summary.error = f"{type(exc).__name__}: {exc}"
        kind = (
            "blocked"
            if isinstance(exc, BlockedError | RobotsDisallowedError | CircuitOpenError)
            else "parse_error"
            if isinstance(exc, ParseError)
            else "fetch_error"
            if isinstance(exc, FetchError)
            else "error"
        )
        with self.engine.begin() as conn:
            self._write_common(conn, cfg, summary.run_id, collected)
            summary.incident_id = open_incident(
                conn,
                source_id=cfg.id,
                kind=kind,
                run_id=summary.run_id,
                summary=summary.error,
                details={"exception": type(exc).__name__},
            )
            runs.finish_run(
                conn, summary.run_id, status="failed", stats=summary.as_dict(), error=summary.error
            )
        log.error("run.failed", error=summary.error, incident_id=summary.incident_id)
        return summary
