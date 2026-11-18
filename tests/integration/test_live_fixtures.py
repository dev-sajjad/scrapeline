"""Hits the nginx fixture sites (docker compose up). Detection only: blocks are never evaded."""

from __future__ import annotations

import os

import pytest

from scrapeline.errors import BlockedError, URLPolicyError
from scrapeline.fetch.browser import BrowserPool
from scrapeline.fetch.client import FetchClient
from scrapeline.fetch.limiter import HostRateLimiter
from scrapeline.fetch.urlpolicy import URLPolicy

BASE = os.environ.get("FIXTURE_BASE_URL", "http://localhost:8080")


def policy() -> URLPolicy:
    return URLPolicy.for_hosts(["localhost"], private_hosts=["localhost"])


def client() -> FetchClient:
    return FetchClient(
        policy=policy(),
        limiter=HostRateLimiter(20),
        user_agent="scrapeline-test",
        max_retries=1,
        sleep=lambda s: None,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/blocked/forbidden", "forbidden"),
        ("/blocked/ratelimited", "rate_limited"),
        ("/blocked/challenge.html", "challenge"),
        ("/blocked/empty-shell.html", "empty_shell"),
    ],
)
def test_block_responses_are_detected(path: str, kind: str) -> None:
    with pytest.raises(BlockedError) as err, client() as c:
        c.get(BASE + path)
    assert err.value.kind == kind


@pytest.mark.browser
def test_browser_renders_js_table_and_clicks_load_more() -> None:
    events = []
    with BrowserPool(
        policy(), HostRateLimiter(20), "scrapeline-test", on_event=events.append
    ) as pool:
        resp = pool.render(
            BASE + "/js-table/", wait_for="tr.meeting", click_until_gone="#load-more"
        )
    html = resp.content.decode()
    assert html.count('class="meeting"') == 25
    assert resp.headers["x-scrapeline-clicks"] == "2"
    assert any(e.url.endswith("/js-table/") for e in events)


@pytest.mark.browser
def test_browser_blocks_off_allowlist_requests() -> None:
    strict = URLPolicy.for_hosts(["localhost"], private_hosts=["localhost"])
    with BrowserPool(strict, HostRateLimiter(20), "scrapeline-test") as pool:
        resp = pool.render(BASE + "/static-list/")
    assert b"Public Meetings" in resp.content
    with pytest.raises(URLPolicyError), BrowserPool(strict, HostRateLimiter(20), "t") as pool:
        pool.render("https://example.com/")


@pytest.mark.browser
def test_browser_detects_challenge_shell() -> None:
    with (
        pytest.raises(BlockedError) as err,
        BrowserPool(policy(), HostRateLimiter(20), "t") as pool,
    ):
        pool.render(BASE + "/blocked/challenge.html")
    assert err.value.kind == "challenge"
