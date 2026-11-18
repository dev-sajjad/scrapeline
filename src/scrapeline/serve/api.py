"""Read-only HTTP API over normalised data and ops tables.

uv run uvicorn scrapeline.serve.api:app --port 8000
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import func, select, text

from scrapeline import __version__
from scrapeline.store import tables as t
from scrapeline.store.db import get_engine

app = FastAPI(
    title="scrapeline",
    version=__version__,
    description="Public meeting records with lineage, plus scraper health.",
)


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            open_incidents = conn.execute(
                select(func.count()).select_from(t.incident).where(t.incident.c.status != "closed")
            ).scalar_one()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"database unavailable: {type(exc).__name__}"
        ) from exc
    return {"status": "ok", "version": __version__, "open_incidents": open_incidents}


@app.get("/sources")
def sources() -> list[dict[str, Any]]:
    last_run = (
        select(t.run.c.source_id, func.max(t.run.c.started_at).label("last_run_at"))
        .group_by(t.run.c.source_id)
        .subquery()
    )
    q = (
        select(
            t.sources.c.id,
            t.sources.c.platform,
            t.sources.c.tenant,
            t.sources.c.status,
            t.sources.c.jurisdiction_id,
            t.sources.c.watermark,
            last_run.c.last_run_at,
            select(func.count())
            .select_from(t.meetings)
            .where(t.meetings.c.source_id == t.sources.c.id)
            .scalar_subquery()
            .label("meetings"),
        )
        .outerjoin(last_run, last_run.c.source_id == t.sources.c.id)
        .order_by(t.sources.c.id)
    )
    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(q)]


@app.get("/meetings")
def meetings(
    jurisdiction: str | None = Query(None, description="e.g. us-wa-seattle"),
    since: date | None = Query(None, description="meeting_date >= since"),
    status: str | None = Query(None, pattern="^(scheduled|cancelled|postponed)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    m = t.meetings
    q = select(
        m.c.source_id,
        m.c.external_id,
        m.c.jurisdiction_id,
        m.c.body_name,
        m.c.title,
        m.c.meeting_date,
        m.c.start_time,
        m.c.location,
        m.c.status,
        m.c.agenda_url,
        m.c.canonical_url,
        m.c.first_seen_at,
        m.c.last_seen_at,
        m.c.raw_sha256,
    )
    if jurisdiction:
        q = q.where(m.c.jurisdiction_id == jurisdiction)
    if since:
        q = q.where(m.c.meeting_date >= since)
    if status:
        q = q.where(m.c.status == status)
    q = q.order_by(m.c.meeting_date.desc(), m.c.id).limit(limit).offset(offset)
    with get_engine().connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(q)]
    return {"count": len(rows), "limit": limit, "offset": offset, "items": rows}


@app.get("/incidents")
def incidents(open_only: bool = True) -> list[dict[str, Any]]:
    q = select(t.incident).order_by(t.incident.c.opened_at.desc()).limit(100)
    if open_only:
        q = q.where(t.incident.c.status != "closed")
    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(q)]
