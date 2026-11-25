"""The panel shell over a real database: pages render, missing tables explain themselves."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from scrapeline.serve.panel.app import create_app
from tests.integration.panel.conftest import Seeded

pytestmark = pytest.mark.integration


def test_health_page_counts_seeded_data(client: TestClient, seeded: Seeded) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<h1>Health</h1>" in page.text
    assert 'aria-current="page"' in page.text


def test_api_is_mounted_under_api(client: TestClient) -> None:
    assert client.get("/api/health").status_code in (200, 503)


def test_unmigrated_database_says_how_to_fix_it(seeded: Seeded) -> None:
    # A database without the panel's tables: point the engine at the maintenance database.
    url = seeded.engine.url.set(database="postgres")
    app = create_app(engine=create_engine(url), settings=seeded.settings)
    page = TestClient(app).get("/")
    assert page.status_code == 503
    assert "make migrate" in page.text
