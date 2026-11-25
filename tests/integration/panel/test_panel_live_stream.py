"""/events/stream end to end: a real uvicorn server, a real HTTP client, real LISTEN/NOTIFY.

TestClient buffers whole responses, so an endless stream needs a real server. Every read has
a timeout, so a broken stream fails the test instead of hanging it.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import Engine

from scrapeline.ops.events import Event, publish_in
from scrapeline.serve.panel.app import create_app
from scrapeline.serve.panel.live import EventHub
from tests.integration.panel.conftest import panel_settings

pytestmark = pytest.mark.integration

READ_TIMEOUT = 5.0


@dataclass
class Server:
    url: str
    app: FastAPI
    uvicorn: uvicorn.Server
    thread: threading.Thread

    @property
    def hub(self) -> EventHub:
        hub: EventHub = self.app.state.event_hub
        return hub

    def stop(self) -> None:
        self.uvicorn.should_exit = True
        self.thread.join(10)
        assert not self.thread.is_alive(), "the server did not shut down"


@pytest.fixture
def server(clean_db: Engine, tmp_path: Path) -> Iterator[Server]:
    settings = panel_settings(tmp_path).model_copy(update={"panel_allowed_hosts": ["127.0.0.1"]})
    app = create_app(engine=clean_db, settings=settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    # The graceful timeout bounds shutdown while a stream is still open.
    config = uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=1)
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: instance.started, "uvicorn starts")
    running = Server(f"http://127.0.0.1:{port}", app, instance, thread)
    yield running
    if thread.is_alive():
        running.stop()
    sock.close()


def wait_for(condition: Callable[[], bool], what: str, timeout: float = READ_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for: {what}")
        time.sleep(0.02)


def publish(engine: Engine, kind: str, **fields: Any) -> int:
    with engine.begin() as conn:
        event_id = publish_in(conn, Event(kind=kind, **fields))
    assert event_id is not None
    return event_id


@dataclass
class SSEReader:
    """Reads Server-Sent Events from a line iterator the way a browser does."""

    lines: Iterator[str]
    last_id: str | None = None
    retry: int | None = None
    comments: list[str] = field(default_factory=list)

    def next_message(self) -> dict[str, Any]:
        data: list[str] = []
        event = ""
        for line in self.lines:
            if not line:
                if data:
                    return {
                        "id": self.last_id,
                        "event": event or "message",
                        "data": "\n".join(data),
                    }
                event = ""
                continue
            if line.startswith(":"):
                self.comments.append(line)
                continue
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "data":
                data.append(value)
            elif name == "event":
                event = value
            elif name == "id":
                self.last_id = value
            elif name == "retry":
                self.retry = int(value)
        raise AssertionError("the stream ended")


def client(server: Server) -> httpx.Client:
    return httpx.Client(base_url=server.url, timeout=httpx.Timeout(READ_TIMEOUT))


def test_stream_replays_then_pushes_live_events_as_sse(server: Server, clean_db: Engine) -> None:
    replayed = publish(clean_db, "run.started", source_id="shop-sim", message="first <b>run</b>")
    publish(clean_db, "run.started", source_id="other", message="filtered out")

    with (
        client(server) as http,
        http.stream("GET", "/events/stream", params={"source_id": "shop-sim"}) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        sse = SSEReader(response.iter_lines())

        reset = sse.next_message()
        assert sse.retry == 3000
        assert (reset["id"], reset["event"], reset["data"]) == (None, "reset", "")
        first = sse.next_message()
        assert (first["id"], first["event"]) == (str(replayed), "ops")
        assert first["data"].startswith("<tr>") and first["data"].endswith("</tr>")
        assert "first &lt;b&gt;run&lt;/b&gt;" in first["data"]
        assert "\n" in first["data"], "a multi-line row arrives whole"

        status = sse.next_message()
        assert status["event"] == "status" and "latest 1 event" in status["data"]

        live = publish(clean_db, "records.loaded", source_id="shop-sim", message="live one")
        publish(clean_db, "records.loaded", source_id="other", message="not for this stream")
        pushed = sse.next_message()
        assert (pushed["id"], pushed["event"]) == (str(live), "ops")
        assert "live one" in pushed["data"] and "<code>records.loaded</code>" in pushed["data"]
        assert server.hub.subscriber_count == 1

    wait_for(lambda: server.hub.subscriber_count == 0, "the closed stream is unsubscribed")


def test_reconnect_with_last_event_id_replays_only_what_was_missed(
    server: Server, clean_db: Engine
) -> None:
    seen = publish(clean_db, "run.started", source_id="shop-sim", message="seen")
    missed = [
        publish(clean_db, "records.loaded", source_id="shop-sim", message="missed 1"),
        publish(clean_db, "run.finished", source_id="shop-sim", message="missed 2"),
    ]
    with (
        client(server) as http,
        http.stream("GET", "/events/stream", headers={"Last-Event-ID": str(seen)}) as response,
    ):
        sse = SSEReader(response.iter_lines())
        # No reset: the browser keeps the rows it has and gets only the missed ones.
        replay = [sse.next_message(), sse.next_message()]
        assert [(m["id"], m["event"]) for m in replay] == [(str(i), "ops") for i in missed]
        status = sse.next_message()
        assert status["event"] == "status" and "caught up 2 missed events" in status["data"]


def test_invalid_filters_are_refused_before_streaming(server: Server) -> None:
    with client(server) as http:
        response = http.get("/events/stream", params={"run_id": "not-a-uuid"})
    assert response.status_code == 422
    assert "run_id" in response.text


def test_open_stream_ends_when_the_server_shuts_down(server: Server, clean_db: Engine) -> None:
    publish(clean_db, "run.started", source_id="shop-sim")
    with client(server) as http, http.stream("GET", "/events/stream") as response:
        sse = SSEReader(response.iter_lines())
        assert [sse.next_message()["event"] for _ in range(3)] == ["reset", "ops", "status"]
        hub = server.hub
        threading.Thread(target=server.stop, daemon=True).start()
        # Ends cleanly or is cut off; a stream that stays open fails with a read timeout.
        with pytest.raises((AssertionError, httpx.RemoteProtocolError)):
            sse.next_message()
    wait_for(lambda: not server.thread.is_alive(), "the server stops")
    assert hub.subscriber_count == 0 and not hub.listening
