"""Compiled instruction sources and the scheduler: manual sources are never enqueued."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select

from scrapeline.catalog.datasets import DatasetRegistry
from scrapeline.ops.runs import set_status, sync_source
from scrapeline.ops.worker import schedule_due
from scrapeline.registry.config import SourceConfig
from scrapeline.registry.instruction import ScrapeInstruction, compile_instruction
from scrapeline.store import tables as t

pytestmark = pytest.mark.integration

INSTRUCTION = """
id: {id}
name: Shop {id}
start_urls: [https://shop.example.com/{id}]
collect: "Every product with its link, title and price"
dataset: product
fields: {{url: url, title: text, price: money}}
schedule: {schedule}
usage: {{purpose: Scheduler test}}
"""


def compiled_source(source_id: str, schedule: str) -> SourceConfig:
    text = INSTRUCTION.format(id=source_id, schedule=schedule)
    ins = ScrapeInstruction.from_yaml(text)
    return compile_instruction(ins, DatasetRegistry.load(())).source


def test_schedule_due_never_enqueues_manual_sources(clean_db: Engine) -> None:
    sources = {
        sid: compiled_source(sid, schedule)
        for sid, schedule in (("shop-daily", "daily"), ("shop-manual", "manual"))
    }
    with clean_db.begin() as conn:
        for cfg in sources.values():
            assert sync_source(conn, cfg) == "draft"
    assert schedule_due(clean_db, sources) == []  # drafts never run on a schedule

    with clean_db.begin() as conn:
        for sid in sources:
            set_status(conn, sid, "active", "approved in test")
    assert schedule_due(clean_db, sources) == ["shop-daily"]
    much_later = datetime.now(UTC) + timedelta(days=30)
    assert schedule_due(clean_db, sources, now=much_later) == []  # daily one still queued
    with clean_db.connect() as conn:
        queued = conn.execute(select(t.job.c.source_id)).scalars().all()
    assert queued == ["shop-daily"]
