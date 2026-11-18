"""robots.txt cache (protego). 4xx = no rules (allow); 5xx/network error = disallow all."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from protego import Protego


@dataclass(frozen=True)
class RobotsFile:
    status: int
    body: str


RobotsFetcher = Callable[[str], RobotsFile]


class RobotsCache:
    def __init__(self, fetcher: RobotsFetcher, user_agent: str) -> None:
        self._fetcher = fetcher
        self._ua = user_agent
        self._cache: dict[str, Protego | bool] = {}
        self._lock = threading.Lock()

    def _rules(self, origin: str) -> Protego | bool:
        with self._lock:
            if origin in self._cache:
                return self._cache[origin]
        try:
            robots = self._fetcher(origin + "/robots.txt")
        except Exception:  # network failure: be conservative
            rules: Protego | bool = False
        else:
            if 200 <= robots.status < 300:
                rules = Protego.parse(robots.body)
            elif 400 <= robots.status < 500:
                rules = True
            else:
                rules = False
        with self._lock:
            self._cache[origin] = rules
        return rules

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        rules = self._rules(f"{parts.scheme}://{parts.netloc}")
        if isinstance(rules, bool):
            return rules
        return bool(rules.can_fetch(url, self._ua))

    def crawl_delay(self, url: str) -> float | None:
        parts = urlsplit(url)
        rules = self._rules(f"{parts.scheme}://{parts.netloc}")
        if isinstance(rules, bool):
            return None
        delay = rules.crawl_delay(self._ua)
        return float(delay) if delay is not None else None
