"""FetchClient: the only httpx path to the network (CLAUDE.md §0.2).

Per request: URL policy → robots.txt → circuit breaker → rate limiter → GET (no auto-redirect)
→ each redirect target re-checked → retries with backoff on 429/5xx/timeouts (Retry-After honoured)
→ anti-bot classification → every attempt reported through `on_event` (→ raw.fetch_log).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_exponential_jitter

from scrapeline.errors import (
    BlockedError,
    CircuitOpenError,
    FetchError,
    HTTPStatusError,
    RobotsDisallowedError,
)
from scrapeline.fetch.limiter import HostRateLimiter
from scrapeline.fetch.robots import RobotsCache, RobotsFile
from scrapeline.fetch.urlpolicy import URLPolicy
from scrapeline.ops.logging import get_logger
from scrapeline.quality import antibot

log = get_logger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 120.0


@dataclass(frozen=True)
class FetchResponse:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    content: bytes
    content_type: str | None
    fetched_at: datetime
    elapsed_ms: int
    redirects: tuple[str, ...] = ()


@dataclass(frozen=True)
class FetchEvent:
    """One HTTP attempt. Persisted as a raw.fetch_log row by the runner."""

    url: str
    status: int | None
    elapsed_ms: int
    nbytes: int
    attempt: int
    fetched_at: datetime
    error: str | None = None


class CircuitBreaker:
    def __init__(
        self, threshold: int, reset_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.threshold = threshold
        self.reset_s = reset_s
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def before(self, host: str) -> None:
        with self._lock:
            opened = self._opened_at.get(host)
            if opened is None:
                return
            if self._clock() - opened < self.reset_s:
                raise CircuitOpenError(f"circuit open for {host}")
            del self._opened_at[host]  # half-open: allow one trial
            self._failures[host] = self.threshold - 1

    def success(self, host: str) -> None:
        with self._lock:
            self._failures.pop(host, None)

    def failure(self, host: str) -> None:
        with self._lock:
            n = self._failures.get(host, 0) + 1
            self._failures[host] = n
            if n >= self.threshold:
                self._opened_at[host] = self._clock()

    def open_now(self, host: str) -> None:
        with self._lock:
            self._failures[host] = self.threshold
            self._opened_at[host] = self._clock()


class _RetryableStatusError(Exception):
    def __init__(self, response: httpx.Response, retry_after: float | None) -> None:
        super().__init__(f"retryable status {response.status_code}")
        self.response = response
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


@dataclass
class FetchClient:
    policy: URLPolicy
    limiter: HostRateLimiter
    user_agent: str
    respect_robots: bool = True
    timeout_s: float = 30.0
    max_retries: int = 4
    max_redirects: int = 5
    breaker: CircuitBreaker = field(default_factory=lambda: CircuitBreaker(5, 300.0))
    transport: httpx.BaseTransport | None = None
    on_event: Callable[[FetchEvent], None] | None = None
    sleep: Callable[[float], None] = time.sleep
    _http: httpx.Client = field(init=False, repr=False)
    _robots: RobotsCache = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(self.timeout_s),
            follow_redirects=False,
            transport=self.transport,
        )
        self._robots = RobotsCache(self._fetch_robots, self.user_agent)

    # -- public ----------------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        expect_content: bool = True,
    ) -> FetchResponse:
        url = self.policy.check(url)
        host = urlsplit(url).netloc
        self.breaker.before(host)
        self._check_robots(url)
        started = time.monotonic()
        try:
            response, redirects = self._get_with_retries(url, headers or {})
        except _RetryableStatusError as exc:
            self.breaker.failure(host)
            status = exc.response.status_code
            if status == 429:
                self.breaker.open_now(host)
                raise BlockedError(url, antibot.BlockKind.RATE_LIMITED, status) from exc
            raise HTTPStatusError(url, status) from exc
        except httpx.TransportError as exc:
            self.breaker.failure(host)
            raise FetchError(f"transport error after retries: {exc!r}: {url}") from exc

        content = response.content
        content_type = response.headers.get("content-type")
        kind = antibot.classify(
            response.status_code, content, content_type, expect_content=expect_content
        )
        if kind is not None:
            self.breaker.open_now(host)
            log.warning("fetch.blocked", url=url, kind=kind, status=response.status_code)
            raise BlockedError(url, kind, response.status_code)
        if response.status_code >= 400:
            self.breaker.failure(host)
            raise HTTPStatusError(url, response.status_code)
        self.breaker.success(host)
        return FetchResponse(
            url=url,
            final_url=str(response.url),
            status=response.status_code,
            headers=dict(response.headers),
            content=content,
            content_type=content_type,
            fetched_at=datetime.now(UTC),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            redirects=tuple(redirects),
        )

    @property
    def robots(self) -> RobotsCache | None:
        return self._robots if self.respect_robots else None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> FetchClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals -------------------------------------------------------------

    def _check_robots(self, url: str) -> None:
        if self.respect_robots and not self._robots.allowed(url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")
        delay = self._robots.crawl_delay(url) if self.respect_robots else None
        if delay:
            self.limiter.set_rate(urlsplit(url).netloc, min(1.0 / delay, 1.0))

    def _fetch_robots(self, robots_url: str) -> RobotsFile:
        robots_url = self.policy.check(robots_url)
        self.limiter.acquire(urlsplit(robots_url).netloc)
        response = self._http.get(robots_url)
        return RobotsFile(response.status_code, response.text if response.is_success else "")

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _RetryableStatusError) and exc.retry_after is not None:
            return min(exc.retry_after, MAX_RETRY_AFTER_S)
        backoff: float = wait_exponential_jitter(initial=1, max=60)(state)
        return backoff

    def _get_with_retries(
        self, url: str, headers: dict[str, str]
    ) -> tuple[httpx.Response, list[str]]:
        retrying = Retrying(
            stop=stop_after_attempt(self.max_retries + 1),
            wait=self._wait,
            retry=retry_if_exception_type((_RetryableStatusError, httpx.TransportError)),
            sleep=self.sleep,
            reraise=True,
        )
        attempt = 0
        for call in retrying:
            with call:
                attempt += 1
                return self._get_following_redirects(url, headers, attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def _get_following_redirects(
        self, url: str, headers: dict[str, str], attempt: int
    ) -> tuple[httpx.Response, list[str]]:
        redirects: list[str] = []
        current = url
        for _ in range(self.max_redirects + 1):
            response = self._send(current, headers, attempt)
            if response.is_redirect and "location" in response.headers:
                target = self.policy.check(urljoin(current, response.headers["location"]))
                self._check_robots(target)
                redirects.append(target)
                current = target
                continue
            if response.status_code in RETRY_STATUSES:
                if response.status_code == 429:
                    retry_after = _parse_retry_after(response.headers.get("retry-after"))
                    self.limiter.penalise(urlsplit(current).netloc, retry_after or 5.0)
                else:
                    retry_after = _parse_retry_after(response.headers.get("retry-after"))
                raise _RetryableStatusError(response, retry_after)
            return response, redirects
        raise FetchError(f"too many redirects (>{self.max_redirects}): {url}")

    def _send(self, url: str, headers: dict[str, str], attempt: int) -> httpx.Response:
        self.limiter.acquire(urlsplit(url).netloc)
        started = time.monotonic()
        fetched_at = datetime.now(UTC)
        try:
            response = self._http.get(url, headers=headers)
            response.read()
        except httpx.TransportError as exc:
            self._emit(FetchEvent(url, None, _ms(started), 0, attempt, fetched_at, repr(exc)))
            log.info("fetch.error", url=url, attempt=attempt, error=repr(exc))
            raise
        self._emit(
            FetchEvent(
                url, response.status_code, _ms(started), len(response.content), attempt, fetched_at
            )
        )
        log.info("fetch", url=url, status=response.status_code, attempt=attempt)
        return response

    def _emit(self, event: FetchEvent) -> None:
        if self.on_event is not None:
            self.on_event(event)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
