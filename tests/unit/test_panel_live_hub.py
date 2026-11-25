"""Event filters, notice parsing and the hub's fan-out, without a database."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.dialects import postgresql

from scrapeline.ops.events import (
    LEVELS,
    EventFilter,
    StoredEvent,
    listen_conninfo,
    parse_notice,
)
from scrapeline.serve.panel import live
from scrapeline.serve.panel.app import create_app
from scrapeline.serve.panel.live import (
    EventHub,
    LiveUnavailableError,
    TooManyStreamsError,
)
from scrapeline.settings import Settings

RUN = uuid.UUID("6f1c3a52-9d1e-4c4b-8a8e-2b7f0c1d2e3f")
OTHER_RUN = uuid.UUID("0b0e7a52-1111-4c4b-8a8e-2b7f0c1d2e3f")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def unreachable() -> Engine:
    # Port 1 refuses connections at once.
    return create_engine("postgresql+psycopg://nobody:secret@127.0.0.1:1/none")


def stored(event_id: int, **overrides: Any) -> StoredEvent:
    values: dict[str, Any] = {
        "id": event_id,
        "at": datetime(2026, 11, 25, 9, 30, tzinfo=UTC),
        "kind": "run.started",
        "level": "info",
        "source_id": "shop-sim",
        "run_id": RUN,
        "job_id": None,
        "message": "",
        "data": {},
    }
    values.update(overrides)
    return StoredEvent(**values)


class OfflineHub(EventHub):
    """A hub that listens to nothing: subscribers and dispatch work, the database is never used."""

    async def _run(self) -> None:
        self._listening.set()
        await asyncio.Event().wait()


def test_kind_prefix_matches_whole_segments() -> None:
    run = EventFilter(kind_prefix="run")
    assert run.matches(stored(1, kind="run.started"))
    assert run.matches(stored(2, kind="run.failed"))
    assert not run.matches(stored(3, kind="runner.started"))
    assert not run.matches(stored(4, kind="records.loaded"))
    exact = EventFilter(kind_prefix="records.loaded")
    assert exact.matches(stored(5, kind="records.loaded"))
    assert not exact.matches(stored(6, kind="records.loaded_late"))


def test_source_run_and_level_filters() -> None:
    assert EventFilter(source_id="shop-sim").matches(stored(1))
    assert not EventFilter(source_id="other").matches(stored(1))
    assert not EventFilter(source_id="shop-sim").matches(stored(1, source_id=None))
    assert EventFilter(run_id=RUN).matches(stored(1))
    assert not EventFilter(run_id=OTHER_RUN).matches(stored(1))
    warnings = EventFilter(min_level="warning")
    kept = [level for level in LEVELS if warnings.matches(stored(1, level=level))]
    assert kept == ["warning", "error"]
    assert EventFilter().matches(stored(1, level="debug"))


def test_a_notice_may_match_whatever_its_level_turns_out_to_be() -> None:
    notice = parse_notice(
        '{"id": 1, "kind": "run.failed", "source_id": "shop-sim", "run_id": null}'
    )
    assert notice is not None
    assert EventFilter(min_level="error", kind_prefix="run").may_match(notice)
    assert not EventFilter(source_id="other").may_match(notice)


@pytest.mark.parametrize(
    "prefix", ["Run", "run.", "run..x", "run*", "run%", "", "run started", "run\n"]
)
def test_invalid_kind_prefixes_are_refused(prefix: str) -> None:
    with pytest.raises(ValueError):
        EventFilter(kind_prefix=prefix)


def test_invalid_level_is_refused() -> None:
    with pytest.raises(ValueError):
        EventFilter(min_level="loud")  # type: ignore[arg-type]


def test_kind_prefix_sql_escapes_like_wildcards() -> None:
    sql = str(EventFilter(kind_prefix="run_x").where().compile(dialect=postgresql.dialect()))
    assert "LIKE" in sql and "ESCAPE" in sql


def test_notice_payloads_are_parsed() -> None:
    notice = parse_notice(
        f'{{"id": 42, "kind": "records.loaded", "source_id": "shop-sim", "run_id": "{RUN}"}}'
    )
    assert notice is not None
    assert (notice.id, notice.kind, notice.source_id, notice.run_id) == (
        42,
        "records.loaded",
        "shop-sim",
        RUN,
    )
    bare = parse_notice('{"id": 1, "kind": "site.classified", "source_id": null, "run_id": null}')
    assert bare is not None and bare.source_id is None and bare.run_id is None


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[1, 2]",
        '"just a string"',
        '{"kind": "run.started"}',
        '{"id": "5", "kind": "run.started"}',
        '{"id": true, "kind": "run.started"}',
        '{"id": -1, "kind": "run.started"}',
        '{"id": 5, "kind": 7}',
        '{"id": 5, "kind": "run.started", "run_id": "not-a-uuid"}',
        '{"id": 5, "kind": "run.started", "source_id": 12}',
    ],
)
def test_malformed_notices_are_logged_and_skipped(
    payload: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert parse_notice(payload) is None
    assert "event.notice_malformed" in capsys.readouterr().err


def test_conninfo_drops_the_sqlalchemy_driver() -> None:
    url = make_url("postgresql+psycopg://user:p%40ss@db.internal:5433/panel?sslmode=require")
    conninfo = listen_conninfo(url)
    assert "+psycopg" not in conninfo and "postgresql" not in conninfo
    for part in ("host=db.internal", "port=5433", "dbname=panel", "user=user", "sslmode=require"):
        assert part in conninfo
    assert "p@ss" in conninfo


@pytest.mark.anyio
async def test_dispatch_follows_each_subscribers_filter(unreachable: Engine) -> None:
    hub = OfflineHub(unreachable)
    everything = await hub.subscribe(EventFilter())
    shop_errors = await hub.subscribe(EventFilter(source_id="shop-sim", min_level="error"))
    other_run = await hub.subscribe(EventFilter(run_id=OTHER_RUN))
    hub.dispatch(stored(1))
    hub.dispatch(stored(2, level="error", kind="run.failed"))
    hub.dispatch(stored(3, run_id=OTHER_RUN, source_id="legistar"))
    assert [e.id for e in await drain(everything)] == [1, 2, 3]
    assert [e.id for e in await drain(shop_errors)] == [2]
    assert [e.id for e in await drain(other_run)] == [3]
    await hub.stop()


@pytest.mark.anyio
async def test_a_slow_subscriber_is_dropped_without_holding_up_the_others(
    unreachable: Engine,
) -> None:
    hub = OfflineHub(unreachable, queue_size=2)
    slow = await hub.subscribe(EventFilter())
    fast = await hub.subscribe(EventFilter())
    received = []
    for event_id in (1, 2, 3, 4):
        hub.dispatch(stored(event_id))
        received += await drain(fast)
    assert [e.id for e in received] == [1, 2, 3, 4]
    assert slow.closed_reason == "slow"
    assert await slow.get(1) is None
    assert hub.subscriber_count == 1
    await hub.stop()
    assert fast.closed_reason == "shutdown"


@pytest.mark.anyio
async def test_streams_are_capped(unreachable: Engine) -> None:
    hub = OfflineHub(unreachable, max_subscribers=2)
    first = await hub.subscribe(EventFilter())
    await hub.subscribe(EventFilter())
    with pytest.raises(TooManyStreamsError):
        await hub.subscribe(EventFilter())
    hub.unsubscribe(first)
    assert first.closed_reason == "unsubscribed"
    await hub.subscribe(EventFilter())
    await hub.stop()


@pytest.mark.anyio
async def test_stop_closes_everything_and_refuses_new_subscribers(unreachable: Engine) -> None:
    hub = OfflineHub(unreachable)
    subscription = await hub.subscribe(EventFilter())
    task = hub._task
    await hub.stop()
    assert task is not None and task.cancelled()
    assert subscription.closed_reason == "shutdown" and hub.subscriber_count == 0
    with pytest.raises(LiveUnavailableError):
        await hub.subscribe(EventFilter())


@pytest.mark.anyio
async def test_a_subscriber_still_waiting_for_listen_is_refused_once_the_hub_stops(
    unreachable: Engine,
) -> None:
    hub = OfflineHub(unreachable)
    waiting = asyncio.ensure_future(hub.subscribe(EventFilter()))
    await asyncio.sleep(0)  # subscribe starts the hub and waits for LISTEN
    assert not waiting.done() and hub._task is not None
    # The hub starts listening, which wakes the subscriber, but stop() runs before it resumes.
    stopping = asyncio.ensure_future(hub.stop())
    with pytest.raises(LiveUnavailableError):
        await waiting
    await stopping
    assert hub.subscriber_count == 0


@pytest.mark.anyio
async def test_hub_without_a_database_is_unavailable_not_stuck(unreachable: Engine) -> None:
    hub = EventHub(unreachable, ready_timeout=0.3, backoff=(0.05, 0.1))
    with pytest.raises(LiveUnavailableError):
        await hub.subscribe(EventFilter())
    assert not hub.listening
    await hub.stop()


async def drain(subscription: live.Subscription) -> list[StoredEvent]:
    events = []
    while (event := await subscription.get(0.01)) is not None:
        events.append(event)
    return events


@pytest.fixture
def panel(unreachable: Engine) -> Iterator[TestClient]:
    settings = Settings(_env_file=None, panel_allowed_hosts=["testserver"])
    with TestClient(create_app(engine=unreachable, settings=settings)) as client:
        yield client


def app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


@pytest.mark.parametrize(
    "query",
    ["run_id=nope", "kind=Run!", "kind=run%25", "level=loud", "source_id=" + "x" * 101],
)
def test_invalid_stream_filters_get_an_html_422(panel: TestClient, query: str) -> None:
    for path in ("/events/stream", "/live"):
        response = panel.get(f"{path}?{query}")
        assert response.status_code == 422, (path, query)
        assert "text/html" in response.headers["content-type"]


def test_stream_over_the_cap_gets_a_503(panel: TestClient, unreachable: Engine) -> None:
    app_of(panel).state.event_hub = OfflineHub(unreachable, max_subscribers=0)
    response = panel.get("/events/stream")
    assert response.status_code == 503
    assert "Too many live streams" in response.text


def test_stream_without_a_database_gets_a_503(panel: TestClient, unreachable: Engine) -> None:
    app_of(panel).state.event_hub = EventHub(unreachable, ready_timeout=0.2, backoff=(0.05, 0.1))
    response = panel.get("/events/stream", params={"source_id": "shop-sim"})
    assert response.status_code == 503
    assert "Live events are unavailable" in response.text


def test_live_page_without_a_database_gets_a_503(panel: TestClient) -> None:
    response = panel.get("/live")
    assert response.status_code == 503
    assert "Database unavailable" in response.text


@pytest.mark.anyio
async def test_shutdown_without_a_hub_does_nothing(unreachable: Engine) -> None:
    settings = Settings(_env_file=None, panel_allowed_hosts=["testserver"])
    await live.shutdown(create_app(engine=unreachable, settings=settings))
