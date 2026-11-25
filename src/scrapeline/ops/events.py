"""Operational events: what the platform is doing, for live views and audit.

Events are rows in `ops.event`. Each insert is followed by `pg_notify(EVENTS_CHANNEL, ...)`
with a small JSON payload (id, kind, source_id, run_id); listeners read the full row by id.
Notifications are delivered when the transaction commits, so a rolled-back event is never seen.
Publishing must never break the caller's work: failures are logged, not raised.

The read side serves live views: `recent_events` and `events_since` query stored events, and
`EventListener` LISTENs on the channel and yields parsed notices.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal, Protocol, Self, get_args

import psycopg
from psycopg import sql
from psycopg.abc import Query
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import URL, ColumnElement, Connection, Engine, Row, and_, func, or_, select, true

from scrapeline.errors import ScrapelineError
from scrapeline.ops.logging import get_logger
from scrapeline.store import tables as t

log = get_logger(__name__)

EVENTS_CHANNEL = "scrapeline_events"
EventLevel = Literal["debug", "info", "warning", "error"]
LEVELS: tuple[EventLevel, ...] = get_args(EventLevel)
KIND_PREFIX_PATTERN = re.compile(r"[a-z_]+(\.[a-z_]+)*")


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


@dataclass(frozen=True)
class StoredEvent:
    """An event as stored in `ops.event`, with its id. `at` is in UTC."""

    id: int
    at: datetime
    kind: str
    level: str
    source_id: str | None
    run_id: uuid.UUID | None
    job_id: int | None
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Row[Any]) -> StoredEvent:
        at: datetime = row.at if row.at.tzinfo else row.at.replace(tzinfo=UTC)
        return cls(
            id=row.id,
            at=at.astimezone(UTC),
            kind=row.kind,
            level=row.level,
            source_id=row.source_id,
            run_id=row.run_id,
            job_id=row.job_id,
            message=row.message,
            data=dict(row.data or {}),
        )


class EventNotice(BaseModel):
    """One notification payload: enough to decide who wants the event, not the event itself."""

    model_config = ConfigDict(frozen=True, strict=True)

    id: int = Field(gt=0)
    kind: str
    source_id: str | None = None
    run_id: uuid.UUID | None = None


def parse_notice(payload: str) -> EventNotice | None:
    """Parse a notification payload. A malformed one is logged and gives None."""
    try:
        return EventNotice.model_validate_json(payload)
    except ValidationError as exc:  # also raised for invalid JSON
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'payload'}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        log.warning("event.notice_malformed", payload=payload[:200], error=problems)
        return None


@dataclass(frozen=True)
class EventFilter:
    """Which events a reader wants; unset fields match everything.

    `kind_prefix` matches whole dotted segments: "run" matches run.started but not runner.x.
    `min_level` keeps events of that level and the more severe ones.
    """

    source_id: str | None = None
    run_id: uuid.UUID | None = None
    kind_prefix: str | None = None
    min_level: EventLevel | None = None

    def __post_init__(self) -> None:
        if self.kind_prefix is not None and not KIND_PREFIX_PATTERN.fullmatch(self.kind_prefix):
            raise ValueError(f"kind prefix must look like run or run.started: {self.kind_prefix!r}")
        if self.min_level is not None and self.min_level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}: {self.min_level!r}")

    @property
    def levels(self) -> tuple[EventLevel, ...]:
        return LEVELS if self.min_level is None else LEVELS[LEVELS.index(self.min_level) :]

    def may_match(self, event: StoredEvent | EventNotice) -> bool:
        """Everything but the level, which a notice does not carry."""
        prefix = self.kind_prefix
        return (
            (self.source_id is None or event.source_id == self.source_id)
            and (self.run_id is None or event.run_id == self.run_id)
            and (prefix is None or event.kind == prefix or event.kind.startswith(prefix + "."))
        )

    def matches(self, event: StoredEvent) -> bool:
        return self.may_match(event) and event.level in self.levels

    def where(self) -> ColumnElement[bool]:
        """The same filter as a condition on `ops.event`."""
        c = t.event.c
        conditions: list[ColumnElement[bool]] = []
        if self.source_id is not None:
            conditions.append(c.source_id == self.source_id)
        if self.run_id is not None:
            conditions.append(c.run_id == self.run_id)
        if self.kind_prefix is not None:
            # autoescape: "_" is legal in kinds and would otherwise be a LIKE wildcard.
            dotted = c.kind.startswith(self.kind_prefix + ".", autoescape=True)
            conditions.append(or_(c.kind == self.kind_prefix, dotted))
        if self.min_level is not None:
            conditions.append(c.level.in_(self.levels))
        return and_(true(), *conditions)


def recent_events(
    conn: Connection,
    *,
    after_id: int | None = None,
    source_id: str | None = None,
    run_id: uuid.UUID | None = None,
    kind_prefix: str | None = None,
    min_level: EventLevel | None = None,
    limit: int = 50,
) -> list[StoredEvent]:
    """The newest `limit` matching events after `after_id`, ordered by id (oldest first)."""
    wanted = EventFilter(source_id, run_id, kind_prefix, min_level)
    query = select(t.event).where(wanted.where())
    if after_id is not None:
        query = query.where(t.event.c.id > after_id)
    rows = conn.execute(query.order_by(t.event.c.id.desc()).limit(limit)).all()
    return [StoredEvent.from_row(row) for row in reversed(rows)]


def events_since(conn: Connection, after_id: int, *, limit: int = 500) -> list[StoredEvent]:
    """The first `limit` events after `after_id` in id order; pass the last id back to page."""
    query = select(t.event).where(t.event.c.id > after_id).order_by(t.event.c.id).limit(limit)
    return [StoredEvent.from_row(row) for row in conn.execute(query)]


def events_by_id(conn: Connection, ids: Sequence[int]) -> list[StoredEvent]:
    query = select(t.event).where(t.event.c.id.in_(ids)).order_by(t.event.c.id)
    return [StoredEvent.from_row(row) for row in conn.execute(query)]


def latest_event_id(conn: Connection) -> int:
    """The highest event id, or 0 when there are none."""
    latest: int = conn.execute(select(func.coalesce(func.max(t.event.c.id), 0))).scalar_one()
    return latest


LISTENER_APPLICATION = "scrapeline-live"


class ListenerStalledError(ScrapelineError):
    """The database did not answer on the listening connection in time; it has been closed."""


def listen_conninfo(url: URL) -> str:
    """A libpq connection string for a SQLAlchemy URL such as postgresql+psycopg://...

    libpq does not understand the +driver suffix, so the URL is taken apart, not reused.
    """
    params: dict[str, Any] = url.translate_connect_args(username="user", database="dbname")
    for key, value in url.query.items():
        params[key] = value if isinstance(value, str) else value[-1]
    return make_conninfo(**{key: str(value) for key, value in params.items()})


async def _execute_within(conn: psycopg.AsyncConnection[Any], query: Query, timeout: float) -> None:
    """Run `query`; without an answer in `timeout` seconds, close the connection and raise
    ListenerStalledError.

    Only a deadline notices a stall once a query is in flight: TCP keepalives do not run
    then, and a proxy that stopped forwarding still acknowledges every packet.
    """
    pending = asyncio.ensure_future(conn.execute(query))
    try:
        done, _ = await asyncio.wait({pending}, timeout=timeout)
    finally:
        if not pending.done():  # timed out, or the caller was cancelled
            # Closing first leaves psycopg nothing to cancel on the server, which it would
            # otherwise try for up to 10 s; the query then ends at once.
            await conn.close()
            pending.cancel()
            await asyncio.wait({pending})
    if pending not in done:
        if not pending.cancelled():
            pending.exception()  # marks it retrieved; the stall is the error to report
        raise ListenerStalledError(f"the database did not answer within {timeout:g} s")
    pending.result()


class EventListener:
    """One autocommit connection that LISTENs on the events channel.

    async with await EventListener.connect(conninfo) as listener:
        async for notice in listener.notices():
            ...
    """

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn

    @classmethod
    async def connect(
        cls, conninfo: str, *, connect_timeout: int = 5, listen_timeout: float = 5.0
    ) -> EventListener:
        conn: psycopg.AsyncConnection[Any] = await psycopg.AsyncConnection.connect(
            conninfo,
            autocommit=True,
            connect_timeout=connect_timeout,
            application_name=LISTENER_APPLICATION,
        )
        try:
            listen = sql.SQL("LISTEN {}").format(sql.Identifier(EVENTS_CHANNEL))
            await _execute_within(conn, listen, listen_timeout)
        except BaseException:
            await conn.close()
            raise
        return cls(conn)

    @property
    def backend_pid(self) -> int:
        return self._conn.info.backend_pid

    async def notices(
        self, *, ping_every: float = 15.0, ping_timeout: float = 5.0
    ) -> AsyncIterator[EventNotice]:
        """Parsed notices as they arrive. Raises when the connection is lost or stalls.

        A connection quiet for `ping_every` seconds is pinged, and one that does not answer
        within `ping_timeout` seconds is closed with ListenerStalledError. So a dead or stuck
        connection is noticed within `ping_every + ping_timeout` seconds, never left waiting
        for notifications that cannot arrive.
        """
        while True:
            quiet = True
            async for notify in self._conn.notifies(timeout=ping_every):
                quiet = False
                if (notice := parse_notice(notify.payload)) is not None:
                    yield notice
            if quiet:
                await _execute_within(self._conn, "SELECT 1", ping_timeout)

    async def aclose(self) -> None:
        await self._conn.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
