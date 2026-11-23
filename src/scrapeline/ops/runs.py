"""Bookkeeping for sources, scraper versions, runs and watermarks."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from scrapeline.catalog.repository import ensure_site
from scrapeline.errors import StatusTransitionError
from scrapeline.registry.config import SourceConfig, check_transition
from scrapeline.store import tables as t


def sync_source(conn: Connection, cfg: SourceConfig) -> str:
    """Upsert the source row (plus its site and jurisdiction) from YAML. Returns the DB status.

    YAML status is the initial status; afterwards the DB owns runtime status (broken, active).
    """
    jurisdiction_id = None
    if cfg.jurisdiction is not None:
        j = cfg.jurisdiction
        jurisdiction_id = j.id
        conn.execute(
            insert(t.jurisdictions)
            .values(id=j.id, name=j.name, state=j.state, country=j.country)
            .on_conflict_do_update(
                index_elements=[t.jurisdictions.c.id],
                set_={"name": j.name, "state": j.state, "country": j.country},
            )
        )
    columns = {
        "name": cfg.name,
        "platform": cfg.platform,
        "tenant": cfg.tenant,
        "jurisdiction_id": jurisdiction_id,
        "config_version": cfg.config_version,
        "config": cfg.model_dump(mode="json", by_alias=True),
        "site_id": ensure_site(conn, cfg.entry),
        "dataset": cfg.dataset_name,
    }
    conn.execute(
        insert(t.sources)
        .values(id=cfg.id, status=cfg.status, **columns)
        .on_conflict_do_update(
            index_elements=[t.sources.c.id], set_={**columns, "updated_at": func.now()}
        )
    )
    status: str = conn.execute(
        select(t.sources.c.status).where(t.sources.c.id == cfg.id)
    ).scalar_one()
    return status


def set_status(conn: Connection, source_id: str, new: str, note: str | None = None) -> None:
    """Validated status change. Activation needs a reviewed `legal` block."""
    current, config = conn.execute(
        select(t.sources.c.status, t.sources.c.config).where(t.sources.c.id == source_id)
    ).one()
    check_transition(current, new)
    if new == "active" and not SourceConfig.model_validate(config).terms_reviewed:
        raise StatusTransitionError(
            f"{source_id}: review the site's terms and robots.txt and set legal.terms_checked "
            "before activating"
        )
    conn.execute(
        update(t.sources)
        .where(t.sources.c.id == source_id)
        .values(status=new, status_note=note, updated_at=func.now())
    )


def scraper_version(
    conn: Connection, source_id: str, adapter: str, code_hash: str, author: str
) -> int:
    conn.execute(
        insert(t.scraper_versions)
        .values(source_id=source_id, adapter=adapter, code_hash=code_hash, author=author)
        .on_conflict_do_nothing(constraint="uq_scraper_versions_source_code")
    )
    version_id: int = conn.execute(
        select(t.scraper_versions.c.id).where(
            t.scraper_versions.c.source_id == source_id,
            t.scraper_versions.c.code_hash == code_hash,
        )
    ).scalar_one()
    return version_id


def try_source_lock(conn: Connection, source_id: str) -> bool:
    """Session-level advisory lock held on a dedicated connection for the whole run.

    Guarantees one run per source at a time across processes/workers.
    """
    return bool(
        conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": f"source:{source_id}"}
        ).scalar_one()
    )


def release_source_lock(conn: Connection, source_id: str) -> None:
    conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": f"source:{source_id}"})


def start_run(
    conn: Connection,
    *,
    source_id: str,
    kind: str,
    scraper_version_id: int | None,
    since: datetime | None,
    until: datetime | None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        t.run.insert().values(
            id=run_id,
            source_id=source_id,
            kind=kind,
            status="running",
            scraper_version_id=scraper_version_id,
            since=since,
            until=until,
        )
    )
    return run_id


def finish_run(
    conn: Connection, run_id: uuid.UUID, *, status: str, stats: dict[str, Any], error: str | None
) -> None:
    conn.execute(
        update(t.run)
        .where(t.run.c.id == run_id)
        .values(status=status, stats=stats, error=error, finished_at=func.now())
    )


def get_watermark(conn: Connection, source_id: str) -> datetime | None:
    value: datetime | None = conn.execute(
        select(t.sources.c.watermark).where(t.sources.c.id == source_id)
    ).scalar_one_or_none()
    return value


def advance_watermark(conn: Connection, source_id: str, value: datetime) -> None:
    """Monotonic: never moves backwards."""
    conn.execute(
        update(t.sources)
        .where(t.sources.c.id == source_id)
        .where((t.sources.c.watermark.is_(None)) | (t.sources.c.watermark < value))
        .values(watermark=value, updated_at=func.now())
    )
