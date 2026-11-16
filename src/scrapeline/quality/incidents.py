"""ops.incident lifecycle: open (deduplicated per source+kind) -> ack -> closed."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Connection, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from scrapeline.ops.logging import get_logger
from scrapeline.store.tables import incident

log = get_logger(__name__)


def open_incident(
    conn: Connection,
    *,
    source_id: str,
    kind: str,
    summary: str,
    run_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Open an incident, or bump the existing open one for the same source+kind."""
    existing = conn.execute(
        select(incident.c.id, incident.c.details).where(
            incident.c.source_id == source_id,
            incident.c.kind == kind,
            incident.c.status.in_(("open", "ack")),
        )
    ).first()
    if existing is not None:
        merged = dict(existing.details or {})
        merged["occurrences"] = int(merged.get("occurrences", 1)) + 1
        merged["last_summary"] = summary
        merged["last_run_id"] = str(run_id) if run_id else None
        conn.execute(update(incident).where(incident.c.id == existing.id).values(details=merged))
        log.warning("incident.repeat", incident_id=existing.id, source_id=source_id, kind=kind)
        return int(existing.id)
    new_id = conn.execute(
        incident.insert()
        .values(
            source_id=source_id,
            run_id=run_id,
            kind=kind,
            summary=summary,
            details={"occurrences": 1, **(details or {})},
        )
        .returning(incident.c.id)
    ).scalar_one()
    # Alert = structured log event (webhook optional, later). Runbook: docs/runbook/<kind>.md
    log.error(
        "incident.opened", incident_id=new_id, source_id=source_id, kind=kind, summary=summary
    )
    return int(new_id)


def close_incidents(conn: Connection, *, source_id: str, kind: str, note: str) -> int:
    result = conn.execute(
        update(incident)
        .where(
            incident.c.source_id == source_id,
            incident.c.kind == kind,
            incident.c.status.in_(("open", "ack")),
        )
        .values(
            status="closed",
            closed_at=func.now(),
            details=incident.c.details.op("||")(literal({"resolution": note}, type_=JSONB)),
        )
    )
    return int(result.rowcount or 0)
