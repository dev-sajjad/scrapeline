"""Live events for the panel: one LISTEN connection per process, fanned out to subscribers.

The hub starts with its first subscriber. For each notice it reads the event row once and
queues it for every subscriber whose filter matches. A subscriber that falls behind is
dropped instead of slowing the others: its stream ends and the browser reconnects with
Last-Event-ID. When the listening connection is lost or stops answering, the hub reconnects
with backoff and catches up from `ops.event`, so events committed during the outage still
reach subscribers.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, Callable, Iterable
from functools import partial
from typing import TypeVar

from fastapi import FastAPI
from sqlalchemy import Connection, Engine

from scrapeline.errors import ScrapelineError
from scrapeline.ops.events import (
    EventFilter,
    EventListener,
    EventNotice,
    StoredEvent,
    events_by_id,
    events_since,
    latest_event_id,
    listen_conninfo,
)
from scrapeline.ops.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

MAX_STREAMS = 50
QUEUE_SIZE = 256
KEEPALIVE_SECONDS = 15.0
RETRY_MS = 3000
# On reconnect the hub re-reads this many ids below the last one it dispatched: ids are taken
# at insert time, so a long transaction can commit an older id after newer ones.
CATCH_UP_OVERLAP = 1000
CATCH_UP_PAGE = 500


class LiveUnavailableError(ScrapelineError):
    """The hub cannot listen for events, usually because the database is unreachable."""


class TooManyStreamsError(ScrapelineError):
    """This process already serves the maximum number of live streams."""


class Subscription:
    """One subscriber: its filter and a bounded queue of matching events."""

    def __init__(self, event_filter: EventFilter, maxsize: int) -> None:
        self.filter = event_filter
        self.closed_reason: str | None = None
        self._queue: asyncio.Queue[StoredEvent | None] = asyncio.Queue(maxsize)

    @property
    def closed(self) -> bool:
        return self.closed_reason is not None

    def offer(self, event: StoredEvent) -> bool:
        """Queue an event without waiting. False when the queue is full."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            return False
        return True

    def close(self, reason: str) -> None:
        """End the subscription; a pending or later `get` returns None at once."""
        if self.closed:
            return
        self.closed_reason = reason
        # Queued events are discarded so the wake-up marker always fits; a reconnecting
        # browser gets them back by replay from its Last-Event-ID.
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)

    async def get(self, timeout: float) -> StoredEvent | None:
        """The next event, or None after `timeout` seconds or once the subscription is closed."""
        if self.closed:
            return None
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None


class EventHub:
    """Fans events from one LISTEN connection out to many subscribers. One per app."""

    def __init__(
        self,
        engine: Engine,
        *,
        max_subscribers: int = MAX_STREAMS,
        queue_size: int = QUEUE_SIZE,
        ready_timeout: float = 5.0,
        backoff: tuple[float, float] = (0.5, 15.0),
        ping_every: float = 15.0,
        ping_timeout: float = 5.0,
    ) -> None:
        self.engine = engine
        self.max_subscribers = max_subscribers
        self.queue_size = queue_size
        self.ready_timeout = ready_timeout
        self.backoff = backoff
        self.ping_every = ping_every
        self.ping_timeout = ping_timeout
        self.listener_pid: int | None = None
        self._conninfo = listen_conninfo(engine.url)
        self._subscribers: set[Subscription] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._listening = asyncio.Event()
        # Events up to `_floor` existed before the hub first listened; subscribers replay those.
        self._floor = 0
        self._last_id: int | None = None
        self._seen: set[int] = set()

    @property
    def listening(self) -> bool:
        return self._listening.is_set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def subscribe(self, event_filter: EventFilter) -> Subscription:
        """A new subscription, returned once the hub is listening.

        Waiting for LISTEN first means a subscriber that replays history afterwards misses
        nothing in between. Raises TooManyStreamsError at the cap, and LiveUnavailableError
        when the hub cannot listen within `ready_timeout` seconds or is stopped.
        """
        if self._stopped:
            raise LiveUnavailableError("the panel is shutting down")
        self._check_capacity()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="scrapeline-live-hub")
        try:
            await asyncio.wait_for(self._listening.wait(), self.ready_timeout)
        except TimeoutError:
            raise LiveUnavailableError("cannot listen for events on the database") from None
        # stop() may have run while this one waited; nothing would ever close its subscription.
        if self._stopped:
            raise LiveUnavailableError("the panel is shutting down")
        self._check_capacity()  # others may have joined while this one waited
        subscription = Subscription(event_filter, self.queue_size)
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription)
        subscription.close("unsubscribed")

    async def stop(self) -> None:
        """Close every subscription and the listening connection."""
        self._stopped = True
        for subscription in list(self._subscribers):
            self._drop(subscription, "shutdown")
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task})
        self._listening.clear()
        self.listener_pid = None

    def _check_capacity(self) -> None:
        if len(self._subscribers) >= self.max_subscribers:
            raise TooManyStreamsError(f"at most {self.max_subscribers} live streams per process")

    async def _run(self) -> None:
        delay = self.backoff[0]
        while True:
            try:
                connecting = EventListener.connect(self._conninfo, listen_timeout=self.ping_timeout)
                async with await connecting as listener:
                    self.listener_pid = listener.backend_pid
                    await self._catch_up()
                    self._listening.set()
                    delay = self.backoff[0]
                    log.info("live.listening", pid=self.listener_pid, db=self.engine.url.database)
                    notices = listener.notices(
                        ping_every=self.ping_every, ping_timeout=self.ping_timeout
                    )
                    async for notice in notices:
                        await self._deliver(notice)
            except Exception as exc:  # whatever broke, the remedy is the same: reconnect
                log.warning("live.listener_lost", error=repr(exc), retry_in=delay)
            self._listening.clear()
            self.listener_pid = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.backoff[1])

    async def _catch_up(self) -> None:
        """Dispatch what was committed while the hub was not listening. LISTEN is active."""
        if self._last_id is None:
            self._floor = self._last_id = await self._read(latest_event_id)
            return
        after = max(self._last_id - CATCH_UP_OVERLAP, self._floor)
        while True:
            page = await self._read(partial(events_since, after_id=after, limit=CATCH_UP_PAGE))
            for event in page:
                if event.id not in self._seen:
                    self.dispatch(event)
            if len(page) < CATCH_UP_PAGE:
                return
            after = page[-1].id

    async def _deliver(self, notice: EventNotice) -> None:
        if notice.id in self._seen:
            return
        # The row is read only when someone may want it. The id is remembered after the
        # read, so an event whose read failed is picked up again by the catch-up.
        if any(sub.filter.may_match(notice) for sub in self._subscribers):
            for event in await self._read(partial(events_by_id, ids=[notice.id])):
                self._fan_out(event)
        self._remember(notice.id)

    def dispatch(self, event: StoredEvent) -> None:
        """Queue `event` for every subscriber whose filter matches; drop those that are full."""
        self._remember(event.id)
        self._fan_out(event)

    def _fan_out(self, event: StoredEvent) -> None:
        for subscription in list(self._subscribers):
            if subscription.filter.matches(event) and not subscription.offer(event):
                log.info("live.subscriber_dropped", reason="slow", event_id=event.id)
                self._drop(subscription, "slow")

    def _drop(self, subscription: Subscription, reason: str) -> None:
        self._subscribers.discard(subscription)
        subscription.close(reason)

    def _remember(self, event_id: int) -> None:
        self._seen.add(event_id)
        self._last_id = max(self._last_id or 0, event_id)
        if len(self._seen) > 2 * CATCH_UP_OVERLAP:
            low = self._last_id - CATCH_UP_OVERLAP
            self._seen = {seen for seen in self._seen if seen > low}

    async def _read(self, query: Callable[[Connection], T]) -> T:
        def run() -> T:
            with self.engine.connect() as conn:
                return query(conn)

        return await asyncio.to_thread(run)


def event_hub(app: FastAPI) -> EventHub:
    """The app's hub, created on first use."""
    hub: EventHub | None = getattr(app.state, "event_hub", None)
    if hub is None:
        hub = EventHub(app.state.engine)
        app.state.event_hub = hub
    return hub


async def shutdown(app: FastAPI) -> None:
    """Stop the event hub if one was started. Called when the app shuts down."""
    hub: EventHub | None = getattr(app.state, "event_hub", None)
    if hub is not None:
        await hub.stop()


_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def sse_message(data: str, *, event: str | None = None, event_id: int | None = None) -> str:
    """One Server-Sent Events message. Every line of `data` gets its own data: field."""
    fields: list[str] = []
    if event_id is not None:
        fields.append(f"id: {event_id}")
    if event is not None:
        if _LINE_BREAK.search(event):
            raise ValueError("an SSE event name must be a single line")
        fields.append(f"event: {event}")
    fields.extend(f"data: {line}" for line in _LINE_BREAK.split(data))
    return "\n".join(fields) + "\n\n"


def sse_comment(text: str = "keepalive") -> str:
    """A comment: ignored by browsers, but it keeps idle proxies from closing the stream."""
    return "".join(f": {line}\n" for line in _LINE_BREAK.split(text)) + "\n"


def sse_retry(milliseconds: int) -> str:
    """Tells the browser how long to wait before reconnecting."""
    return f"retry: {milliseconds}\n\n"


async def event_stream(
    subscription: Subscription,
    replay: Iterable[StoredEvent],
    render: Callable[[StoredEvent], str],
    *,
    reset: bool = False,
    status: str | None = None,
    keepalive: float = KEEPALIVE_SECONDS,
    retry_ms: int = RETRY_MS,
) -> AsyncGenerator[str, None]:
    """The body of an SSE response: replayed events, then live ones as they arrive.

    Events are sent as `event: ops` with their id, so a reconnecting browser resumes from
    Last-Event-ID. With `reset`, an empty `event: reset` comes first so the page drops the
    rows it shows before the replay brings them back. `status` (rendered HTML) goes out once
    the replay is done, as `event: status`. Idle periods get a keepalive comment. Ends when
    the subscription closes.
    """
    yield sse_retry(retry_ms)
    if reset:
        yield sse_message("", event="reset")
    replayed: set[int] = set()
    for past in replay:
        replayed.add(past.id)
        yield sse_message(render(past), event="ops", event_id=past.id)
    if status is not None:
        yield sse_message(status, event="status")
    while True:
        event = await subscription.get(keepalive)
        if event is None:
            if subscription.closed:
                return
            yield sse_comment()
        elif event.id not in replayed:  # it may have committed while the replay was read
            yield sse_message(render(event), event="ops", event_id=event.id)
