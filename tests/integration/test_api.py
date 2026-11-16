from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from scrapeline.pipeline.runner import Runner
from scrapeline.serve import api

pytestmark = pytest.mark.integration


@pytest.fixture
def client(runner: Runner, clean_db: Engine, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    runner.run("legistar-testcity")
    monkeypatch.setattr(api, "get_engine", lambda: clean_db)
    return TestClient(api.app)


def test_health_sources_meetings(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    (src,) = client.get("/sources").json()
    assert src["id"] == "legistar-testcity" and src["meetings"] == 25
    page = client.get("/meetings", params={"jurisdiction": "us-wa-testcity", "limit": 5}).json()
    assert page["count"] == 5 and all(i["raw_sha256"] for i in page["items"])
    cancelled = client.get("/meetings", params={"status": "cancelled"}).json()
    assert all(i["status"] == "cancelled" for i in cancelled["items"])
    assert client.get("/meetings", params={"status": "bogus"}).status_code == 422
