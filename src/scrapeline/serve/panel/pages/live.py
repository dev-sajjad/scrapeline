"""Live events: the /live page and the Server-Sent Events stream behind every live feed."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from markupsafe import escape
from sqlalchemy import Connection, Engine, select
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from scrapeline.ops.events import LEVELS, EventFilter, StoredEvent, recent_events
from scrapeline.serve.panel.deps import EngineDep, render, templates
from scrapeline.serve.panel.live import (
    LiveUnavailableError,
    TooManyStreamsError,
    event_hub,
    event_stream,
)
from scrapeline.store import tables as t

router = APIRouter()

REPLAY_LIMIT = 50
# A browser that reconnects with Last-Event-ID gets at most this many missed events back.
RESUME_LIMIT = 500
SOURCE_CHOICES = 500
KIND_SUGGESTIONS = (
    "run",
    "run.started",
    "run.finished",
    "run.failed",
    "records.loaded",
    "checks.done",
    "site.classified",
)
EVENT_ID = re.compile(r"[0-9]{1,18}")


def event_filter(
    source_id: Annotated[str, Query(max_length=100)] = "",
    run_id: Annotated[str, Query(max_length=36)] = "",
    kind: Annotated[str, Query(max_length=50)] = "",
    level: Annotated[Literal["", "debug", "info", "warning", "error"], Query()] = "",
) -> EventFilter:
    """Filters from the query string. Empty values, as GET forms send them, mean any."""
    try:
        run = uuid.UUID(run_id) if run_id else None
    except ValueError:
        raise HTTPException(422, "run_id: must be a UUID") from None
    try:
        return EventFilter(source_id or None, run, kind.strip().rstrip(".") or None, level or None)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


FilterDep = Annotated[EventFilter, Depends(event_filter)]


def data_json(event: StoredEvent) -> str:
    """The event's data for people to read, or "" when there is none."""
    if not event.data:
        return ""
    return json.dumps(event.data, indent=2, sort_keys=True, ensure_ascii=False, default=str)


def render_event_row(event: StoredEvent) -> str:
    """One event as a table row: the unit every stream message carries."""
    template = templates.get_template("live/_event_row.html")
    return template.render(event=event, data_json=data_json(event))


def stream_status(replayed: int, *, resumed: bool) -> str:
    """The line above a live feed once the stream has replayed what it had, as HTML."""
    now = datetime.now(UTC).strftime("%H:%M:%S")
    events = "event" if replayed == 1 else "events"
    if resumed:
        text = f"Reconnected at {now} UTC and caught up {replayed} missed {events}."
    elif replayed:
        text = f"Live since {now} UTC. Showing the latest {replayed} {events}; new ones go on top."
    else:
        text = f"Live since {now} UTC. No matching events yet; new ones appear here as they happen."
    return f"<p>{escape(text)}</p>"


def _recent(
    conn: Connection, wanted: EventFilter, after_id: int | None, limit: int
) -> list[StoredEvent]:
    return recent_events(
        conn,
        after_id=after_id,
        source_id=wanted.source_id,
        run_id=wanted.run_id,
        kind_prefix=wanted.kind_prefix,
        min_level=wanted.min_level,
        limit=limit,
    )


def _replay(engine: Engine, wanted: EventFilter, resume_after: int | None) -> list[StoredEvent]:
    limit = REPLAY_LIMIT if resume_after is None else RESUME_LIMIT
    with engine.connect() as conn:
        return _recent(conn, wanted, resume_after, limit)


class EventStreamResponse(StreamingResponse):
    """A text/event-stream response that releases its subscription however the stream ends."""

    media_type = "text/event-stream"

    def __init__(self, content: AsyncGenerator[str, None], release: Callable[[], None]) -> None:
        # no-cache and no proxy buffering: each message must reach the browser at once.
        super().__init__(content, headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        self._content = content
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()
            await self._content.aclose()


@router.get("/events/stream", include_in_schema=False)
async def stream_events(
    request: Request,
    engine: EngineDep,
    wanted: FilterDep,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Matching events as Server-Sent Events: a replay first, then each new one as it commits."""
    resume_after = (
        int(last_event_id) if last_event_id and EVENT_ID.fullmatch(last_event_id) else None
    )
    hub = event_hub(request.app)
    try:
        subscription = await hub.subscribe(wanted)
    except TooManyStreamsError:
        raise HTTPException(503, "Too many live streams are open; close another tab.") from None
    except LiveUnavailableError:
        raise HTTPException(503, "Live events are unavailable: no database connection.") from None
    # Subscribed before the replay is read, so nothing committed in between is missed.
    try:
        replay = await run_in_threadpool(_replay, engine, wanted, resume_after)
    except BaseException:
        hub.unsubscribe(subscription)
        raise
    resumed = resume_after is not None
    body = event_stream(
        subscription,
        replay,
        render_event_row,
        reset=not resumed,  # a resuming browser keeps its rows and gets only what it missed
        status=stream_status(len(replay), resumed=resumed),
    )
    return EventStreamResponse(body, release=partial(hub.unsubscribe, subscription))


@router.get("/live", response_class=HTMLResponse)
def live(request: Request, engine: EngineDep, wanted: FilterDep) -> HTMLResponse:
    with engine.connect() as conn:
        source_ids = list(
            conn.execute(
                select(t.sources.c.id).order_by(t.sources.c.id).limit(SOURCE_CHOICES)
            ).scalars()
        )
        latest = _recent(conn, wanted, None, REPLAY_LIMIT)
    if wanted.source_id and wanted.source_id not in source_ids:
        source_ids.append(wanted.source_id)  # keep the current choice selectable
    return render(
        request,
        "live/index.html",
        {
            "title": "Live events",
            "filters": wanted,
            "source_ids": source_ids,
            "kinds": KIND_SUGGESTIONS,
            "levels": LEVELS,
            "replay_limit": REPLAY_LIMIT,
            # Newest first, as the live table shows them.
            "initial": [(event, data_json(event)) for event in reversed(latest)],
        },
    )
