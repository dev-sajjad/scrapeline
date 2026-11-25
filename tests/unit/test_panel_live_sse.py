"""Server-Sent Events framing, the stream generator and event rows, without a database."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from scrapeline.ops.events import EventFilter, StoredEvent
from scrapeline.serve.panel.deps import templates
from scrapeline.serve.panel.live import (
    Subscription,
    event_stream,
    sse_comment,
    sse_message,
    sse_retry,
)
from scrapeline.serve.panel.pages.live import render_event_row, stream_status

RUN = uuid.UUID("6f1c3a52-9d1e-4c4b-8a8e-2b7f0c1d2e3f")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Messages as a browser dispatches them, following the WHATWG event stream rules."""
    messages: list[dict[str, Any]] = []
    data: list[str] = []
    event, last_id = "", None
    for line in re.split(r"\r\n|\r|\n", text):
        if not line:
            if data:
                messages.append(
                    {"id": last_id, "event": event or "message", "data": "\n".join(data)}
                )
            data, event = [], ""
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            data.append(value)
        elif name == "event":
            event = value
        elif name == "id":
            last_id = value
    return messages


def stored(event_id: int, **overrides: Any) -> StoredEvent:
    values: dict[str, Any] = {
        "id": event_id,
        "at": datetime(2026, 11, 25, 9, 30, 5, tzinfo=UTC),
        "kind": "run.started",
        "level": "info",
        "source_id": "shop-sim",
        "run_id": RUN,
        "job_id": None,
        "message": f"event {event_id}",
        "data": {},
    }
    values.update(overrides)
    return StoredEvent(**values)


def test_single_line_message_has_id_event_and_data() -> None:
    assert sse_message("hello", event="ops", event_id=7) == "id: 7\nevent: ops\ndata: hello\n\n"


def test_multi_line_data_becomes_one_data_field_per_line() -> None:
    framed = sse_message("<tr>\n  <td>a</td>\r\n<td>b</td>\r</tr>", event="ops", event_id=3)
    assert framed.endswith("\n\n") and "\n\n" not in framed[:-2]
    assert framed.split("\n")[2:-2] == [
        "data: <tr>",
        "data:   <td>a</td>",
        "data: <td>b</td>",
        "data: </tr>",
    ]
    [message] = parse_sse(framed)
    assert message == {"id": "3", "event": "ops", "data": "<tr>\n  <td>a</td>\n<td>b</td>\n</tr>"}


def test_blank_lines_in_data_cannot_end_the_message_early() -> None:
    [message] = parse_sse(sse_message("one\n\n\ntwo", event="ops", event_id=1))
    assert message["data"] == "one\n\n\ntwo"


def test_empty_data_is_still_a_message() -> None:
    assert sse_message("") == "data: \n\n"
    assert parse_sse(sse_message("")) == [{"id": None, "event": "message", "data": ""}]


def test_event_name_must_be_one_line() -> None:
    with pytest.raises(ValueError):
        sse_message("x", event="ops\ndata: injected")


def test_comments_and_retry_are_framed() -> None:
    assert sse_comment() == ": keepalive\n\n"
    assert sse_comment("a\nb") == ": a\n: b\n\n"
    assert parse_sse(sse_comment("a\nb")) == []
    assert sse_retry(3000) == "retry: 3000\n\n"


async def collect(stream: Any, until: int, timeout: float = 2.0) -> list[str]:
    chunks: list[str] = []
    for _ in range(until):
        chunks.append(await asyncio.wait_for(anext(stream), timeout))
    return chunks


@pytest.mark.anyio
async def test_stream_replays_then_sends_status_then_live_events() -> None:
    subscription = Subscription(EventFilter(), maxsize=10)
    stream = event_stream(
        subscription, [stored(1), stored(2)], lambda e: f"<tr>{e.id}</tr>", status="ready"
    )
    head = await collect(stream, 4)
    assert head[0] == "retry: 3000\n\n"
    # An event read by the replay and also delivered live is sent once.
    subscription.offer(stored(2))
    subscription.offer(stored(3))
    tail = await collect(stream, 1)
    subscription.close("shutdown")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 1)
    messages = parse_sse("".join(head + tail))
    assert [(m["id"], m["event"], m["data"]) for m in messages] == [
        ("1", "ops", "<tr>1</tr>"),
        ("2", "ops", "<tr>2</tr>"),
        ("2", "status", "ready"),  # a message without id keeps the last id, as browsers do
        ("3", "ops", "<tr>3</tr>"),
    ]


@pytest.mark.anyio
async def test_reset_comes_before_the_replay() -> None:
    subscription = Subscription(EventFilter(), maxsize=10)
    stream = event_stream(subscription, [stored(4)], lambda e: f"<tr>{e.id}</tr>", reset=True)
    chunks = await collect(stream, 3)
    await stream.aclose()
    assert chunks[1] == "event: reset\ndata: \n\n"
    assert [(m["event"], m["data"]) for m in parse_sse("".join(chunks))] == [
        ("reset", ""),
        ("ops", "<tr>4</tr>"),
    ]


@pytest.mark.anyio
async def test_idle_stream_sends_keepalive_comments() -> None:
    subscription = Subscription(EventFilter(), maxsize=10)
    stream = event_stream(subscription, [], str, keepalive=0.01)
    chunks = await collect(stream, 3)
    assert chunks[1:] == [": keepalive\n\n", ": keepalive\n\n"]
    await stream.aclose()


@pytest.mark.anyio
async def test_stream_ends_when_its_subscription_is_closed() -> None:
    subscription = Subscription(EventFilter(), maxsize=10)
    stream = event_stream(subscription, [], str, keepalive=5)
    await collect(stream, 1)
    waiting = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    subscription.close("slow")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, 1)


@pytest.mark.anyio
async def test_full_queue_refuses_and_close_always_wakes_the_reader() -> None:
    subscription = Subscription(EventFilter(), maxsize=2)
    assert subscription.offer(stored(1)) and subscription.offer(stored(2))
    assert not subscription.offer(stored(3))
    subscription.close("slow")
    assert subscription.closed and subscription.closed_reason == "slow"
    assert await subscription.get(1) is None
    assert await subscription.get(1) is None


def test_event_row_escapes_everything_from_the_database() -> None:
    hostile = stored(
        9,
        source_id='x"><script>alert(1)</script>',
        message="<img src=x onerror=alert(2)>",
        data={"title": "</pre><script>alert(3)</script>"},
        level="warning",
    )
    row = render_event_row(hostile)
    assert "<script" not in row and "<img" not in row
    assert "&lt;img src=x onerror=alert(2)&gt;" in row
    assert 'href="/sources/x%22%3E%3Cscript%3Ealert%281%29%3C%2Fscript%3E"' in row
    assert "&lt;/pre&gt;&lt;script&gt;alert(3)" in row
    assert 'class="badge badge-warn">warning<' in row


def test_event_row_shows_time_kind_and_links() -> None:
    row = render_event_row(stored(5, data={"pages": 3}))
    assert '<time datetime="2026-11-25T09:30:05+00:00">2026-11-25 09:30:05</time>' in row
    assert "<code>run.started</code>" in row
    assert '<a href="/sources/shop-sim">shop-sim</a>' in row
    assert f'<a href="/runs/{RUN}"' in row and "run 6f1c3a52…" in row
    assert "<details><summary>Data</summary>" in row
    assert "<details>" not in render_event_row(stored(6, source_id=None, run_id=None))


def test_status_is_escaped_html_for_each_situation() -> None:
    empty = stream_status(0, resumed=False)
    assert empty.startswith("<p>Live since ") and empty.endswith("</p>")
    assert "No matching events yet" in empty
    assert "latest 1 event;" in stream_status(1, resumed=False)
    assert "caught up 3 missed events" in stream_status(3, resumed=True)
    restarted = stream_status(50, resumed=True, restarted=True)
    assert "missed more than 500 events" in restarted and "latest 50 events" in restarted
    assert "caught up" not in restarted


def test_live_feed_macro_builds_an_accessible_stream_url() -> None:
    page = templates.env.from_string(
        '{% from "live/_macros.html" import live_feed %}'
        "{{ live_feed(source_id='a b&c', run_id=run, kind='run') }}"
    ).render(run=RUN)
    assert f'sse-connect="/events/stream?source_id=a+b%26c&amp;run_id={RUN}&amp;kind=run"' in page
    assert '<tbody sse-swap="ops" hx-target="this" hx-swap="afterbegin"' in page
    assert 'sse-swap="reset" hx-target="find tbody" hx-swap="innerHTML"' in page
    assert 'aria-live="polite"' in page and 'role="status"' in page
    assert "<noscript>" in page and 'href="/live?source_id=a+b%26c' in page
    assert "<time" not in page and "No events yet." not in page


def test_live_feed_without_filters_streams_everything() -> None:
    page = templates.env.from_string(
        '{% from "live/_macros.html" import live_feed %}{{ live_feed() }}'
    ).render()
    assert 'sse-connect="/events/stream"' in page


def test_live_feed_renders_initial_rows_or_says_there_are_none() -> None:
    template = templates.env.from_string(
        '{% from "live/_macros.html" import live_feed %}{{ live_feed(initial=rows) }}'
    )
    filled = template.render(rows=[(stored(2), ""), (stored(1), '{"a": 1}')])
    assert filled.index("event 2") < filled.index("event 1")
    assert "&#34;a&#34;: 1" in filled and "No events yet." not in filled
    assert "reload the page to see newer ones" in filled
    assert '<td colspan="4" class="empty">No events yet.</td>' in template.render(rows=[])
