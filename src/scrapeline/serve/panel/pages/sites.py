"""Sites: every host a source belongs to, with its category and how it was decided."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import ColumnElement, func, select

from scrapeline.catalog.sites import site_host
from scrapeline.serve.panel.deps import EngineDep, PageDep, render
from scrapeline.store import tables as t

router = APIRouter()

METHODS = ("human", "rule", "evidence", "llm", "none")
LOG_ROWS = 50
SOURCES_ROWS = 100


@router.get("/sites", response_class=HTMLResponse)
def sites_list(
    request: Request,
    engine: EngineDep,
    page: PageDep,
    category: Annotated[str | None, Query(max_length=30)] = None,
    method: Annotated[str | None, Query(max_length=20)] = None,
) -> HTMLResponse:
    site, cat, s = t.site, t.site_category, t.sources
    conditions: list[ColumnElement[bool]] = []
    if category:
        conditions.append(site.c.category == category)
    if method:
        conditions.append(site.c.category_method == method)
    per_site = select(s.c.site_id, func.count().label("sources")).group_by(s.c.site_id).subquery()
    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(site).where(*conditions)).scalar_one()
        found = conn.execute(
            select(
                site.c.host,
                site.c.category,
                cat.c.label.label("category_label"),
                site.c.category_confidence,
                site.c.category_method,
                site.c.country,
                site.c.language,
                site.c.platform,
                site.c.classified_at,
                func.coalesce(per_site.c.sources, 0).label("sources"),
            )
            .select_from(
                site.join(cat, cat.c.code == site.c.category).outerjoin(
                    per_site, per_site.c.site_id == site.c.id
                )
            )
            .where(*conditions)
            .order_by(site.c.host)
            .limit(page.limit)
            .offset(page.offset)
        ).mappings()
        rows = list(found)
        categories = conn.execute(select(cat.c.code, cat.c.label).order_by(cat.c.label)).all()
    return render(
        request,
        "sites/index.html",
        {
            "title": "Sites",
            "rows": rows,
            "total": total,
            "page": page,
            "filters": {"category": category, "method": method},
            "filtered": bool(conditions),
            "category_choices": [(code, label) for code, label in categories],
            "method_choices": [(value, value) for value in METHODS],
        },
    )


@router.get("/sites/{host}", response_class=HTMLResponse)
def site_detail(request: Request, engine: EngineDep, host: str) -> HTMLResponse:
    site, cat, log, s = t.site, t.site_category, t.classification_log, t.sources
    try:
        key = site_host(host)
    except ValueError as exc:
        raise HTTPException(404, f"{host!r} is not a host name.") from exc
    old_cat, new_cat = cat.alias("old_category"), cat.alias("new_category")
    with engine.connect() as conn:
        found = (
            conn.execute(
                select(site, cat.c.label.label("category_label"), cat.c.description)
                .select_from(site.join(cat, cat.c.code == site.c.category))
                .where(site.c.host == key)
            )
            .mappings()
            .first()
        )
        if found is None:
            raise HTTPException(404, f"There is no site {key!r}.")
        history = list(
            conn.execute(
                select(
                    log.c.old_category,
                    old_cat.c.label.label("old_label"),
                    log.c.new_category,
                    new_cat.c.label.label("new_label"),
                    log.c.method,
                    log.c.confidence,
                    log.c.evidence,
                    log.c.actor,
                    log.c.created_at,
                )
                .select_from(
                    log.outerjoin(old_cat, old_cat.c.code == log.c.old_category).join(
                        new_cat, new_cat.c.code == log.c.new_category
                    )
                )
                .where(log.c.site_id == found["id"])
                .order_by(log.c.created_at.desc(), log.c.id.desc())
                .limit(LOG_ROWS)
            ).mappings()
        )
        log_total = conn.execute(
            select(func.count()).select_from(log).where(log.c.site_id == found["id"])
        ).scalar_one()
        sources = list(
            conn.execute(
                select(s.c.id, s.c.name, s.c.status, s.c.dataset, s.c.platform)
                .where(s.c.site_id == found["id"])
                .order_by(s.c.id)
                .limit(SOURCES_ROWS)
            ).mappings()
        )
    return render(
        request,
        "sites/detail.html",
        {
            "title": key,
            "site": found,
            "evidence": _evidence(found["category_evidence"]),
            "history": history,
            "log_total": log_total,
            "sources": sources,
        },
    )


def _evidence(value: object) -> list[dict[str, object]]:
    """Evidence entries as stored by the classifier; anything malformed is skipped."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]
