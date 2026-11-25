"""The panel in a real browser: layout at phone and laptop widths, and the live feed's empty state.

Runs a uvicorn server over the seeded database and drives Chromium with Playwright.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
import uvicorn
from sqlalchemy import select

from scrapeline.serve.panel.app import create_app
from scrapeline.store import tables as t
from tests.integration.panel.conftest import SHOP_SIM_HOST, Seeded

if TYPE_CHECKING:
    from playwright.sync_api import Browser

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = [pytest.mark.integration, pytest.mark.browser]

WIDTHS = (375, 1280)


@pytest.fixture(scope="module")
def base_url(seeded: Seeded) -> Iterator[str]:
    settings = seeded.settings.model_copy(update={"panel_allowed_hosts": ["127.0.0.1"]})
    app = create_app(engine=seeded.engine, settings=settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)
    sock.close()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_api.sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch()
        except sync_api.Error as exc:
            pytest.skip(f"Playwright Chromium is not installed: {exc}")
        yield chromium
        chromium.close()


def pages(seeded: Seeded) -> list[str]:
    with seeded.engine.connect() as conn:
        record_id = conn.execute(
            select(t.records.c.id).where(t.records.c.external_id == "SIM-0013")
        ).scalar_one()
    return [
        "/",
        "/sources",
        "/sources/shop-sim",
        "/sites",
        f"/sites/{SHOP_SIM_HOST}",
        "/runs",
        f"/runs/{seeded.shop_runs[1]}",
        f"/runs/{seeded.failed_run}",
        "/incidents",
        f"/incidents/{seeded.incident_id}",
        "/data",
        "/data/product",
        f"/data/product/{record_id}",
        "/storage",
        "/live",
    ]


@pytest.mark.parametrize("width", WIDTHS)
def test_no_page_scrolls_sideways(
    browser: Browser, base_url: str, seeded: Seeded, width: int
) -> None:
    """Wide tables scroll inside their own box; the page itself never does."""
    page = browser.new_page(viewport={"width": width, "height": 800})
    try:
        wide = {}
        for path in pages(seeded):
            page.goto(base_url + path)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            if overflow > 0:
                wide[path] = overflow
        assert wide == {}
    finally:
        page.close()


def test_inline_empty_text_is_only_muted(browser: Browser, base_url: str, seeded: Seeded) -> None:
    """`.empty` marks both empty-state boxes and muted words inside tables and headings."""
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(base_url + "/data/product")
        inline = page.evaluate(
            """() => [...document.querySelectorAll('span.empty')].map(e => {
                const s = getComputedStyle(e);
                return [s.borderTopStyle, s.paddingTop];
            })"""
        )
        assert inline and all(style == ["none", "0px"] for style in inline)
        page.goto(base_url + "/incidents?status=closed")
        box = page.evaluate("getComputedStyle(document.querySelector('p.empty')).borderTopStyle")
        assert box == "dashed"
    finally:
        page.close()


def test_live_feed_says_it_is_waiting_when_nothing_matches(browser: Browser, base_url: str) -> None:
    page = browser.new_page()
    try:
        page.goto(base_url + "/live?kind=nothing_matches")
        # The stream's reset empties the table, then the status line reports the replay.
        page.wait_for_function(
            "document.querySelector('.live-status').textContent.includes('No matching events')"
        )
        state = page.evaluate(
            """() => {
                const table = document.querySelector('table.events');
                return [table.querySelectorAll('tbody tr').length,
                        getComputedStyle(table, '::after').content];
            }"""
        )
        assert state == [0, '"Waiting for events…"']
    finally:
        page.close()


def test_without_javascript_the_empty_feed_is_hidden(browser: Browser, base_url: str) -> None:
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.goto(base_url + "/")
        assert not page.locator("table.events").is_visible()
        # Playwright's text search skips <noscript>, so look at the rendered element.
        notice = page.locator(".live-feed noscript p")
        assert notice.is_visible() and "Live updates need JavaScript." in notice.inner_text()
        # The event log renders its rows on the server, so it still shows them.
        page.goto(base_url + "/live")
        assert page.locator("table.events tbody tr").count() > 0
        assert page.locator("table.events").is_visible()
    finally:
        context.close()
