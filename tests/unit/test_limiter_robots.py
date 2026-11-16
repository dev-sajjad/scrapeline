from scrapeline.fetch.limiter import HostRateLimiter
from scrapeline.fetch.robots import RobotsCache, RobotsFile


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


def test_token_bucket_spaces_requests_per_host() -> None:
    clock = FakeClock()
    limiter = HostRateLimiter(2.0, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        limiter.acquire("a.gov")
    assert abs(clock.now - 2.0) < 1e-9  # first free, then 4 x 0.5s


def test_hosts_are_independent() -> None:
    clock = FakeClock()
    limiter = HostRateLimiter(1.0, clock=clock, sleep=clock.sleep)
    limiter.acquire("a.gov")
    limiter.acquire("b.gov")
    assert clock.slept == []


def test_penalise_delays_next_request() -> None:
    clock = FakeClock()
    limiter = HostRateLimiter(1.0, clock=clock, sleep=clock.sleep)
    limiter.acquire("a.gov")
    limiter.penalise("a.gov", 10)
    limiter.acquire("a.gov")
    assert clock.now >= 10


def test_robots_rules_and_missing_file() -> None:
    files = {
        "https://a.gov/robots.txt": RobotsFile(
            200, "User-agent: *\nDisallow: /private\nCrawl-delay: 4\n"
        ),
        "https://b.gov/robots.txt": RobotsFile(404, ""),
        "https://c.gov/robots.txt": RobotsFile(503, ""),
    }
    robots = RobotsCache(lambda url: files[url], "scrapeline/0.1")
    assert robots.allowed("https://a.gov/meetings")
    assert not robots.allowed("https://a.gov/private/x")
    assert robots.crawl_delay("https://a.gov/") == 4.0
    assert robots.allowed("https://b.gov/anything")  # 404 = no rules
    assert not robots.allowed("https://c.gov/anything")  # 5xx = conservative disallow


def test_robots_network_error_disallows() -> None:
    def boom(url: str) -> RobotsFile:
        raise OSError("down")

    assert not RobotsCache(boom, "ua").allowed("https://a.gov/x")


def test_float_drift_does_not_spin_forever() -> None:
    # Regression: after a large penalty, sleeping exactly the computed delay could leave
    # 0.99999... tokens and a delay below the clock's resolution -> infinite loop.
    clock = FakeClock()
    clock.now = 1_000_000.123
    limiter = HostRateLimiter(100.0, clock=clock, sleep=clock.sleep)
    limiter.acquire("a.gov")
    limiter.penalise("a.gov", 10.0)
    limiter.acquire("a.gov")
    assert len(clock.slept) < 5
