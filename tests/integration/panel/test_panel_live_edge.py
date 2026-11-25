"""The /live page on an empty database and with hostile event content. Writes, so `clean_db`."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from scrapeline.ops.events import Event, publish_in
from scrapeline.serve.panel.app import create_app
from tests.integration.panel.conftest import panel_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def panel(clean_db: Engine, tmp_path: Path) -> TestClient:
    return TestClient(create_app(engine=clean_db, settings=panel_settings(tmp_path)))


def test_empty_database_renders_a_helpful_page(panel: TestClient) -> None:
    page = panel.get("/live")
    assert page.status_code == 200
    assert '<td colspan="4" class="empty">No events yet.</td>' in page.text
    assert '<option value="">All sources</option>' in page.text
    assert "Live updates need JavaScript" in page.text


def test_hostile_event_content_is_escaped(panel: TestClient, clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        publish_in(
            conn,
            Event(
                kind="records.loaded",
                level="warning",
                source_id='"><img src=x onerror=alert(1)>',
                message="<script>alert(2)</script>",
                data={"title": "</pre><script>alert(3)</script>", "url": "javascript:alert(4)"},
            ),
        )
    html = panel.get("/live").text
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html
    assert "&lt;/pre&gt;&lt;script&gt;alert(3)" in html
    assert 'href="/sources/%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert%281%29%3E"' in html
    assert 'href="javascript:' not in html
