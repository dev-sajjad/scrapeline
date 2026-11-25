"""Incidents: problems the pipeline opened for a person to look at."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import ColumnElement, func, select

from scrapeline.serve.panel.deps import EngineDep, PageDep, render
from scrapeline.serve.panel.pages.sources import OPEN_INCIDENT, source_choices
from scrapeline.store import tables as t

router = APIRouter()

# The status filter: empty means still needing someone (open or acknowledged).
STATUS_CHOICES = (("open", "open"), ("ack", "acknowledged"), ("closed", "closed"), ("all", "all"))
KINDS_LIMIT = 100
BIGINT_MAX = 2**63 - 1


@router.get("/incidents", response_class=HTMLResponse)
def incidents_list(
    request: Request,
    engine: EngineDep,
    page: PageDep,
    status: Annotated[str | None, Query(max_length=10)] = None,
    source_id: Annotated[str | None, Query(max_length=100)] = None,
    kind: Annotated[str | None, Query(max_length=50)] = None,
) -> HTMLResponse:
    i = t.incident
    conditions: list[ColumnElement[bool]] = []
    if not status:
        conditions.append(i.c.status.in_(OPEN_INCIDENT))
    elif status != "all":
        conditions.append(i.c.status == status)
    if source_id:
        conditions.append(i.c.source_id == source_id)
    if kind:
        conditions.append(i.c.kind == kind)
    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(i).where(*conditions)).scalar_one()
        incidents = list(
            conn.execute(
                select(i)
                .where(*conditions)
                .order_by(i.c.opened_at.desc(), i.c.id.desc())
                .limit(page.limit)
                .offset(page.offset)
            ).mappings()
        )
        kinds = conn.execute(
            select(i.c.kind).group_by(i.c.kind).order_by(i.c.kind).limit(KINDS_LIMIT)
        ).scalars()
        kind_choices = [(name, name) for name in kinds]
        sources = source_choices(conn)
    return render(
        request,
        "incidents/index.html",
        {
            "title": "Incidents",
            "incidents": incidents,
            "total": total,
            "page": page,
            "filters": {"status": status, "source_id": source_id, "kind": kind},
            "filtered": bool(status or source_id or kind),
            "status_choices": STATUS_CHOICES,
            "source_choices": sources,
            "kind_choices": kind_choices,
        },
    )


@router.get("/incidents/{incident_id}", response_class=HTMLResponse)
def incident_detail(
    request: Request,
    engine: EngineDep,
    incident_id: Annotated[int, Path(ge=1, le=BIGINT_MAX)],
) -> HTMLResponse:
    i = t.incident
    with engine.connect() as conn:
        incident = conn.execute(select(i).where(i.c.id == incident_id)).mappings().first()
    if incident is None:
        raise HTTPException(404, f"There is no incident #{incident_id}.")
    details = incident["details"] if isinstance(incident["details"], dict) else {}
    return render(
        request,
        "incidents/detail.html",
        {
            "title": f"Incident #{incident_id}",
            "incident": incident,
            "details": details,
            "occurrences": details.get("occurrences", 1),
        },
    )
