"""Health overview: is the platform working, and what needs attention."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from scrapeline.serve.panel.deps import EngineDep, render
from scrapeline.store import tables as t

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def health(request: Request, engine: EngineDep) -> HTMLResponse:
    with engine.connect() as conn:
        sources = conn.execute(select(func.count()).select_from(t.sources)).scalar_one()
        open_incidents = conn.execute(
            select(func.count()).select_from(t.incident).where(t.incident.c.status != "closed")
        ).scalar_one()
    return render(
        request,
        "health/index.html",
        {"title": "Health", "sources": sources, "open_incidents": open_incidents},
    )
