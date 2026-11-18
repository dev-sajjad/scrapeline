"""Trusted worker loop and cron-driven scheduler."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, func, select

from scrapeline.ops import queue
from scrapeline.ops.logging import bind_context, get_logger
from scrapeline.registry.config import SourceConfig
from scrapeline.store import tables as t

log = get_logger(__name__)
CADENCE = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(weeks=1)}


def schedule_due(
    engine: Engine, sources: dict[str, SourceConfig], now: datetime | None = None
) -> list[str]:
    """Enqueue a `run` job for every active source whose cadence has elapsed since its last run."""
    now = now or datetime.now(UTC)
    enqueued = []
    with engine.connect() as conn:
        status = dict(conn.execute(select(t.sources.c.id, t.sources.c.status)).all())
        last = dict(
            conn.execute(
                select(t.run.c.source_id, func.max(t.run.c.started_at))
                .where(t.run.c.kind == "incremental")
                .group_by(t.run.c.source_id)
            ).all()
        )
    for sid, cfg in sources.items():
        if status.get(sid, cfg.status) != "active":
            continue
        every = CADENCE.get(cfg.cadence, timedelta(days=1))
        if last.get(sid) is None or now - last[sid] >= every:
            if queue.enqueue(engine, "run", sid) is not None:
                enqueued.append(sid)
    return enqueued


def work(
    engine: Engine,
    handle: Callable[[queue.Job], None],
    *,
    once: bool = False,
    idle_sleep: float = 5.0,
    max_jobs: int | None = None,
) -> int:
    wid = queue.worker_id()
    done = 0
    while max_jobs is None or done < max_jobs:
        job = queue.claim(engine, wid)
        if job is None:
            if once:
                break
            time.sleep(idle_sleep)
            continue
        with bind_context(job_id=job.id, source_id=job.source_id):
            try:
                handle(job)
            except Exception as exc:  # job failure must never kill the worker
                status = queue.fail(engine, job, f"{type(exc).__name__}: {exc}")
                log.error("job.failed", status=status, error=str(exc)[:500])
            else:
                queue.complete(engine, job)
                log.info("job.done")
        done += 1
    return done
