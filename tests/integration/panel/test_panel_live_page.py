"""The /live page over the seeded database. Read only: these tests never write."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select

from scrapeline.serve.panel.app import create_app
from scrapeline.store import tables as t
from tests.integration.panel.conftest import Seeded

pytestmark = pytest.mark.integration


def initial_rows(html: str) -> list[str]:
    """The event rows rendered into the live table before the stream connects."""
    start = html.index('<tbody sse-swap="ops"')
    tbody = html[start : html.index("</tbody>", start)]
    return re.findall(r"<tr>\s*<td><time.*?</tr>", tbody, flags=re.S)


def test_live_page_renders_the_feed_and_its_filters(client: TestClient, seeded: Seeded) -> None:
    page = client.get("/live")
    assert page.status_code == 200
    html = page.text
    assert "<h1>Live events</h1>" in html
    assert '<a href="/live" aria-current="page">Live</a>' in html
    assert 'sse-connect="/events/stream"' in html
    assert 'aria-live="polite"' in html
    for field in ("live-source", "live-kind", "live-level"):
        assert f'<label for="{field}">' in html and f'id="{field}"' in html
    assert '<option value="shop-sim">shop-sim</option>' in html
    assert '<option value="legistar-testcity">legistar-testcity</option>' in html
    with seeded.engine.connect() as conn:
        events = conn.execute(select(func.count()).select_from(t.event)).scalar_one()
    assert events > 0
    rows = initial_rows(html)
    assert len(rows) == min(events, 50)
    # Newest first: the failed day-4 run is near the top, the first Legistar event at the bottom.
    assert "run.started" in rows[-1] and "legistar-testcity" in rows[-1]
    assert any("run.failed" in row and "HTTPStatusError" in row for row in rows[:3])


def test_filters_narrow_the_rows_and_the_stream(client: TestClient, seeded: Seeded) -> None:
    html = client.get("/live", params={"source_id": "shop-sim", "level": "error"}).text
    assert 'sse-connect="/events/stream?source_id=shop-sim&amp;level=error"' in html
    assert '<option value="shop-sim" selected>' in html
    assert '<option value="error" selected>' in html
    rows = initial_rows(html)
    assert len(rows) == 1 and "run.failed" in rows[0]
    assert f'href="/runs/{seeded.failed_run}"' in rows[0]


def test_kind_prefix_filter(client: TestClient) -> None:
    html = client.get("/live", params={"kind": "records"}).text
    assert 'name="kind" value="records"' in html
    rows = initial_rows(html)
    assert rows and all("<code>records.loaded</code>" in row for row in rows)


def test_run_filter_shows_one_run_and_a_way_back(client: TestClient, seeded: Seeded) -> None:
    html = client.get("/live", params={"run_id": str(seeded.failed_run)}).text
    assert "Only events of run" in html
    assert f'<input type="hidden" name="run_id" value="{seeded.failed_run}">' in html
    assert f"run_id={seeded.failed_run}" in html  # in the stream URL
    assert '<a href="http://testserver/live">Show every run</a>' in html
    kinds = [re.findall(r"<code>(.*?)</code>", row)[0] for row in initial_rows(html)]
    assert kinds == ["run.failed", "run.started"]


def test_unknown_source_keeps_the_choice_and_says_there_is_nothing(client: TestClient) -> None:
    html = client.get("/live", params={"source_id": "ghost"}).text
    assert '<option value="ghost" selected>ghost</option>' in html
    assert initial_rows(html) == []
    assert "No events yet." in html


@pytest.mark.parametrize(
    "query", ["run_id=nope", "kind=RUN", "kind=run*", "level=fatal", "source_id=" + "s" * 101]
)
def test_invalid_filters_get_an_html_422(client: TestClient, query: str) -> None:
    response = client.get(f"/live?{query}")
    assert response.status_code == 422
    assert "text/html" in response.headers["content-type"]


def test_page_has_no_inline_script_or_style(client: TestClient) -> None:
    html = client.get("/live").text
    assert "style=" not in html
    assert re.findall(r"<script(?![^>]*\bsrc=)", html) == []
    assert re.search(r"\son[a-z]+=", html) is None


def test_unmigrated_database_says_how_to_fix_it(seeded: Seeded) -> None:
    url = seeded.engine.url.set(database="postgres")
    app = create_app(engine=create_engine(url), settings=seeded.settings)
    page = TestClient(app).get("/live")
    assert page.status_code == 503
    assert "make migrate" in page.text
