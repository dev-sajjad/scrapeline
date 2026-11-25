"""Runs: every pipeline run, newest first, and one run with everything it touched."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import ColumnElement, func, or_, select

from scrapeline.serve.panel.deps import EngineDep, PageDep, render
from scrapeline.serve.panel.pages.sources import source_choices
from scrapeline.store import tables as t

router = APIRouter()

STATUSES = ("running", "succeeded", "failed")
KINDS = ("incremental", "backfill", "reprocess")
LOAD_COLUMNS = ("inserted", "updated", "unchanged", "restored", "removed")
REJECTS_SHOWN = 20
FETCHES_SHOWN = 100
DETAIL_ROWS = 50


@router.get("/runs", response_class=HTMLResponse)
def runs_list(
    request: Request,
    engine: EngineDep,
    page: PageDep,
    source_id: Annotated[str | None, Query(max_length=100)] = None,
    status: Annotated[str | None, Query(max_length=20)] = None,
    kind: Annotated[str | None, Query(max_length=20)] = None,
) -> HTMLResponse:
    r = t.run
    conditions: list[ColumnElement[bool]] = []
    if source_id:
        conditions.append(r.c.source_id == source_id)
    if status:
        conditions.append(r.c.status == status)
    if kind:
        conditions.append(r.c.kind == kind)
    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(r).where(*conditions)).scalar_one()
        runs = list(
            conn.execute(
                select(
                    r.c.id,
                    r.c.source_id,
                    r.c.kind,
                    r.c.status,
                    r.c.started_at,
                    r.c.finished_at,
                    r.c.stats,
                    r.c.error,
                )
                .where(*conditions)
                .order_by(r.c.started_at.desc(), r.c.id)
                .limit(page.limit)
                .offset(page.offset)
            ).mappings()
        )
        sources = source_choices(conn)
    return render(
        request,
        "runs/index.html",
        {
            "title": "Runs",
            "runs": runs,
            "total": total,
            "page": page,
            "filters": {"source_id": source_id, "status": status, "kind": kind},
            "filtered": bool(conditions),
            "source_choices": sources,
            "status_choices": [(value, value) for value in STATUSES],
            "kind_choices": [(value, value) for value in KINDS],
        },
    )


def load_rows(loaded: object) -> list[dict[str, Any]]:
    """Per-dataset load counts from run stats as table rows, one per dataset."""
    if not isinstance(loaded, Mapping):
        return []
    return [
        {"dataset": dataset, **{column: counts.get(column) for column in LOAD_COLUMNS}}
        for dataset, counts in loaded.items()
        if isinstance(counts, Mapping)
    ]


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, engine: EngineDep, run_id: str) -> HTMLResponse:
    try:
        key = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(404, f"{run_id!r} is not a run id.") from exc
    r, v, c, rej, f, i = (
        t.run,
        t.scraper_versions,
        t.check_result,
        t.rejects,
        t.fetch_log,
        t.incident,
    )
    with engine.connect() as conn:
        run = (
            conn.execute(
                select(
                    r,
                    v.c.adapter,
                    v.c.code_hash,
                    v.c.author,
                )
                .select_from(r.outerjoin(v, v.c.id == r.c.scraper_version_id))
                .where(r.c.id == key)
            )
            .mappings()
            .first()
        )
        if run is None:
            raise HTTPException(404, f"There is no run {run_id}.")
        checks = list(
            conn.execute(
                select(c).where(c.c.run_id == key).order_by(c.c.id).limit(DETAIL_ROWS)
            ).mappings()
        )
        rejects_total = conn.execute(
            select(func.count()).select_from(rej).where(rej.c.run_id == key)
        ).scalar_one()
        rejects = list(
            conn.execute(
                select(rej).where(rej.c.run_id == key).order_by(rej.c.id).limit(REJECTS_SHOWN)
            ).mappings()
        )
        fetches_total = conn.execute(
            select(func.count()).select_from(f).where(f.c.run_id == key)
        ).scalar_one()
        fetches = list(
            conn.execute(
                select(
                    f.c.url,
                    f.c.final_url,
                    f.c.status,
                    f.c.attempt,
                    f.c.elapsed_ms,
                    f.c.nbytes,
                    f.c.kind,
                    f.c.content_type,
                    f.c.raw_sha256,
                    f.c.error,
                    f.c.fetched_at,
                )
                .where(f.c.run_id == key)
                .order_by(f.c.id)
                .limit(FETCHES_SHOWN)
            ).mappings()
        )
        # An incident opened by an earlier run points here once this run repeated it.
        incidents = list(
            conn.execute(
                select(i)
                .where(or_(i.c.run_id == key, i.c.details["last_run_id"].astext == str(key)))
                .order_by(i.c.opened_at.desc(), i.c.id.desc())
                .limit(DETAIL_ROWS)
            ).mappings()
        )
    stats = run["stats"] if isinstance(run["stats"], Mapping) else {}
    return render(
        request,
        "runs/detail.html",
        {
            "title": f"Run {str(key)[:8]}",
            "run": run,
            "stats": stats,
            "loaded": load_rows(stats.get("loaded")),
            "checks": checks,
            "rejects": rejects,
            "rejects_total": rejects_total,
            "fetches": fetches,
            "fetches_total": fetches_total,
            "incidents": incidents,
        },
    )
