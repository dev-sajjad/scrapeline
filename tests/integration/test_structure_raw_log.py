"""Reading raw documents back through raw.fetch_log, for reprocess and verify."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, func, select

from scrapeline.adapters.rawdoc import RawDoc
from scrapeline.errors import ConfigError
from scrapeline.pipeline.crawl import stored_raw_rows
from scrapeline.pipeline.runner import Runner
from scrapeline.store import tables as t

pytestmark = pytest.mark.integration

SID = "legistar-testcity"


def stored_urls(engine: Engine, run_id: uuid.UUID) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(t.fetch_log.c.url)
                .where(t.fetch_log.c.run_id == run_id, t.fetch_log.c.raw_key.is_not(None))
                .order_by(t.fetch_log.c.id)
            ).scalars()
        )


def test_latest_raw_docs_need_a_succeeded_run(runner: Runner) -> None:
    with pytest.raises(ConfigError, match="no succeeded run"):
        runner.latest_raw_docs(SID)


def test_latest_raw_docs_are_the_latest_crawls_and_skip_reprocessing(
    runner: Runner, clean_db: Engine
) -> None:
    first = runner.run(SID)
    docs = runner.latest_raw_docs(SID)
    assert [d.url for d in docs] == stored_urls(clean_db, first.run_id)
    assert docs and all(RawDoc.hash_bytes(d.content) == d.sha256 for d in docs)
    assert all(d.source_id == SID and d.status == 200 for d in docs)

    assert runner.reprocess(SID).status == "succeeded"
    assert [d.sha256 for d in runner.latest_raw_docs(SID)] == [d.sha256 for d in docs]

    second = runner.run(SID)
    assert [d.url for d in runner.latest_raw_docs(SID)] == stored_urls(clean_db, second.run_id)


def test_stored_raw_rows_cover_every_run_and_honour_since(runner: Runner, clean_db: Engine) -> None:
    first = runner.run(SID)
    second = runner.run(SID)
    with clean_db.connect() as conn:
        rows = stored_raw_rows(conn, SID)
        cutoff = conn.execute(
            select(func.max(t.fetch_log.c.fetched_at)).where(t.fetch_log.c.run_id == first.run_id)
        ).scalar_one()
        recent = stored_raw_rows(conn, SID, since=cutoff)
    assert {r.run_id for r in rows} == {first.run_id, second.run_id}
    assert [r.fetched_at for r in rows] == sorted(r.fetched_at for r in rows)
    assert all(r.raw_key and 200 <= r.status < 300 for r in rows)
    assert [r.id for r in recent] == [r.id for r in rows if r.fetched_at >= cutoff]
    assert second.run_id in {r.run_id for r in recent}
