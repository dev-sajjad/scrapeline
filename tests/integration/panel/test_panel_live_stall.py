"""A listening connection that stalls without closing, as behind a paused database or a stuck
proxy. TCP keepalives do not notice it, because the proxy still acknowledges every packet.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool

from scrapeline.ops.events import Event, EventFilter, publish_in
from scrapeline.serve.panel.live import EventHub

pytestmark = pytest.mark.integration

TIMEOUT = 5.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class StallingProxy:
    """A TCP proxy to the database. `freeze()` holds every byte of the connections open at
    that moment without closing them; later connections pass through.
    """

    def __init__(self, host: str, port: int) -> None:
        self.target = (host, port)
        self.port = 0
        self._gates: list[asyncio.Event] = []
        self._pumps: set[asyncio.Task[None]] = set()
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def freeze(self) -> None:
        for gate in self._gates:
            gate.clear()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for pump in self._pumps:
            pump.cancel()
        await asyncio.gather(*self._pumps, return_exceptions=True)
        for writer in self._writers:
            writer.close()

    async def _accept(
        self, client_in: asyncio.StreamReader, client_out: asyncio.StreamWriter
    ) -> None:
        upstream_in, upstream_out = await asyncio.open_connection(*self.target)
        gate = asyncio.Event()
        gate.set()
        self._gates.append(gate)
        self._writers += [client_out, upstream_out]
        for reader, writer in ((client_in, upstream_out), (upstream_in, client_out)):
            self._pumps.add(asyncio.create_task(self._pump(reader, writer, gate)))

    @staticmethod
    async def _pump(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter, gate: asyncio.Event
    ) -> None:
        while data := await reader.read(65536):
            await gate.wait()
            writer.write(data)
            await writer.drain()
        writer.close()


@asynccontextmanager
async def stalling_proxy(engine: Engine) -> AsyncIterator[StallingProxy]:
    url = engine.url
    proxy = StallingProxy(url.host or "localhost", url.port or 5432)
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.close()


def publish(engine: Engine, message: str) -> int:
    with engine.begin() as conn:
        event_id = publish_in(conn, Event(kind="run.started", message=message))
    assert event_id is not None
    return event_id


@pytest.mark.anyio
async def test_a_stalled_listener_is_replaced_and_misses_nothing(clean_db: Engine) -> None:
    loop = asyncio.get_running_loop()
    async with stalling_proxy(clean_db) as proxy:
        # NullPool: every read opens a new connection, which the frozen proxy lets through.
        proxied = create_engine(
            clean_db.url.set(host="127.0.0.1", port=proxy.port), poolclass=NullPool
        )
        hub = EventHub(proxied, ping_every=0.2, ping_timeout=0.5, backoff=(0.05, 0.05))
        try:
            subscription = await hub.subscribe(EventFilter())
            stalled_pid = hub.listener_pid
            assert stalled_pid is not None

            proxy.freeze()
            frozen_at = loop.time()
            during = await asyncio.to_thread(publish, clean_db, "during the stall")

            while not (hub.listening and hub.listener_pid not in (None, stalled_pid)):
                assert loop.time() - frozen_at < TIMEOUT, "the hub never noticed the stall"
                await asyncio.sleep(0.02)
            # A ping every 0.2 s that may take 0.5 s: noticed well within a second or two.
            assert loop.time() - frozen_at < 2.5

            delivered = await subscription.get(TIMEOUT)
            assert delivered is not None and delivered.id == during
        finally:
            await hub.stop()
            proxied.dispose()


@pytest.mark.anyio
async def test_stopping_the_hub_does_not_wait_for_a_stalled_ping(clean_db: Engine) -> None:
    loop = asyncio.get_running_loop()
    async with stalling_proxy(clean_db) as proxy:
        proxied = create_engine(
            clean_db.url.set(host="127.0.0.1", port=proxy.port), poolclass=NullPool
        )
        hub = EventHub(proxied, ping_every=0.1, ping_timeout=60)
        try:
            await hub.subscribe(EventFilter())
            proxy.freeze()
            await asyncio.sleep(0.3)  # the ping is sent and never answered
            assert hub.listening
            stopping_at = loop.time()
            await asyncio.wait_for(hub.stop(), TIMEOUT)
            # psycopg would spend up to 10 s asking a stuck server to cancel the ping.
            assert loop.time() - stopping_at < 1
            assert not hub.listening
        finally:
            await hub.stop()
            proxied.dispose()
