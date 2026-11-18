"""Postgres job queue (ADR-0003): FOR UPDATE SKIP LOCKED, leases, retries, dead-letter.

Jobs are idempotent by design (runs upsert), so a re-delivered job after a lost lease is safe.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, and_, func, or_, select, update

from scrapeline.store import tables as t

LEASE = timedelta(minutes=15)
BACKOFF_BASE = timedelta(minutes=2)


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    source_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def enqueue(
    engine: Engine,
    kind: str,
    source_id: str | None,
    payload: dict[str, Any] | None = None,
    *,
    dedupe: bool = True,
) -> int | None:
    """Add a job. With dedupe, skip if the same kind+source is already queued or running."""
    with engine.begin() as conn:
        if dedupe:
            exists = conn.execute(
                select(t.job.c.id).where(
                    t.job.c.kind == kind,
                    t.job.c.source_id == source_id,
                    t.job.c.status.in_(("queued", "running")),
                )
            ).first()
            if exists:
                return None
        job_id: int = conn.execute(
            t.job.insert()
            .values(kind=kind, source_id=source_id, payload=payload or {})
            .returning(t.job.c.id)
        ).scalar_one()
        return job_id


def claim(engine: Engine, wid: str, now: datetime | None = None) -> Job | None:
    """Claim one runnable job: queued and due, or running with an expired lease."""
    now = now or datetime.now(UTC)
    with engine.begin() as conn:
        row = conn.execute(
            select(t.job)
            .where(
                or_(
                    and_(t.job.c.status == "queued", t.job.c.run_after <= now),
                    and_(t.job.c.status == "running", t.job.c.lease_until < now),
                )
            )
            .order_by(t.job.c.run_after, t.job.c.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if row is None:
            return None
        conn.execute(
            update(t.job)
            .where(t.job.c.id == row.id)
            .values(
                status="running",
                lease_until=now + LEASE,
                worker_id=wid,
                attempts=t.job.c.attempts + 1,
                updated_at=func.now(),
            )
        )
        return Job(
            row.id, row.kind, row.source_id, row.payload or {}, row.attempts + 1, row.max_attempts
        )


def heartbeat(engine: Engine, job: Job) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(t.job)
            .where(t.job.c.id == job.id)
            .values(lease_until=datetime.now(UTC) + LEASE, updated_at=func.now())
        )


def complete(engine: Engine, job: Job) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(t.job)
            .where(t.job.c.id == job.id)
            .values(status="done", lease_until=None, updated_at=func.now())
        )


def fail(engine: Engine, job: Job, error: str) -> str:
    """Retry with exponential backoff, or dead-letter after max_attempts. Returns new status."""
    dead = job.attempts >= job.max_attempts
    status = "dead" if dead else "queued"
    with engine.begin() as conn:
        conn.execute(
            update(t.job)
            .where(t.job.c.id == job.id)
            .values(
                status=status,
                lease_until=None,
                last_error=error[:2000],
                updated_at=func.now(),
                run_after=datetime.now(UTC) + BACKOFF_BASE * 2 ** (job.attempts - 1),
            )
        )
    return status
