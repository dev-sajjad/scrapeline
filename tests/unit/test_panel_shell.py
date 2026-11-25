"""Panel shell without a database: routing, static files, hardening headers, filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from scrapeline.serve.panel import filters
from scrapeline.serve.panel.app import create_app
from scrapeline.serve.panel.deps import Page, nav_active
from scrapeline.settings import Settings


@pytest.fixture
def client() -> TestClient:
    # Port 1 refuses connections at once: any page that needs the database gets a 503.
    engine = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    settings = Settings(_env_file=None, panel_allowed_hosts=["testserver"])
    return TestClient(create_app(engine=engine, settings=settings))


def test_liveness_needs_no_database(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200 and response.text == "ok"


def test_pages_carry_hardening_headers(client: TestClient) -> None:
    headers = client.get("/healthz").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"


def test_unknown_host_is_refused(client: TestClient) -> None:
    response = client.get("/healthz", headers={"host": "attacker.example"})
    assert response.status_code == 400


def test_static_assets_are_served(client: TestClient) -> None:
    assert client.get("/static/panel.css").status_code == 200
    htmx = client.get("/static/vendor/htmx-2.0.4.min.js")
    assert htmx.status_code == 200 and "htmx" in htmx.text


def test_unknown_page_renders_the_error_template(client: TestClient) -> None:
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "Back to health" in response.text


def test_invalid_query_renders_an_html_422(client: TestClient) -> None:
    router = APIRouter()

    @router.get("/probe")
    def probe(limit: int) -> str:
        return str(limit)

    client.app.include_router(router)  # type: ignore[attr-defined]
    response = client.get("/probe", params={"limit": "many"})
    assert response.status_code == 422
    assert "text/html" in response.headers["content-type"]
    assert "limit" in response.text


def test_lifespan_runs_and_stops_cleanly() -> None:
    engine = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    settings = Settings(_env_file=None, panel_allowed_hosts=["testserver"])
    with TestClient(create_app(engine=engine, settings=settings)) as client:
        assert client.get("/healthz").text == "ok"


def test_database_down_gives_a_helpful_503(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 503
    assert "Database unavailable" in response.text


def test_nav_marks_only_the_current_section() -> None:
    assert nav_active("/", "/")
    assert not nav_active("/sources", "/")
    assert nav_active("/sources/shop-sim", "/sources")
    assert not nav_active("/sourcesx", "/sources")


def test_page_offsets() -> None:
    page = Page(limit=50, offset=50)
    assert page.previous_offset == 0
    assert page.has_next(101) and not page.has_next(100)
    assert Page(limit=50, offset=0).previous_offset is None


def test_filters_format_values() -> None:
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    assert filters.dt(now) == "2026-11-24 12:00 UTC"
    assert filters.dt(None) == filters.EMPTY
    assert filters.ago(now - timedelta(seconds=10), now) == "just now"
    assert filters.ago(now - timedelta(minutes=5), now) == "5 min ago"
    assert filters.ago(now - timedelta(hours=3), now) == "3 h ago"
    assert filters.ago(now + timedelta(days=2), now) == "in 2 d"
    assert filters.nbytes(512) == "512 B"
    assert filters.nbytes(1536) == "1.5 KiB"
    assert filters.money({"amount": "1249.00", "currency": "GBP"}) == "1,249.00 GBP"
    assert filters.money(None) == filters.EMPTY
    assert filters.number(12345) == "12,345"
    assert filters.number(Decimal("2.50")) == "2.5"
    assert filters.percent(0.125) == "12.5%"
    assert filters.duration_ms(350) == "350 ms"
    assert filters.duration_ms(1200) == "1.2 s"
    assert filters.duration_ms(184_000) == "3 min 4 s"
    assert filters.short("a" * 64) == "a" * 12 + "…"
