"""Smoke test for the compose stack CI brings up: Postgres and the nginx fixture server."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine, text

from scrapeline.settings import Settings

pytestmark = pytest.mark.integration


def test_postgres_reachable_and_version_16() -> None:
    engine = create_engine(Settings().database_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
            version = conn.execute(text("SHOW server_version_num")).scalar_one()
    finally:
        engine.dispose()
    assert int(version) // 10000 == 16


def test_fixture_server_healthy() -> None:
    response = httpx.get(f"{Settings().fixture_base_url}/healthz", timeout=5)
    assert response.status_code == 200
    assert response.text.strip() == "ok"
