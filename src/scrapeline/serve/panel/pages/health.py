"""Health overview: is the platform working, and what needs attention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import Connection, and_, func, select

from scrapeline.analytics.export import lake_status
from scrapeline.errors import ConfigError
from scrapeline.serve.panel.deps import EngineDep, SettingsDep, render
from scrapeline.serve.panel.pages.sources import OPEN_INCIDENT, STATUSES, source_overview
from scrapeline.store import tables as t

router = APIRouter()

# Health judges at most this many sources; the sources page lists the rest.
SOURCES_CHECKED = 500
SOURCES_SHOWN = 25
INCIDENTS_SHOWN = 5
# Dead jobs stay in the queue table for good, so only recent ones say something is wrong now.
FAILED_HOURS = 24
FAILED_WINDOW = timedelta(hours=FAILED_HOURS)


@dataclass(frozen=True)
class LakeFreshness:
    """When the Parquet lake was last written, summarised over its tables."""

    last_export: datetime | None
    tables: int
    exported: int
    rows: int
    error: str | None = None


def lake_freshness(lake_dir: Path) -> LakeFreshness:
    try:
        status = lake_status(lake_dir)
    except (ConfigError, OSError, duckdb.Error) as exc:
        return LakeFreshness(None, 0, 0, 0, error=str(exc))
    tables: dict[str, Any] = status["tables"]
    times = [
        datetime.fromisoformat(entry["last_export"]["at"])
        for entry in tables.values()
        if (entry.get("last_export") or {}).get("at")
    ]
    return LakeFreshness(
        last_export=max(times) if times else None,
        tables=len(tables),
        exported=len(times),
        rows=sum(int(entry.get("rows", 0)) for entry in tables.values()),
    )


def job_queue(conn: Connection) -> dict[str, Any]:
    """Queue counts in one pass.

    A running job whose lease has passed has stalled. `failed` counts jobs dead-lettered
    within FAILED_WINDOW (their last update is the dead-lettering); `failed_total` counts all.
    """
    j = t.job
    row = conn.execute(
        select(
            func.count().filter(j.c.status == "queued").label("queued"),
            func.count().filter(j.c.status == "running").label("running"),
            func.count()
            .filter(and_(j.c.status == "running", j.c.lease_until < func.now()))
            .label("stalled"),
            func.count()
            .filter(and_(j.c.status == "dead", j.c.updated_at > func.now() - FAILED_WINDOW))
            .label("failed"),
            func.count().filter(j.c.status == "dead").label("failed_total"),
            func.min(j.c.created_at).filter(j.c.status == "queued").label("oldest_queued"),
        )
    ).mappings()
    return dict(row.one())


def _counted(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def verdict(flagged: int, queue: dict[str, Any], open_incidents: int) -> tuple[str, list[str]]:
    """The overall state ('ok', 'attention' or 'bad') and the problems behind it.

    Failing sources and stuck or recently dead jobs mean scraping is not happening ('bad');
    open incidents alone are known problems someone may already be handling ('attention').
    """
    problems = []
    if flagged:
        verb = "needs" if flagged == 1 else "need"
        problems.append(f"{_counted(flagged, 'source')} {verb} attention")
    if queue["stalled"]:
        problems.append(_counted(queue["stalled"], "stalled job"))
    if queue["failed"]:
        problems.append(f"{_counted(queue['failed'], 'failed job')} in the last {FAILED_HOURS} h")
    if open_incidents:
        problems.append(_counted(open_incidents, "open incident"))
    if not problems:
        return "ok", []
    serious = flagged or queue["stalled"] or queue["failed"]
    return ("bad" if serious else "attention"), problems


@router.get("/", response_class=HTMLResponse)
def health(request: Request, engine: EngineDep, settings: SettingsDep) -> HTMLResponse:
    s, i = t.sources, t.incident
    with engine.connect() as conn:
        queue = job_queue(conn)
        by_status: dict[str, int] = dict(
            conn.execute(select(s.c.status, func.count()).group_by(s.c.status)).all()
        )
        checked = conn.execute(
            select(s.c.id, s.c.name, s.c.status, s.c.config["cadence"].astext.label("cadence"))
            .order_by(s.c.id)
            .limit(SOURCES_CHECKED)
        ).mappings()
        overview = source_overview(conn, list(checked), datetime.now(UTC))
        open_incidents = conn.execute(
            select(func.count()).select_from(i).where(i.c.status.in_(OPEN_INCIDENT))
        ).scalar_one()
        incidents = list(
            conn.execute(
                select(i)
                .where(i.c.status.in_(OPEN_INCIDENT))
                .order_by(i.c.opened_at.desc(), i.c.id.desc())
                .limit(INCIDENTS_SHOWN)
            ).mappings()
        )
    # Sources that need attention first, then the rest by id.
    overview.sort(key=lambda row: (not row["attention"], row["id"]))
    flagged = sum(1 for row in overview if row["attention"])
    state, problems = verdict(flagged, queue, open_incidents)
    total_sources = sum(by_status.values())
    return render(
        request,
        "health/index.html",
        {
            "title": "Health",
            "state": state,
            "problems": problems,
            "queue": queue,
            "failed_hours": FAILED_HOURS,
            "by_status": [(status, by_status.get(status, 0)) for status in STATUSES]
            + [(status, n) for status, n in sorted(by_status.items()) if status not in STATUSES],
            "total_sources": total_sources,
            "checked_all": total_sources <= SOURCES_CHECKED,
            "sources_checked": SOURCES_CHECKED,
            "sources": overview[:SOURCES_SHOWN],
            "flagged": flagged,
            "open_incidents": open_incidents,
            "incidents": incidents,
            "lake": lake_freshness(settings.lake_dir),
        },
    )
