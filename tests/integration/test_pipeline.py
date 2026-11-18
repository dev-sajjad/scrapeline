from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
from alembic import command
from sqlalchemy import Engine, func, select, text

from scrapeline.pipeline.runner import Runner
from scrapeline.store import tables as t
from tests.integration.conftest import FakeLegistar

pytestmark = pytest.mark.integration
SID = "legistar-testcity"


def count(engine: Engine, table: t.Table) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(table).where(table.c.source_id == SID)
            ).scalar_one()
        )


def test_migrations_match_table_definitions(engine: Engine) -> None:
    command.check(engine.alembic_config)  # raises if models and migrations diverge


def test_run_loads_records_with_full_lineage(runner: Runner, clean_db: Engine) -> None:
    summary = runner.run(SID)
    assert summary.status == "succeeded", summary.error
    assert summary.loaded["meeting"]["inserted"] == 25
    assert summary.loaded["body"]["inserted"] == 83
    with clean_db.connect() as conn:
        row = conn.execute(select(t.meetings).limit(1)).one()
        for col in (
            "source_id",
            "external_id",
            "canonical_url",
            "content_hash",
            "schema_version",
            "first_seen_at",
            "last_seen_at",
            "fetched_at",
            "run_id",
            "scraper_version_id",
            "raw_sha256",
        ):
            assert getattr(row, col) is not None, col
        checks = conn.execute(select(t.check_result.c.check_name, t.check_result.c.status)).all()
    assert dict(checks)["record_count"] == "pass"


def test_rerun_is_idempotent(runner: Runner, clean_db: Engine) -> None:
    runner.run(SID)
    before = (count(clean_db, t.meetings), count(clean_db, t.documents), count(clean_db, t.bodies))
    second = runner.run(SID)
    after = (count(clean_db, t.meetings), count(clean_db, t.documents), count(clean_db, t.bodies))
    assert before == after == (25, before[1], 83)
    assert second.loaded["meeting"] == {"inserted": 0, "updated": 0, "unchanged": 25}


def test_title_change_updates_row_and_keeps_old_raw(
    runner: Runner, clean_db: Engine, fake_legistar: FakeLegistar
) -> None:
    runner.run(SID)
    event = fake_legistar.events[0]
    event_id = str(event["EventId"])
    with clean_db.connect() as conn:
        old = conn.execute(select(t.meetings).where(t.meetings.c.external_id == event_id)).one()
    event["EventBodyName"] = "City Council (Corrected Title)"
    summary = runner.run(SID)
    assert summary.loaded["meeting"]["updated"] == 1
    assert count(clean_db, t.meetings) == 25  # no duplicate
    with clean_db.connect() as conn:
        new = conn.execute(select(t.meetings).where(t.meetings.c.external_id == event_id)).one()
        raw_shas = set(
            conn.execute(
                select(t.fetch_log.c.raw_sha256).where(t.fetch_log.c.kind == "events")
            ).scalars()
        )
    assert new.title == "City Council (Corrected Title)"
    assert new.content_hash != old.content_hash and new.raw_sha256 != old.raw_sha256
    assert new.first_seen_at == old.first_seen_at and new.last_seen_at > old.last_seen_at
    assert {old.raw_sha256, new.raw_sha256} <= raw_shas  # old raw still referenced


def test_watermark_drives_next_incremental_window(
    runner: Runner, clean_db: Engine, fake_legistar: FakeLegistar
) -> None:
    first = runner.run(SID)
    assert first.watermark is not None
    fake_legistar.requests.clear()
    runner.run(SID)
    events_req = next(r for r in fake_legistar.requests if r.url.path.endswith("/events"))
    flt = parse_qs(events_req.url.query.decode())["$filter"][0]
    assert flt.startswith("EventLastModifiedUtc ge datetime'")
    # since = watermark - 24h overlap, which is later than the 365-day initial lookback
    assert first.watermark[:4] in flt


def test_reprocess_from_raw_is_idempotent(runner: Runner, clean_db: Engine) -> None:
    runner.run(SID)
    summary = runner.reprocess(SID)
    assert summary.status == "succeeded"
    assert summary.loaded["meeting"] == {"inserted": 0, "updated": 0, "unchanged": 25}
    assert summary.checks["duplicate_rate"] == "pass"


def test_reprocess_rebuilds_deleted_rows(runner: Runner, clean_db: Engine) -> None:
    runner.run(SID)
    with clean_db.begin() as conn:
        conn.execute(text("DELETE FROM documents"))
        conn.execute(text("DELETE FROM meetings"))
    summary = runner.reprocess(SID)
    assert summary.loaded["meeting"]["inserted"] == 25
    assert count(clean_db, t.meetings) == 25


def test_blocked_source_fails_run_and_opens_one_incident(
    runner: Runner, clean_db: Engine, fake_legistar: FakeLegistar
) -> None:
    fake_legistar.override = lambda r: (
        httpx.Response(403) if r.url.path.endswith("/events") else None
    )
    first = runner.run(SID)
    second = runner.run(SID)
    assert first.status == second.status == "failed"
    assert first.incident_id == second.incident_id  # deduplicated, occurrences bumped
    with clean_db.connect() as conn:
        inc = conn.execute(select(t.incident)).one()
        logged = conn.execute(select(t.fetch_log.c.status).where(t.fetch_log.c.status == 403)).all()
        watermark = conn.execute(select(t.sources.c.watermark)).scalar_one()
    assert inc.kind == "blocked" and inc.details["occurrences"] == 2
    assert len(logged) == 2  # one per run; 403 is not retried
    assert watermark is None  # failed runs never advance the watermark


def test_malformed_record_goes_to_rejects(
    runner: Runner, clean_db: Engine, fake_legistar: FakeLegistar
) -> None:
    fake_legistar.events[0]["EventDate"] = "garbage"
    summary = runner.run(SID)
    assert summary.rejects == 1
    with clean_db.connect() as conn:
        reject = conn.execute(select(t.rejects)).one()
    assert "meeting_date" in reject.reason
    assert count(clean_db, t.meetings) == 24
