"""Operational events: what the platform is doing, for live views and audit.

Events are rows in `ops.event`. Each insert is followed by `pg_notify(EVENTS_CHANNEL, ...)`
with a small JSON payload (id, kind, source_id, run_id); listeners read the full row by id.
Notifications are delivered when the transaction commits, so a rolled-back event is never seen.
Publishing must never break the caller's work: failures are logged, not raised.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, func, select

from scrapeline.ops.logging import get_logger
from scrapeline.store import tables as t

log = get_logger(__name__)

EVENTS_CHANNEL = "scrapeline_events"
EventLevel = Literal["debug", "info", "warning", "error"]


class Event(BaseModel):
    """`kind` is dotted: area.action, e.g. run.started, fetch.done, records.loaded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(pattern=r"^[a-z_]+(\.[a-z_]+)+$", max_length=50)
    message: str = ""
    level: EventLevel = "info"
    source_id: str | None = Field(default=None, max_length=100)
    run_id: uuid.UUID | None = None
    job_id: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EventBus(Protocol):
    def publish(self, event: Event) -> None: ...


class NullEventBus:
    """Discards events. For tests and offline tools."""

    def publish(self, event: Event) -> None:
        return None


class PostgresEventBus:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def publish(self, event: Event) -> None:
        try:
            with self.engine.begin() as conn:
                publish_in(conn, event)
        except Exception as exc:  # events are observability, never a reason to fail a run
            log.warning("event.publish_failed", kind=event.kind, error=repr(exc))


def publish_in(conn: Connection, event: Event) -> int | None:
    """Insert and notify inside the caller's transaction. Returns the event id, or None.

    Runs in a savepoint: a failed insert is logged and rolled back alone, so the caller's
    transaction stays usable.
    """
    try:
        with conn.begin_nested():
            return _insert_and_notify(conn, event)
    except Exception as exc:  # events are observability, never a reason to fail a run
        log.warning("event.publish_failed", kind=event.kind, error=repr(exc))
        return None


def _insert_and_notify(conn: Connection, event: Event) -> int:
    event_id: int = conn.execute(
        t.event.insert()
        .values(
            at=event.at,
            kind=event.kind,
            level=event.level,
            source_id=event.source_id,
            run_id=event.run_id,
            job_id=event.job_id,
            message=event.message,
            data=json.loads(json.dumps(event.data, default=str)),
        )
        .returning(t.event.c.id)
    ).scalar_one()
    payload = {
        "id": event_id,
        "kind": event.kind,
        "source_id": event.source_id,
        "run_id": str(event.run_id) if event.run_id else None,
    }
    conn.execute(select(func.pg_notify(EVENTS_CHANNEL, json.dumps(payload))))
    return event_id
