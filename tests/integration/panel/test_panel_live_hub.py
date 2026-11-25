"""The event hub over real LISTEN/NOTIFY: delivery, filters, rollback, reconnect, shutdown.

Every test starts from an empty database (`clean_db`), so event ids start at 1.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from scrapeline.ops.events import (
    Event,
    EventFilter,
    PostgresEventBus,
    StoredEvent,
    events_since,
    latest_event_id,
    publish_in,
    recent_events,
)
from scrapeline.serve.panel import live
from scrapeline.serve.panel.app import create_app
from scrapeline.serve.panel.live import EventHub, Subscription, event_hub
from tests.integration.panel.conftest import panel_settings

pytestmark = pytest.mark.integration

RUN = uuid.UUID("6f1c3a52-9d1e-4c4b-8a8e-2b7f0c1d2e3f")
TIMEOUT = 5.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def publish(engine: Engine, kind: str = "run.started", **fields: Any) -> int:
    """Publish one committed event and return its id."""
    with engine.begin() as conn:
        event_id = publish_in(conn, Event(kind=kind, **fields))
    assert event_id is not None
    return event_id


async def apublish(engine: Engine, kind: str = "run.started", **fields: Any) -> int:
    return await asyncio.to_thread(publish, engine, kind, **fields)


async def receive(subscription: Subscription, count: int) -> list[StoredEvent]:
    """The next `count` events, failing fast instead of hanging when they do not come."""
    events: list[StoredEvent] = []
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while len(events) < count:
        remaining = deadline - asyncio.get_running_loop().time()
        event = await subscription.get(max(remaining, 0))
        if event is None:
            raise AssertionError(f"expected {count} events, got {[e.id for e in events]}")
        events.append(event)
    return events


async def nothing_more(subscription: Subscription, wait: float = 0.3) -> bool:
    return await subscription.get(wait) is None


async def eventually(condition: Callable[[], bool], what: str) -> None:
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for: {what}")
        await asyncio.sleep(0.02)


def backend_alive(engine: Engine, pid: int) -> bool:
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
        ).scalar_one()
    return bool(found)


@pytest.mark.anyio
async def test_events_reach_subscribers_in_order_and_filtered(clean_db: Engine) -> None:
    hub = EventHub(clean_db, backoff=(0.05, 0.5))
    try:
        everything = await hub.subscribe(EventFilter())
        shop = await hub.subscribe(EventFilter(source_id="shop-sim"))
        one_run = await hub.subscribe(EventFilter(run_id=RUN))
        records = await hub.subscribe(EventFilter(kind_prefix="records"))
        errors = await hub.subscribe(EventFilter(min_level="error"))

        bus = PostgresEventBus(clean_db)
        published = [
            Event(kind="run.started", source_id="shop-sim", run_id=RUN),
            Event(kind="records.loaded", source_id="shop-sim", run_id=RUN),
            Event(kind="run.started", source_id="legistar-testcity"),
            Event(kind="run.failed", level="error", source_id="shop-sim", run_id=RUN),
            Event(kind="site.classified", message="no source"),
        ]
        for event in published:
            await asyncio.to_thread(bus.publish, event)

        assert [e.id for e in await receive(everything, 5)] == [1, 2, 3, 4, 5]
        assert [e.id for e in await receive(shop, 3)] == [1, 2, 4]
        assert [e.id for e in await receive(one_run, 3)] == [1, 2, 4]
        assert [e.id for e in await receive(records, 1)] == [2]
        assert [(e.id, e.kind, e.level) for e in await receive(errors, 1)] == [
            (4, "run.failed", "error")
        ]
        for subscription in (everything, shop, one_run, records, errors):
            assert await nothing_more(subscription, 0.1)
    finally:
        await hub.stop()


@pytest.mark.anyio
async def test_a_rolled_back_event_is_never_delivered(clean_db: Engine) -> None:
    hub = EventHub(clean_db)
    try:
        subscription = await hub.subscribe(EventFilter())

        def publish_then_roll_back() -> None:
            with pytest.raises(RuntimeError), clean_db.begin() as conn:
                assert publish_in(conn, Event(kind="run.started", message="rolled back"))
                raise RuntimeError("the caller's work failed")

        await asyncio.to_thread(publish_then_roll_back)
        marker = await apublish(clean_db, "run.finished", message="committed")
        [delivered] = await receive(subscription, 1)
        assert (delivered.id, delivered.message) == (marker, "committed")
        assert await nothing_more(subscription)
    finally:
        await hub.stop()


@pytest.mark.anyio
async def test_malformed_and_dangling_notices_are_skipped(clean_db: Engine) -> None:
    hub = EventHub(clean_db)
    try:
        subscription = await hub.subscribe(EventFilter())

        def notify(payload: str) -> None:
            with clean_db.begin() as conn:
                conn.execute(text("SELECT pg_notify('scrapeline_events', :p)"), {"p": payload})

        await asyncio.to_thread(notify, "not json at all")
        await asyncio.to_thread(notify, '{"id": "7", "kind": "run.started"}')
        # Well formed, but no such row: nothing to deliver.
        await asyncio.to_thread(notify, '{"id": 999999, "kind": "run.started"}')
        real = await apublish(clean_db, message="after the noise")
        [delivered] = await receive(subscription, 1)
        assert delivered.id == real and delivered.message == "after the noise"
    finally:
        await hub.stop()


@pytest.mark.anyio
async def test_hub_reconnects_and_delivers_events_committed_while_it_was_down(
    clean_db: Engine,
) -> None:
    # A one second pause before reconnecting leaves a window to commit events in.
    hub = EventHub(clean_db, backoff=(1.0, 1.0))
    try:
        subscription = await hub.subscribe(EventFilter(source_id="shop-sim"))
        before = await apublish(clean_db, source_id="shop-sim", message="before")
        assert [e.id for e in await receive(subscription, 1)] == [before]

        pid = await kill_listener(hub, clean_db)

        during = [
            await apublish(clean_db, source_id="shop-sim", message="during 1"),
            await apublish(clean_db, source_id="other", message="filtered out"),
            await apublish(clean_db, source_id="shop-sim", message="during 2"),
        ]
        assert not hub.listening, "the hub reconnected too early for this test to mean anything"

        caught_up = await receive(subscription, 2)
        assert [e.id for e in caught_up] == [during[0], during[2]]
        assert hub.listening and hub.listener_pid not in (None, pid)

        after = await apublish(clean_db, source_id="shop-sim", message="after")
        assert [e.id for e in await receive(subscription, 1)] == [after]
        assert await nothing_more(subscription)
    finally:
        await hub.stop()


def _terminate(engine: Engine, pid: int) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}).scalar_one()


async def kill_listener(hub: EventHub, engine: Engine) -> int:
    """Terminate the hub's LISTEN backend and wait until the hub notices. Returns the old pid."""
    pid = hub.listener_pid
    assert pid is not None
    await asyncio.to_thread(_terminate, engine, pid)
    await eventually(lambda: not hub.listening, "the hub notices its connection died")
    return pid


@pytest.mark.anyio
async def test_an_older_id_committed_during_an_outage_is_still_delivered(
    clean_db: Engine,
) -> None:
    # Ids are taken at insert time: a long transaction commits id 1 after id 2 was delivered.
    # The catch-up re-reads ids below the last one dispatched, so id 1 is not lost.
    hub = EventHub(clean_db, backoff=(1.0, 1.0))
    inserted, commit = threading.Event(), threading.Event()

    def long_transaction() -> int:
        with clean_db.begin() as conn:
            event_id = publish_in(conn, Event(kind="run.finished", message="slow commit"))
            inserted.set()
            assert commit.wait(TIMEOUT), "the test never released the transaction"
        assert event_id is not None
        return event_id

    try:
        subscription = await hub.subscribe(EventFilter())
        slow = asyncio.ensure_future(asyncio.to_thread(long_transaction))
        assert await asyncio.to_thread(inserted.wait, TIMEOUT)
        newer = await apublish(clean_db, message="newer")
        assert [e.id for e in await receive(subscription, 1)] == [newer]

        await kill_listener(hub, clean_db)
        commit.set()
        older = await slow
        assert older < newer
        assert hub.listener_pid is None, "the hub reconnected before the commit"

        [caught_up] = await receive(subscription, 1)
        assert (caught_up.id, caught_up.message) == (older, "slow commit")
        assert await nothing_more(subscription)
    finally:
        commit.set()
        await hub.stop()


@pytest.mark.anyio
async def test_events_from_before_the_hub_listened_are_not_sent_on_reconnect(
    clean_db: Engine,
) -> None:
    # Subscribers replay older events themselves; the hub must not send them a second time.
    before = await apublish(clean_db, message="before the hub")
    hub = EventHub(clean_db, backoff=(0.05, 0.05))
    try:
        subscription = await hub.subscribe(EventFilter())
        first = await apublish(clean_db, message="first")
        assert [e.id for e in await receive(subscription, 1)] == [first]

        old_pid = await kill_listener(hub, clean_db)
        await eventually(
            lambda: hub.listening and hub.listener_pid not in (None, old_pid), "the hub reconnects"
        )
        after = await apublish(clean_db, message="after the reconnect")
        assert [e.id for e in await receive(subscription, 1)] == [after]
        assert before < first < after
        assert await nothing_more(subscription)
    finally:
        await hub.stop()


@pytest.mark.anyio
async def test_stop_cancels_the_hub_and_closes_its_connection(clean_db: Engine) -> None:
    hub = EventHub(clean_db)
    subscription = await hub.subscribe(EventFilter())
    pid, task = hub.listener_pid, hub._task
    assert pid is not None and task is not None
    await hub.stop()
    assert task.done() and subscription.closed_reason == "shutdown"
    assert not hub.listening and hub.subscriber_count == 0
    await eventually(lambda: not backend_alive(clean_db, pid), "the listener's backend exits")


@pytest.mark.anyio
async def test_app_shutdown_stops_its_hub(clean_db: Engine, tmp_path: Path) -> None:
    app = create_app(engine=clean_db, settings=panel_settings(tmp_path))
    hub = event_hub(app)
    assert event_hub(app) is hub
    subscription = await hub.subscribe(EventFilter())
    pid = hub.listener_pid
    assert pid is not None
    await live.shutdown(app)
    assert subscription.closed_reason == "shutdown"
    await eventually(lambda: not backend_alive(clean_db, pid), "the listener's backend exits")


def test_recent_events_replay_the_newest_matching_events_oldest_first(clean_db: Engine) -> None:
    ids = [
        publish(clean_db, "run.started", source_id="shop-sim", run_id=RUN),
        publish(clean_db, "records.loaded", source_id="shop-sim", run_id=RUN),
        publish(clean_db, "run.started", source_id="other"),
        publish(clean_db, "run.failed", source_id="shop-sim", run_id=RUN, level="error"),
        publish(clean_db, "checks.done", source_id="shop-sim", run_id=RUN, level="warning"),
    ]
    with clean_db.connect() as conn:
        assert [e.id for e in recent_events(conn)] == ids
        assert [e.id for e in recent_events(conn, limit=2)] == ids[3:]
        assert [e.id for e in recent_events(conn, after_id=ids[1])] == ids[2:]
        assert [e.id for e in recent_events(conn, after_id=ids[0], limit=2)] == ids[3:]
        assert [e.id for e in recent_events(conn, source_id="other")] == [ids[2]]
        assert [e.id for e in recent_events(conn, run_id=RUN, kind_prefix="run")] == [
            ids[0],
            ids[3],
        ]
        assert [e.id for e in recent_events(conn, min_level="warning")] == ids[3:]
        assert [e.id for e in events_since(conn, ids[0], limit=2)] == ids[1:3]
        assert latest_event_id(conn) == ids[-1]
        newest = recent_events(conn, limit=1)[0]
    assert newest.at.utcoffset() is not None and newest.at.utcoffset().total_seconds() == 0
    assert newest.data == {} and newest.level == "warning"


def test_kind_prefix_is_not_a_like_pattern(clean_db: Engine) -> None:
    wanted = publish(clean_db, "run_x.started")
    publish(clean_db, "runax.started")
    publish(clean_db, "run_xy.started")
    with clean_db.connect() as conn:
        assert [e.id for e in recent_events(conn, kind_prefix="run_x")] == [wanted]


def test_an_empty_event_table_reads_as_empty(clean_db: Engine) -> None:
    with clean_db.connect() as conn:
        assert recent_events(conn) == []
        assert events_since(conn, 0) == []
        assert latest_event_id(conn) == 0
