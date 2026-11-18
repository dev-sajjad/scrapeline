"""Per-host token bucket shared by FetchClient and BrowserPool (one politeness policy)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    rate: float
    capacity: float
    tokens: float
    updated: float


_EPS = 1e-6
_MIN_SLEEP = 1e-3


class HostRateLimiter:
    """Blocking token bucket per host. `rps` = sustained requests/second; `burst` = bucket size."""

    def __init__(
        self,
        default_rps: float = 1.0,
        *,
        burst: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if default_rps <= 0:
            raise ValueError("rps must be positive")
        self._default_rps = default_rps
        self._burst = burst
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def set_rate(self, host: str, rps: float) -> None:
        with self._lock:
            now = self._clock()
            self._buckets[host] = _Bucket(rps, self._burst, self._burst, now)

    def acquire(self, host: str) -> float:
        """Block until a token is available. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                bucket = self._buckets.get(host)
                if bucket is None:
                    bucket = _Bucket(self._default_rps, self._burst, self._burst, now)
                    self._buckets[host] = bucket
                bucket.tokens = min(
                    bucket.capacity, bucket.tokens + (now - bucket.updated) * bucket.rate
                )
                bucket.updated = now
                # Epsilon: float drift can leave 0.99999... after sleeping exactly the delay.
                if bucket.tokens >= 1.0 - _EPS:
                    bucket.tokens = max(bucket.tokens - 1.0, -_EPS)
                    return waited
                delay = max((1.0 - bucket.tokens) / bucket.rate, _MIN_SLEEP)
            self._sleep(delay)
            waited += delay

    def penalise(self, host: str, seconds: float) -> None:
        """Push the host's next token `seconds` into the future (Retry-After, 429)."""
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is not None:
                bucket.tokens = min(bucket.tokens, 0.0) - seconds * bucket.rate
