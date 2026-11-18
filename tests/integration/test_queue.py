from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, update

from scrapeline.ops import queue
from scrapeline.ops.runs import sync_source
from scrapeline.ops.worker import schedule_due, work
from scrapeline.registry import load_sources
from scrapeline.settings import PROJECT_ROOT
from scrapeline.store import tables as t

pytestmark = pytest.mark.integration


@pytest.fixture
def sources(clean_db: Engine) -> dict:
    srcs = load_sources(PROJECT_ROOT / "config/sources")
    with clean_db.begin() as conn:
        for cfg in srcs.values():
            sync_source(conn, cfg)
    return srcs


def test_schedule_enqueues_only_active_sources_once(clean_db: Engine, sources: dict) -> None:
    first = schedule_due(clean_db, sources)
    assert sorted(first) == ["fixture-js-table", "fixture-static-list"]  # legistar-* are draft
    assert schedule_due(clean_db, sources) == []  # deduplicated while queued


def test_skip_locked_claims_are_exclusive_and_leases_expire(
    clean_db: Engine, sources: dict
) -> None:
    queue.enqueue(clean_db, "run", "fixture-static-list")
    a = queue.claim(clean_db, "w1")
    assert a is not None and a.attempts == 1
    assert queue.claim(clean_db, "w2") is None  # leased
    later = datetime.now(UTC) + queue.LEASE + timedelta(seconds=1)
    b = queue.claim(clean_db, "w2", now=later)  # lease expired -> reclaimed
    assert b is not None and b.id == a.id and b.attempts == 2


def test_failures_back_off_then_dead_letter(clean_db: Engine, sources: dict) -> None:
    queue.enqueue(clean_db, "run", "fixture-static-list")

    def boom(job: queue.Job) -> None:
        raise RuntimeError("site down")

    far = datetime.now(UTC) + timedelta(days=1)
    for _ in range(3):
        with clean_db.begin() as conn:  # make the backed-off job due again
            conn.execute(update(t.job).values(run_after=datetime.now(UTC)))
        assert work(clean_db, boom, once=True) == 1
    with clean_db.connect() as conn:
        job = conn.execute(select(t.job)).one()
    assert job.status == "dead" and job.attempts == 3 and "site down" in job.last_error
    assert queue.claim(clean_db, "w", now=far) is None
