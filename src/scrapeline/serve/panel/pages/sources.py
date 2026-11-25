"""Sources: every registered source with how it is doing, and one source in full."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import ColumnElement, Connection, case, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on

from scrapeline.ops.worker import CADENCE
from scrapeline.pipeline.records import keeps_personal_data
from scrapeline.registry.config import UNREVIEWED_TERMS, SourceConfig, UsageRights
from scrapeline.serve.panel.deps import EngineDep, PageDep, render
from scrapeline.store import tables as t

router = APIRouter()

STATUSES = ("draft", "active", "broken", "retired")
OPEN_INCIDENT = ("open", "ack")
DEFAULT_CADENCE = str(SourceConfig.model_fields["cadence"].default)
# A scheduled run may start late (cron granularity, a busy worker); only a source that has
# missed its slot by more than half a period is called overdue.
OVERDUE_GRACE = 0.5
DETAIL_ROWS = 20
CHOICES_LIMIT = 500


def is_overdue(status: str, cadence: str, last_started: datetime | None, now: datetime) -> bool:
    """Whether an active, scheduled source has gone longer than its cadence without a run."""
    every = CADENCE.get(cadence)
    if status != "active" or every is None:
        return False
    return last_started is None or now - last_started > every * (1 + OVERDUE_GRACE)


def attention(
    status: str,
    cadence: str,
    last_status: str | None,
    last_scheduled: datetime | None,
    now: datetime,
) -> tuple[str, ...]:
    """Why a source needs an operator, if it does. Drafts and retired sources never do."""
    if status in ("draft", "retired"):
        return ()
    reasons = []
    if last_status == "failed":
        reasons.append("last run failed")
    if is_overdue(status, cadence, last_scheduled, now):
        reasons.append("overdue")
    return tuple(reasons)


def last_runs(conn: Connection, source_ids: Collection[str]) -> dict[str, Mapping[str, Any]]:
    """The newest run of each source, plus the start of its newest scheduled (incremental) run."""
    if not source_ids:
        return {}
    r = t.run
    newest = conn.execute(
        select(
            r.c.source_id,
            r.c.id,
            r.c.kind,
            r.c.status,
            r.c.started_at,
            r.c.finished_at,
            r.c.error,
            r.c.stats["records"].label("records"),
        )
        .where(r.c.source_id.in_(source_ids))
        .ext(distinct_on(r.c.source_id))
        .order_by(r.c.source_id, r.c.started_at.desc(), r.c.id)
    ).mappings()
    scheduled = dict(
        conn.execute(
            select(r.c.source_id, func.max(r.c.started_at))
            .where(r.c.source_id.in_(source_ids), r.c.kind == "incremental")
            .group_by(r.c.source_id)
        ).all()
    )
    result: dict[str, Mapping[str, Any]] = {}
    for row in newest:
        records = row["records"] if isinstance(row["records"], Mapping) else {}
        result[row["source_id"]] = {
            **row,
            "records": sum(v for v in records.values() if isinstance(v, int)),
            "last_scheduled": scheduled.get(row["source_id"]),
        }
    return result


def record_counts(conn: Connection, source_ids: Collection[str]) -> dict[str, int]:
    """Current records per source: generic records not removed, or typed meeting rows."""
    if not source_ids:
        return {}
    counts: dict[str, int] = {}
    generic = (
        select(t.records.c.source_id, func.count())
        .where(t.records.c.source_id.in_(source_ids), t.records.c.removed_at.is_(None))
        .group_by(t.records.c.source_id)
    )
    typed = (
        select(t.meetings.c.source_id, func.count())
        .where(t.meetings.c.source_id.in_(source_ids))
        .group_by(t.meetings.c.source_id)
    )
    for query in (generic, typed):
        for source_id, n in conn.execute(query):
            counts[source_id] = counts.get(source_id, 0) + n
    return counts


def open_incident_counts(conn: Connection, source_ids: Collection[str]) -> dict[str, int]:
    if not source_ids:
        return {}
    i = t.incident
    return dict(
        conn.execute(
            select(i.c.source_id, func.count())
            .where(i.c.source_id.in_(source_ids), i.c.status.in_(OPEN_INCIDENT))
            .group_by(i.c.source_id)
        ).all()
    )


def source_overview(
    conn: Connection, sources: Sequence[Mapping[Any, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Source rows joined with their last run, record count, open incidents and flags."""
    ids = [row["id"] for row in sources]
    runs, counts, incidents = (
        last_runs(conn, ids),
        record_counts(conn, ids),
        open_incident_counts(conn, ids),
    )
    rows = []
    for source in sources:
        cadence = source.get("cadence") or DEFAULT_CADENCE
        last = runs.get(source["id"])
        rows.append(
            {
                **source,
                "cadence": cadence,
                "last_run": last,
                "records": counts.get(source["id"], 0),
                "open_incidents": incidents.get(source["id"], 0),
                "attention": attention(
                    source["status"],
                    cadence,
                    last["status"] if last else None,
                    last["last_scheduled"] if last else None,
                    now,
                ),
            }
        )
    return rows


def source_choices(conn: Connection) -> list[tuple[str, str]]:
    """Source ids for filter menus on other pages."""
    ids = conn.execute(select(t.sources.c.id).order_by(t.sources.c.id).limit(CHOICES_LIMIT))
    return [(source_id, source_id) for source_id in ids.scalars()]


def _source_columns() -> list[Any]:
    s, site, cat = t.sources, t.site, t.site_category
    return [
        s.c.id,
        s.c.name,
        s.c.status,
        s.c.dataset,
        s.c.config["cadence"].astext.label("cadence"),
        site.c.host,
        site.c.category,
        cat.c.label.label("category_label"),
    ]


def _with_site(query: Any) -> Any:
    s, site, cat = t.sources, t.site, t.site_category
    return query.select_from(
        s.outerjoin(site, site.c.id == s.c.site_id).outerjoin(cat, cat.c.code == site.c.category)
    )


@router.get("/sources", response_class=HTMLResponse)
def sources_list(
    request: Request,
    engine: EngineDep,
    page: PageDep,
    status: Annotated[str | None, Query(max_length=20)] = None,
    dataset: Annotated[str | None, Query(max_length=100)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> HTMLResponse:
    s = t.sources
    conditions: list[ColumnElement[bool]] = []
    if status:
        conditions.append(s.c.status == status)
    if dataset:
        conditions.append(s.c.dataset == dataset)
    if q:
        conditions.append(
            or_(s.c.id.icontains(q, autoescape=True), s.c.name.icontains(q, autoescape=True))
        )
    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(s).where(*conditions)).scalar_one()
        found = conn.execute(
            _with_site(select(*_source_columns()))
            .where(*conditions)
            .order_by(s.c.id)
            .limit(page.limit)
            .offset(page.offset)
        ).mappings()
        rows = source_overview(conn, list(found), datetime.now(UTC))
        datasets = conn.execute(
            select(s.c.dataset)
            .where(s.c.dataset.is_not(None))
            .group_by(s.c.dataset)
            .order_by(s.c.dataset)
            .limit(CHOICES_LIMIT)
        ).scalars()
        dataset_choices = [(name, name) for name in datasets]
    return render(
        request,
        "sources/index.html",
        {
            "title": "Sources",
            "rows": rows,
            "total": total,
            "page": page,
            "filters": {"status": status, "dataset": dataset, "q": q},
            "filtered": bool(conditions),
            "status_choices": [(value, value) for value in STATUSES],
            "dataset_choices": dataset_choices,
        },
    )


def _stored_rows(conn: Connection, source_id: str) -> list[tuple[str, int]]:
    """Rows this source has in each store, in one statement; empty stores are left out."""

    def count(table: Any, *where: Any) -> Any:
        return (
            select(func.count())
            .select_from(table)
            .where(table.c.source_id == source_id, *where)
            .scalar_subquery()
        )

    r = t.records
    labelled = {
        "Current records": count(r, r.c.removed_at.is_(None)),
        "Removed records": count(r, r.c.removed_at.is_not(None)),
        "Meetings": count(t.meetings),
        "Agenda items": count(t.agenda_items),
        "Documents": count(t.documents),
        "Bodies": count(t.bodies),
    }
    values = conn.execute(select(*labelled.values())).one()
    return [(label, n) for label, n in zip(labelled, values, strict=True) if n]


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_detail(request: Request, engine: EngineDep, source_id: str) -> HTMLResponse:
    s, r, i, v = t.sources, t.run, t.incident, t.scraper_versions
    with engine.connect() as conn:
        source = (
            conn.execute(
                _with_site(
                    select(
                        *_source_columns(),
                        s.c.platform,
                        s.c.tenant,
                        s.c.status_note,
                        s.c.config,
                        s.c.config_version,
                        s.c.watermark,
                        s.c.created_at,
                        s.c.updated_at,
                    )
                ).where(s.c.id == source_id)
            )
            .mappings()
            .first()
        )
        if source is None:
            raise HTTPException(404, f"There is no source with id {source_id!r}.")
        overview = source_overview(conn, [source], datetime.now(UTC))[0]
        stored = _stored_rows(conn, source_id)
        versions = conn.execute(
            select(v).where(v.c.source_id == source_id).order_by(v.c.id.desc()).limit(DETAIL_ROWS)
        ).mappings()
        runs = conn.execute(
            select(
                r.c.id, r.c.kind, r.c.status, r.c.started_at, r.c.finished_at, r.c.stats, r.c.error
            )
            .where(r.c.source_id == source_id)
            .order_by(r.c.started_at.desc(), r.c.id)
            .limit(DETAIL_ROWS)
        ).mappings()
        incidents = conn.execute(
            select(i)
            .where(i.c.source_id == source_id)
            .order_by(case((i.c.status.in_(OPEN_INCIDENT), 0), else_=1), i.c.opened_at.desc())
            .limit(DETAIL_ROWS)
        ).mappings()
        context = {
            "title": source_id,
            "source": overview,
            "config": config_view(source["config"]),
            "stored": stored,
            "versions": list(versions),
            "runs": list(runs),
            "incidents": list(incidents),
        }
    return render(request, "sources/detail.html", context)


def config_view(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The parts of a stored source config the detail page shows, read defensively.

    The stored config may come from an older config version, so it is read as plain data
    rather than validated again.
    """
    config = config or {}
    legal = config.get("legal") or {}
    terms = legal.get("terms_checked")
    return {
        "raw": config,
        "entry": config.get("entry"),
        "kind": config.get("kind"),
        "adapter": config.get("adapter"),
        "allowed_hosts": config.get("allowed_hosts") or [],
        "locale": {k: v for k, v in (config.get("locale") or {}).items() if v is not None},
        "usage": config.get("usage") or {},
        "personal_data": personal_data(config.get("usage")),
        "terms_checked": terms,
        "terms_unreviewed": terms is None or str(terms) == UNREVIEWED_TERMS.isoformat(),
        "legal_notes": legal.get("notes"),
    }


def personal_data(usage: object) -> str:
    """What the pipeline does with personal-data fields under the stored usage rights.

    'kept' needs allow_pii and a purpose; 'no-purpose' is allow_pii without one, which still
    blanks them. Usage that does not validate grants nothing, as in the lake export, and is
    reported as 'unreadable'.
    """
    try:
        rights = UsageRights.model_validate(usage or {})
    except ValidationError:
        return "unreadable"
    if keeps_personal_data(rights):
        return "kept"
    return "no-purpose" if rights.allow_pii else "blanked"
