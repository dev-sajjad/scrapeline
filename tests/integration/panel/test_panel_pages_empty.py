"""Operator pages on databases the tests write themselves: empty, hostile, overdue, stalled.

Uses `clean_db`, which empties the seeded tables, so it lives apart from the seeded tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node
from sqlalchemy import Connection, Engine, event

from scrapeline.serve.panel.app import create_app
from scrapeline.store import tables as t
from tests.integration.panel.conftest import panel_settings

pytestmark = pytest.mark.integration

SCRIPT = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"
NOW = datetime.now(UTC)


@pytest.fixture
def client(clean_db: Engine, tmp_path: Path) -> TestClient:
    return TestClient(create_app(engine=clean_db, settings=panel_settings(tmp_path)))


def add_source(
    conn: Connection,
    source_id: str,
    *,
    status: str = "active",
    cadence: str = "daily",
    **extra: Any,
) -> None:
    config = {
        "id": source_id,
        "entry": f"https://{source_id}.example/",
        "cadence": cadence,
        "legal": {"terms_checked": "2026-11-01", "notes": "test"},
        **extra.pop("config", {}),
    }
    conn.execute(
        t.sources.insert().values(
            id=source_id,
            name=extra.pop("name", source_id),
            platform="custom",
            status=status,
            config_version=1,
            config=config,
            dataset="product",
            **extra,
        )
    )


def add_run(
    conn: Connection,
    source_id: str,
    *,
    status: str = "succeeded",
    started: datetime = NOW,
    kind: str = "incremental",
    error: str | None = None,
    stats: dict[str, Any] | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        t.run.insert().values(
            id=run_id,
            source_id=source_id,
            kind=kind,
            status=status,
            started_at=started,
            finished_at=started + timedelta(seconds=2),
            stats={"pages": 1, "records": {"product": 3}} if stats is None else stats,
            error=error,
        )
    )
    return run_id


PAGES_AND_EMPTY_STATES = [
    ("/", "No sources registered yet."),
    ("/sources", "No sources registered yet."),
    ("/sites", "No sites yet."),
    ("/runs", "No runs yet."),
    ("/incidents", "No open incidents."),
]


@pytest.mark.parametrize(("path", "message"), PAGES_AND_EMPTY_STATES)
def test_pages_render_on_an_empty_database(client: TestClient, path: str, message: str) -> None:
    page = client.get(path)
    assert page.status_code == 200
    assert message in page.text


def test_empty_health_is_all_clear(client: TestClient) -> None:
    page = client.get("/").text
    assert "All clear" in page
    assert "make lake" in page
    assert "No open incidents." in page
    assert 'sse-connect="/events/stream"' in page


def test_filters_on_an_empty_database_say_nothing_matches(client: TestClient) -> None:
    assert "No sources match these filters." in client.get("/sources?status=active").text
    assert "No runs match these filters." in client.get("/runs?kind=backfill").text
    assert "No incidents match these filters." in client.get("/incidents?status=all").text
    assert "No sites match these filters." in client.get("/sites?method=human").text


def test_hostile_values_are_escaped_everywhere(client: TestClient, clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        add_source(
            conn,
            "hostile",
            name=SCRIPT,
            status_note=SCRIPT,
            config={"entry": "javascript:alert(1)", "usage": {"purpose": SCRIPT}},
        )
        run_id = add_run(conn, "hostile", status="failed", error=SCRIPT)
        incident_id = conn.execute(
            t.incident.insert()
            .values(
                source_id="hostile",
                run_id=run_id,
                kind="quality",
                summary=SCRIPT,
                details={"note": SCRIPT, "occurrences": 1},
            )
            .returning(t.incident.c.id)
        ).scalar_one()
        conn.execute(
            t.fetch_log.insert().values(
                run_id=run_id,
                source_id="hostile",
                url="javascript:alert(1)",
                status=200,
                error=SCRIPT,
                fetched_at=NOW,
            )
        )
    for path in (
        "/",
        "/sources",
        "/sources/hostile",
        "/runs",
        f"/runs/{run_id}",
        "/incidents",
        f"/incidents/{incident_id}",
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        assert SCRIPT not in page.text, path
        assert ESCAPED in page.text, path
        assert 'href="javascript:' not in page.text, path


def test_unreviewed_terms_are_flagged(client: TestClient, clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        add_source(
            conn,
            "unreviewed",
            status="draft",
            config={"legal": {"terms_checked": "1970-01-01", "notes": "todo"}},
        )
    page = client.get("/sources/unreviewed").text
    assert "Terms not reviewed" in page
    assert "not reviewed" in page and "1970-01-01" in page


def test_health_flags_overdue_sources_but_not_drafts_or_manual_ones(
    client: TestClient, clean_db: Engine
) -> None:
    with clean_db.begin() as conn:
        add_source(conn, "late-daily", cadence="daily")
        add_run(conn, "late-daily", started=NOW - timedelta(days=3))
        # A recent reprocess does not count as the scheduled run.
        add_run(conn, "late-daily", started=NOW - timedelta(minutes=5), kind="reprocess")
        add_source(conn, "fresh-hourly", cadence="hourly")
        add_run(conn, "fresh-hourly", started=NOW - timedelta(minutes=20))
        add_source(conn, "never-hourly", cadence="hourly")
        add_source(conn, "draft-daily", status="draft", cadence="daily")
        add_source(conn, "manual-shop", cadence="manual")
    page = client.get("/").text
    assert "2 sources need attention" in page
    sources = client.get("/sources").text
    for source_id, flagged in (
        ("late-daily", True),
        ("never-hourly", True),
        ("fresh-hourly", False),
        ("draft-daily", False),
        ("manual-shop", False),
    ):
        row = sources.split(f'href="/sources/{source_id}"')[0].rsplit("<tr", 1)[1]
        assert ('class="flagged"' in row) is flagged, source_id


def test_health_counts_stalled_and_failed_jobs(client: TestClient, clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        add_source(conn, "queue-test", status="draft")
        conn.execute(
            t.job.insert().values(
                kind="run",
                source_id="queue-test",
                status="running",
                lease_until=NOW - timedelta(minutes=10),
            )
        )
        conn.execute(
            t.job.insert().values(
                kind="run",
                source_id="queue-test",
                status="running",
                lease_until=NOW + timedelta(minutes=10),
            )
        )
        conn.execute(t.job.insert().values(kind="run", source_id="queue-test", status="dead"))
        add_dead_job(conn, "queue-test", NOW - timedelta(days=3))
    page = client.get("/").text
    assert "1 stalled job, 1 failed job in the last 24 h." in page
    assert "verdict-bad" in page
    assert failed_jobs_tile(page) == "1 out of retries; 2 in all"


def test_health_does_not_count_records(client: TestClient, clean_db: Engine) -> None:
    """Health shows no record counts, so it must not pay for scanning the records."""
    with clean_db.begin() as conn:
        add_source(conn, "counted")
        add_run(conn, "counted")

    def statements_for(path: str) -> list[str]:
        seen: list[str] = []

        def remember(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            seen.append(statement)

        event.listen(clean_db, "before_cursor_execute", remember)
        try:
            assert client.get(path).status_code == 200
        finally:
            event.remove(clean_db, "before_cursor_execute", remember)
        return seen

    assert not any("data.records" in sql for sql in statements_for("/"))
    assert any("data.records" in sql for sql in statements_for("/sources"))  # shown there


def test_jobs_that_died_days_ago_leave_health_clear(client: TestClient, clean_db: Engine) -> None:
    """A dead job stays in the queue table; once it is old it no longer turns health red."""
    with clean_db.begin() as conn:
        add_source(conn, "recovered", status="draft")
        add_dead_job(conn, "recovered", NOW - timedelta(days=3))
        add_dead_job(conn, "recovered", NOW - timedelta(hours=25))
    page = client.get("/").text
    assert "All clear" in page
    assert "verdict-ok" in page
    assert failed_jobs_tile(page) == "0 out of retries; 2 in all"


def text_of(node: Node, selector: str) -> str:
    """Whitespace-normalised text of the first match, which must exist."""
    found = node.css_first(selector)
    assert found is not None, selector
    return " ".join(found.text(separator=" ").split())


def failed_jobs_tile(page: str) -> str:
    for tile in HTMLParser(page).css("dl.stats > div"):
        if text_of(tile, "dt") == "Failed jobs (24 h)":
            return text_of(tile, "dd")
    raise AssertionError("no failed jobs tile")


def add_dead_job(conn: Connection, source_id: str, died: datetime) -> None:
    conn.execute(
        t.job.insert().values(
            kind="run",
            source_id=source_id,
            status="dead",
            attempts=3,
            created_at=died - timedelta(minutes=30),
            updated_at=died,
        )
    )


@pytest.mark.parametrize(
    ("usage", "shown"),
    [
        ({"allow_pii": True, "purpose": "Contact sellers"}, "kept allow_pii with a stated purpose"),
        (
            {"allow_pii": True, "purpose": ""},
            "blanked before storage no purpose allow_pii is set, but personal data is kept only"
            " with a purpose",
        ),
        ({"allow_pii": False, "purpose": "Price tracking"}, "blanked before storage"),
        ({"allow_pii": "yes please"}, "blanked before storage the usage block does not validate"),
    ],
)
def test_personal_data_row_follows_the_record_store(
    client: TestClient, clean_db: Engine, usage: dict[str, Any], shown: str
) -> None:
    with clean_db.begin() as conn:
        add_source(conn, "pii", config={"usage": usage})
    tree = HTMLParser(client.get("/sources/pii").text)
    row = next(pair for pair in tree.css("dl.kv > div") if text_of(pair, "dt") == "Personal data")
    assert text_of(row, "dd") == shown


def test_health_shows_records_loaded_not_parsed(client: TestClient, clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        # Parsed 20, but the load rolled back, so nothing was stored.
        add_source(conn, "rolled-back")
        rolled_back = {"pages": 1, "records": {"product": 20}, "loaded": {}}
        add_run(conn, "rolled-back", status="failed", error="IntegrityError", stats=rolled_back)
        add_source(conn, "loaded")
        loaded = {"product": {"inserted": 2, "updated": 3, "unchanged": 10, "removed": 4}}
        add_run(conn, "loaded", stats={"records": {"product": 15}, "loaded": loaded})
        add_source(conn, "still-running")
        add_run(conn, "still-running", status="running", stats={})
    tree = HTMLParser(client.get("/").text)
    headers = [" ".join(th.text().split()) for th in tree.css("thead th")]
    column = headers.index("Loaded in last run")
    for source_id, expected in (("rolled-back", "0"), ("loaded", "15"), ("still-running", "—")):
        row = next(
            row
            for row in tree.css("tbody tr")
            if row.css_first(f'a[href="/sources/{source_id}"]') is not None
        )
        cells = row.css("th, td")
        assert cells[column].text().strip() == expected, source_id
