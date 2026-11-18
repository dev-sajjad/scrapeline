"""BrowserPool: the only Playwright path to the network.

Same URL policy, robots check and per-host rate limiter as FetchClient. `page.route` aborts
off-allowlist requests and heavy resources (images, fonts, media) and logs every response.
Service workers are blocked (they can bypass routing). No stealth, no fingerprint tricks:
an identified user agent, and challenge pages are detected, not solved.
Routing is visibility and control, not a security boundary (generated code never gets here).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scrapeline.errors import BlockedError, FetchError, RobotsDisallowedError, URLPolicyError
from scrapeline.fetch.client import FetchEvent, FetchResponse
from scrapeline.fetch.limiter import HostRateLimiter
from scrapeline.fetch.robots import RobotsCache
from scrapeline.fetch.urlpolicy import URLPolicy
from scrapeline.ops.logging import get_logger
from scrapeline.quality import antibot

log = get_logger(__name__)

BLOCKED_RESOURCES = frozenset({"image", "font", "media"})
RATE_LIMITED_RESOURCES = frozenset({"document", "xhr", "fetch"})


@dataclass
class BrowserPool:
    policy: URLPolicy
    limiter: HostRateLimiter
    user_agent: str
    robots: RobotsCache | None = None
    on_event: Callable[[FetchEvent], None] | None = None
    screenshot_dir: Path | None = None
    timeout_ms: int = 30_000
    block_resources: frozenset[str] = BLOCKED_RESOURCES
    _pw: Any = field(default=None, init=False, repr=False)
    _browser: Any = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _ensure(self) -> Any:
        with self._lock:
            if self._browser is None:
                from playwright.sync_api import sync_playwright

                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(headless=True)
            return self._browser

    def render(
        self,
        url: str,
        *,
        wait_for: str | None = None,
        click_until_gone: str | None = None,
        max_clicks: int = 10,
    ) -> FetchResponse:
        url = self.policy.check(url)
        if self.robots is not None and not self.robots.allowed(url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")
        browser = self._ensure()
        context = browser.new_context(
            user_agent=self.user_agent, service_workers="block", java_script_enabled=True
        )
        context.set_default_timeout(self.timeout_ms)
        page = context.new_page()
        blocked: list[str] = []
        page.route("**/*", lambda route: self._route(route, blocked))
        page.on("response", self._on_response)
        started = time.monotonic()
        try:
            self.limiter.acquire(urlsplit(url).netloc)
            response = page.goto(url, wait_until="domcontentloaded")
            if response is None:
                raise FetchError(f"no response for {url}")
            status = response.status
            final_url = page.url
            self.policy.check(final_url)  # re-check after redirects
            if status >= 400:
                kind = antibot.classify(status, b"", "text/html")
                if kind is not None:
                    raise BlockedError(url, kind, status)
                raise FetchError(f"browser http {status}: {url}")
            if wait_for:
                page.wait_for_selector(wait_for, state="attached")
            clicks = 0
            if click_until_gone:
                while clicks < max_clicks and page.locator(click_until_gone).count() > 0:
                    rows_before = page.content().count("<tr")
                    page.locator(click_until_gone).first.click()
                    clicks += 1
                    page.wait_for_function(
                        "([sel, n]) => !document.querySelector(sel)"
                        " || document.documentElement.outerHTML.split('<tr').length - 1 > n",
                        arg=[click_until_gone, rows_before],
                    )
            content = page.content().encode("utf-8")
            kind = antibot.classify(status, content, "text/html")
            if kind is not None:
                raise BlockedError(url, kind, status)
            log.info("browser.render", url=url, status=status, clicks=clicks, blocked=len(blocked))
            return FetchResponse(
                url=url,
                final_url=final_url,
                status=status,
                headers={"x-scrapeline-rendered": "1", "x-scrapeline-clicks": str(clicks)},
                content=content,
                content_type="text/html; charset=utf-8",
                fetched_at=datetime.now(UTC),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:
            self._screenshot(page, url)
            raise
        finally:
            context.close()

    def _route(self, route: Any, blocked: list[str]) -> None:
        request = route.request
        try:
            self.policy.check(request.url)
        except URLPolicyError as exc:
            blocked.append(request.url)
            log.info("browser.route.blocked", url=request.url, reason=exc.reason)
            route.abort("blockedbyclient")
            return
        if request.resource_type in self.block_resources:
            route.abort("blockedbyclient")
            return
        if request.resource_type in RATE_LIMITED_RESOURCES and not request.is_navigation_request():
            self.limiter.acquire(urlsplit(request.url).netloc)
        route.continue_()

    def _on_response(self, response: Any) -> None:
        if self.on_event is None or response.request.resource_type not in RATE_LIMITED_RESOURCES:
            return
        self.on_event(
            FetchEvent(
                url=response.url,
                status=response.status,
                elapsed_ms=0,
                nbytes=0,
                attempt=1,
                fetched_at=datetime.now(UTC),
                error=None,
            )
        )

    def _screenshot(self, page: Any, url: str) -> None:
        if self.screenshot_dir is None:
            return
        try:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            name = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{abs(hash(url)) % 10**8}.png"
            page.screenshot(path=str(self.screenshot_dir / name), full_page=True)
            log.warning("browser.screenshot", url=url, path=str(self.screenshot_dir / name))
        except Exception as exc:  # screenshot is best effort
            log.info("browser.screenshot_failed", error=repr(exc))

    def close(self) -> None:
        with self._lock:
            if self._browser is not None:
                self._browser.close()
                self._pw.stop()
                self._browser = None
                self._pw = None

    def __enter__(self) -> BrowserPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
