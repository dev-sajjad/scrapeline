"""Finalise a run: quality checks, incidents, completeness and removals, watermark, run stats
and events.

A finished run's writes share one transaction, so a failed load leaves nothing half-done; the
failure path then still records the fetch log, the rejects, an incident and the run status.
Progress is published as events, and a failing event bus never fails a run.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import IntegrityError

from scrapeline.catalog.datasets import DatasetType
from scrapeline.errors import (
    BlockedError,
    CircuitOpenError,
    DatasetError,
    FetchError,
    ParseError,
    RobotsDisallowedError,
    ScrapelineError,
)
from scrapeline.ops import runs
from scrapeline.ops.events import Event, EventBus, EventLevel
from scrapeline.ops.logging import get_logger
from scrapeline.pipeline.crawl import failed_fetches, write_fetch_log
from scrapeline.pipeline.ingest import Collected, Items, load_items, split_by_storage, write_rejects
from scrapeline.pipeline.load import LoadContext, dedupe_latest
from scrapeline.pipeline.records import RecordStore
from scrapeline.quality.checks import CheckResult, CheckStatus, run_checks, worst
from scrapeline.quality.incidents import close_incidents, open_incident
from scrapeline.registry.config import SourceConfig
from scrapeline.store import tables as t

log = get_logger(__name__)

_LEVEL_FOR: dict[CheckStatus, EventLevel] = {"pass": "info", "warn": "warning", "fail": "error"}


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
    complete: bool = False  # only claimed for generic datasets, where it gates removals
    removed: int = 0
    error: str | None = None
    incident_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in self.__dict__.items()}


def is_complete_crawl(
    kind: str,
    *,
    windowed: bool,
    truncated: bool,
    failed_fetches: int,
    rejects: int,
    verdict: CheckStatus,
) -> bool:
    """Whether a run saw everything the source lists, so an identity it did not see is gone.

    Only a full incremental crawl qualifies: every page reached (page limit not hit, no page
    that failed to fetch), no rejects, no time window narrowing what was listed, and no
    failed quality check, so zero rows from a broken parser never empty the history.
    """
    return (
        kind == "incremental"
        and not windowed
        and not truncated
        and failed_fetches == 0
        and rejects == 0
        and verdict != "fail"
    )


def duplicates_within_raw(items: Items, record_type: str) -> int:
    """Extra copies of an identity within one raw document, counted for one record type.

    Twice in one document is a parser bug; the same identity in two documents (overlapping
    pages or windows) is normal and resolved by the loader, so it is not counted.
    """
    per_raw = Counter((prov.raw_sha256, rec.record_type, rec.external_id) for rec, prov in items)
    return sum(n - 1 for (_, rtype, _), n in per_raw.items() if rtype == record_type)


def incident_kind(exc: Exception) -> str:
    """The incident a run that failed with `exc` opens."""
    if isinstance(exc, BlockedError | RobotsDisallowedError | CircuitOpenError):
        return "blocked"
    if isinstance(exc, ParseError):
        return "drift"
    if isinstance(exc, FetchError):
        return "fetch_error"
    return "error"


def run_outcome(summary: RunSummary) -> dict[str, Any]:
    """The run's stats as event data."""
    # The event itself carries the ids; loads and checks have their own events.
    elsewhere = {"run_id", "source_id", "loaded", "checks"}
    return {k: v for k, v in summary.as_dict().items() if k not in elsewhere}


def publish(
    bus: EventBus,
    summary: RunSummary,
    kind: str,
    message: str,
    data: dict[str, Any],
    *,
    level: EventLevel = "info",
) -> None:
    """Publish one event about this run; a failing bus is logged, never raised."""
    try:
        bus.publish(
            Event(
                kind=kind,
                message=message,
                level=level,
                source_id=summary.source_id,
                run_id=summary.run_id,
                data=data,
            )
        )
    except Exception as exc:  # events are observability, never a reason to fail a run
        log.warning("event.publish_failed", kind=kind, error=repr(exc))


def _write_collected(
    conn: Connection,
    cfg: SourceConfig,
    dataset: DatasetType,
    run_id: uuid.UUID,
    collected: Collected,
) -> None:
    write_fetch_log(conn, collected.fetch_rows)
    write_rejects(conn, cfg, dataset, run_id, collected.rejects)


class Finaliser:
    """Settles a run once crawling or reprocessing is over: succeeded, or failed with a reason."""

    def __init__(self, engine: Engine, record_store: RecordStore, event_bus: EventBus) -> None:
        self.engine = engine
        self.record_store = record_store
        self.event_bus = event_bus

    def emit(
        self,
        summary: RunSummary,
        kind: str,
        message: str,
        data: dict[str, Any],
        *,
        level: EventLevel = "info",
    ) -> None:
        publish(self.event_bus, summary, kind, message, data, level=level)

    def finish(
        self,
        cfg: SourceConfig,
        dataset: DatasetType,
        summary: RunSummary,
        collected: Collected,
        version_id: int,
        *,
        zero_is_ok: bool,
        advance_watermark: bool,
        windowed: bool,
    ) -> RunSummary:
        """Check, load and record the run; any failure on the way goes through `fail`."""
        try:
            generic, typed = split_by_storage(dataset, collected.items)
        except DatasetError as exc:
            return self.fail(cfg, dataset, summary, collected, exc)

        latest = dedupe_latest(collected.items)
        records = [rec for pairs in latest.values() for rec, _ in pairs]
        rejects = [rej for rej, _ in collected.rejects]
        checks: list[CheckResult] = run_checks(
            records,
            rejects,
            cfg.quality,
            dataset=dataset,
            zero_is_ok=zero_is_ok,
            duplicates_within_raw=duplicates_within_raw(collected.items, dataset.name),
        )
        verdict = worst(checks)
        failed = [c.name for c in checks if c.status == "fail"]
        for rec in records:
            summary.records[rec.record_type] = summary.records.get(rec.record_type, 0) + 1
        summary.rejects = len(rejects)
        summary.checks = {c.name: c.status for c in checks}
        summary.status = "failed" if verdict == "fail" else "succeeded"
        # A typed adapter may narrow what it lists on its own (Legistar's first-run lookback),
        # which the runner cannot see, so completeness is claimed for generic datasets only.
        summary.complete = dataset.storage == "generic" and is_complete_crawl(
            summary.kind,
            windowed=windowed,
            truncated=summary.truncated,
            failed_fetches=failed_fetches(collected.fetch_rows),
            rejects=len(rejects),
            verdict=verdict,
        )

        try:
            with self.engine.begin() as conn:
                _write_collected(conn, cfg, dataset, summary.run_id, collected)
                started_at = conn.execute(
                    select(t.run.c.started_at).where(t.run.c.id == summary.run_id)
                ).scalar_one()
                ctx = LoadContext(
                    cfg.id,
                    cfg.jurisdiction.id if cfg.jurisdiction else None,
                    summary.run_id,
                    version_id,
                    run_started_at=started_at,
                    usage=cfg.usage,
                )
                self._load(conn, ctx, dataset, summary, generic=generic, typed=typed)
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
        except (ScrapelineError, IntegrityError) as exc:
            # The load rolled back; failing through `fail` still records the fetch log (so
            # the raw stays reachable for reprocess), the incident and the run status.
            summary.loaded, summary.removed, summary.watermark = {}, 0, None
            summary.complete = False
            return self.fail(cfg, dataset, summary, collected, exc)
        log.info("run.finish", **{k: v for k, v in summary.as_dict().items() if k != "run_id"})
        self._publish_finish(dataset, summary, checks)
        return summary

    def fail(
        self,
        cfg: SourceConfig,
        dataset: DatasetType,
        summary: RunSummary,
        collected: Collected,
        exc: Exception,
    ) -> RunSummary:
        """Record a failed run: what it fetched and rejected, an incident and the error."""
        summary.status = "failed"
        summary.error = f"{type(exc).__name__}: {exc}"
        with self.engine.begin() as conn:
            _write_collected(conn, cfg, dataset, summary.run_id, collected)
            summary.incident_id = open_incident(
                conn,
                source_id=cfg.id,
                kind=incident_kind(exc),
                run_id=summary.run_id,
                summary=summary.error,
                details={"exception": type(exc).__name__},
            )
            runs.finish_run(
                conn, summary.run_id, status="failed", stats=summary.as_dict(), error=summary.error
            )
        log.error("run.failed", error=summary.error, incident_id=summary.incident_id)
        self.emit(summary, "run.failed", summary.error, run_outcome(summary), level="error")
        return summary

    def _load(
        self,
        conn: Connection,
        ctx: LoadContext,
        dataset: DatasetType,
        summary: RunSummary,
        *,
        generic: Items,
        typed: Items,
    ) -> None:
        """Load the run's records; after a complete crawl, mark the identities it missed removed."""
        typed_stats, stats = load_items(
            conn, ctx, dataset, generic=generic, typed=typed, record_store=self.record_store
        )
        loaded = {name: s.as_dict() for name, s in typed_stats.items()}
        if stats is not None:
            if summary.complete:
                seen = {rec.external_id for rec, _ in generic}
                stats.removed = self.record_store.mark_removed(conn, ctx, dataset, seen)
            loaded[dataset.name] = stats.as_dict()
            summary.removed = stats.removed
        summary.loaded = loaded

    def _publish_finish(
        self, dataset: DatasetType, summary: RunSummary, checks: list[CheckResult]
    ) -> None:
        for name, counts in summary.loaded.items():
            generic = dataset.storage == "generic" and name == dataset.name
            message = f"{name}: " + ", ".join(f"{n} {change}" for change, n in counts.items())
            data = {"dataset": name, "storage": "generic" if generic else "typed", **counts}
            self.emit(summary, "records.loaded", message, data)
        verdict = worst(checks)
        failed = [c.name for c in checks if c.status == "fail"]
        tally = Counter(c.status for c in checks)
        self.emit(
            summary,
            "checks.done",
            f"checks {verdict}",
            {
                "verdict": verdict,
                "failed": failed,
                **{s: tally[s] for s in ("pass", "warn", "fail")},
            },
            level=_LEVEL_FOR[verdict],
        )
        if summary.status == "failed":
            message = f"quality checks failed: {', '.join(failed)}"
            self.emit(summary, "run.failed", message, run_outcome(summary), level="error")
        else:
            self.emit(summary, "run.finished", "run succeeded", run_outcome(summary))
