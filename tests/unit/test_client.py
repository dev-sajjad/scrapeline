from __future__ import annotations

import httpx
import pytest
import respx

from scrapeline.errors import (
    BlockedError,
    CircuitOpenError,
    HTTPStatusError,
    RobotsDisallowedError,
    URLPolicyError,
)
from scrapeline.fetch.client import CircuitBreaker, FetchClient, FetchEvent
from scrapeline.fetch.limiter import HostRateLimiter
from scrapeline.fetch.urlpolicy import URLPolicy
from tests.conftest import public_resolver


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


def make_client(
    events: list[FetchEvent] | None = None, ft: FakeTime | None = None, **kw
) -> FetchClient:
    ft = ft or FakeTime()
    return FetchClient(
        policy=URLPolicy.for_hosts(["a.gov"], resolver=public_resolver),
        limiter=HostRateLimiter(100.0, clock=ft.clock, sleep=ft.sleep),
        user_agent="scrapeline-test/0.1",
        sleep=ft.sleep,
        on_event=(events.append if events is not None else None),
        **kw,
    )


@pytest.fixture
def mock() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://a.gov/robots.txt").respond(404)
        yield router


def test_success_sends_identified_user_agent(mock: respx.MockRouter) -> None:
    route = mock.get("https://a.gov/x").respond(200, json=[1])
    resp = make_client().get("https://a.gov/x")
    assert resp.status == 200 and resp.content == b"[1]"
    assert route.calls.last.request.headers["user-agent"] == "scrapeline-test/0.1"


def test_retries_5xx_then_succeeds_and_logs_every_attempt(mock: respx.MockRouter) -> None:
    mock.get("https://a.gov/x").mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, text="ok")]
    )
    events: list[FetchEvent] = []
    resp = make_client(events).get("https://a.gov/x")
    assert resp.status == 200
    assert [e.status for e in events] == [503, 502, 200]
    assert [e.attempt for e in events] == [1, 2, 3]


def test_retry_after_is_honoured(mock: respx.MockRouter) -> None:
    mock.get("https://a.gov/x").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    ft = FakeTime()
    make_client(ft=ft).get("https://a.gov/x")
    assert 7 in ft.slept


def test_persistent_429_becomes_blocked_and_opens_circuit(mock: respx.MockRouter) -> None:
    mock.get("https://a.gov/x").respond(429)
    client = make_client(max_retries=2)
    with pytest.raises(BlockedError) as err:
        client.get("https://a.gov/x")
    assert err.value.kind == "rate_limited"
    with pytest.raises(CircuitOpenError):
        client.get("https://a.gov/x")


def test_403_is_blocked_not_retried(mock: respx.MockRouter) -> None:
    route = mock.get("https://a.gov/x").respond(403)
    with pytest.raises(BlockedError) as err:
        make_client().get("https://a.gov/x")
    assert err.value.kind == "forbidden"
    assert route.call_count == 1


def test_challenge_page_detected(mock: respx.MockRouter) -> None:
    mock.get("https://a.gov/x").respond(
        200, html="<html><title>Just a moment...</title><div id='cf-chl-widget'></div></html>"
    )
    with pytest.raises(BlockedError) as err:
        make_client().get("https://a.gov/x")
    assert err.value.kind == "challenge"


def test_404_is_typed_error(mock: respx.MockRouter) -> None:
    mock.get("https://a.gov/missing").respond(404)
    with pytest.raises(HTTPStatusError) as err:
        make_client().get("https://a.gov/missing")
    assert err.value.status == 404


def test_redirect_to_disallowed_host_is_rejected(mock: respx.MockRouter) -> None:
    mock.get("https://a.gov/r").respond(302, headers={"Location": "http://169.254.169.254/latest"})
    with pytest.raises(URLPolicyError):
        make_client().get("https://a.gov/r")


def test_redirect_within_allowlist_is_followed(mock: respx.MockRouter) -> None:
    mock.get("https://a.gov/r").respond(301, headers={"Location": "/final"})
    mock.get("https://a.gov/final").respond(200, text="done")
    resp = make_client().get("https://a.gov/r")
    assert resp.final_url == "https://a.gov/final"
    assert resp.redirects == ("https://a.gov/final",)


def test_robots_disallow_is_respected() -> None:
    with respx.mock() as router:
        router.get("https://a.gov/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
        with pytest.raises(RobotsDisallowedError):
            make_client().get("https://a.gov/x")


def test_circuit_breaker_half_open_after_reset() -> None:
    now = [0.0]
    breaker = CircuitBreaker(2, 60, clock=lambda: now[0])
    breaker.failure("h")
    breaker.failure("h")
    with pytest.raises(CircuitOpenError):
        breaker.before("h")
    now[0] = 61
    breaker.before("h")  # half-open trial allowed
    breaker.failure("h")  # trial failed -> open again
    with pytest.raises(CircuitOpenError):
        breaker.before("h")
