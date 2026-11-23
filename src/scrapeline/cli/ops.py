"""Scheduler and worker entry points."""

from __future__ import annotations

import typer

from scrapeline.cli._common import echo, make_runner


def schedule() -> None:
    """Enqueue run jobs for active sources whose cadence elapsed (run from cron)."""
    from scrapeline.ops.worker import schedule_due
    from scrapeline.registry import load_sources
    from scrapeline.settings import get_settings
    from scrapeline.store.db import get_engine

    echo({"enqueued": schedule_due(get_engine(), load_sources(get_settings().sources_dir))})


def worker(
    concurrency: int = typer.Option(
        1, help="Kept for compose compatibility; v1 runs 1 job at a time."
    ),
    once: bool = typer.Option(False, help="Drain the queue, then exit."),
) -> None:
    """Trusted worker: claim jobs (SKIP LOCKED), run them, retry with backoff, dead-letter."""
    from scrapeline.ops.queue import Job
    from scrapeline.ops.worker import work

    runner = make_runner()

    def handle(job: Job) -> None:
        if job.kind != "run" or not job.source_id:
            raise ValueError(f"unknown job kind {job.kind}")
        summary = runner.run(job.source_id)
        if summary.status != "succeeded":
            raise RuntimeError(summary.error or "run failed quality checks")

    echo({"jobs_processed": work(runner.engine, handle, once=once)})


def register(app: typer.Typer) -> None:
    app.command()(schedule)
    app.command()(worker)
