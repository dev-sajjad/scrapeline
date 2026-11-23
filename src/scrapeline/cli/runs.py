"""Run, reprocess, verify and inspect one source."""

from __future__ import annotations

import typer

from scrapeline.cli._common import echo, make_runner, parse_dt


def run(
    source_id: str,
    since: str = typer.Option(None, help="ISO datetime; overrides the stored watermark."),
    backfill: bool = typer.Option(False, help="Explicit backfill over --from/--to."),
    date_from: str = typer.Option(None, "--from", help="Backfill start (ISO date)."),
    date_to: str = typer.Option(None, "--to", help="Backfill end (ISO date, exclusive)."),
    force: bool = typer.Option(False, help="Run even if the source is broken/retired."),
) -> None:
    """Run one source: fetch -> raw -> parse -> load -> checks."""
    if backfill:
        if not date_from:
            raise typer.BadParameter("--backfill needs --from")
        summary = make_runner().run(
            source_id,
            kind="backfill",
            since=parse_dt(date_from),
            until=parse_dt(date_to),
            force=force,
        )
    else:
        summary = make_runner().run(source_id, since=parse_dt(since), force=force)
    echo(summary.as_dict())
    if summary.status != "succeeded":
        raise typer.Exit(code=1)


def reprocess(
    source_id: str,
    from_raw: bool = typer.Option(True, "--from-raw", help="Rebuild rows from stored raw."),
    since: str = typer.Option(None, help="Only raw fetched at/after this ISO datetime."),
) -> None:
    """Re-parse stored raw responses with the current adapter and reload (idempotent)."""
    if not from_raw:
        raise typer.BadParameter("only --from-raw is supported")
    summary = make_runner().reprocess(source_id, since=parse_dt(since))
    echo(summary.as_dict())
    if summary.status != "succeeded":
        raise typer.Exit(code=1)


def verify(source_id: str) -> None:
    """Parse the latest run's raw docs offline and compare with golden rows (deterministic)."""
    from scrapeline.adapters.base import load_adapter
    from scrapeline.catalog.datasets import resolve_model
    from scrapeline.quality.golden import compare, load_golden
    from scrapeline.settings import PROJECT_ROOT, get_settings

    runner = make_runner()
    cfg = runner.source(source_id)
    golden_name = cfg.options.get("golden") or cfg.id.removeprefix("fixture-")
    golden_path = PROJECT_ROOT / "tests" / "fixtures" / "golden" / f"{golden_name}.json"
    if not golden_path.exists():
        raise typer.BadParameter(f"no golden file {golden_path}")
    adapter = load_adapter(cfg)
    model = resolve_model(cfg.dataset_name)
    records = [
        rec
        for raw in runner.latest_raw_docs(source_id)
        for rec in adapter.parse(raw).records
        if type(rec) is model
    ]
    report = compare(records, load_golden(golden_path, get_settings().fixture_base_url))
    echo(report.as_dict())
    if not report.passed:
        raise typer.Exit(code=1)


def documents(
    source_id: str, limit: int = typer.Option(20, help="Max documents per call.")
) -> None:
    """Download linked documents (PDF) through FetchClient and extract text (+OCR flag)."""
    from scrapeline.pipeline.documents import download_documents

    runner = make_runner()
    cfg = runner.source(source_id)
    with runner._client(cfg, []) as client:
        stats = download_documents(
            runner.engine, client, runner.raw_store, source_id=cfg.id, limit=limit
        )
    echo({"source_id": cfg.id, **stats})


def capture(
    source_id: str,
    url: list[str] = typer.Option(None, help="Extra URLs (pages 2..n) to capture."),
    render: bool = typer.Option(False, help="Render with Playwright (JS sites)."),
) -> None:
    """Trusted worker step: capture sample pages for an agent into data/samples/<source>/.

    Uses the source's politeness policy (URL policy, robots, rate limit). Agents never fetch.
    """
    from pathlib import Path

    from scrapeline.fetch.browser import BrowserPool

    runner = make_runner()
    cfg = runner.source(source_id)
    out_dir = Path("data/samples") / source_id
    out_dir.mkdir(parents=True, exist_ok=True)
    urls = [cfg.entry, *(url or [])]
    saved = []
    with runner._client(cfg, []) as client:
        pool = (
            BrowserPool(client.policy, client.limiter, client.user_agent, robots=client.robots)
            if render
            else None
        )
        try:
            for i, u in enumerate(urls):
                resp = pool.render(u) if pool else client.get(u)
                runner.raw_store.put(cfg.id, resp.content, "html", resp.fetched_at)
                name = "index.html" if i == 0 else Path(u.split("?")[0]).name or f"page{i + 1}.html"
                (out_dir / name).write_bytes(resp.content)
                saved.append(str(out_dir / name))
        finally:
            if pool:
                pool.close()
    echo({"source_id": cfg.id, "saved": saved})


def status(source_id: str = typer.Argument(None)) -> None:
    """Latest runs, checks and open incidents."""
    from sqlalchemy import select

    from scrapeline.store import tables as t
    from scrapeline.store.db import get_engine

    with get_engine().connect() as conn:
        q = select(t.run).order_by(t.run.c.started_at.desc()).limit(10)
        if source_id:
            q = q.where(t.run.c.source_id == source_id)
        runs_ = [
            {
                "run_id": r.id,
                "source": r.source_id,
                "kind": r.kind,
                "status": r.status,
                "started": r.started_at,
                "loaded": r.stats.get("loaded"),
                "error": r.error,
            }
            for r in conn.execute(q)
        ]
        iq = select(t.incident).where(t.incident.c.status != "closed")
        if source_id:
            iq = iq.where(t.incident.c.source_id == source_id)
        incidents = [
            {
                "id": i.id,
                "source": i.source_id,
                "kind": i.kind,
                "summary": i.summary,
                "opened": i.opened_at,
            }
            for i in conn.execute(iq)
        ]
    echo({"runs": runs_, "open_incidents": incidents})


def register(app: typer.Typer) -> None:
    for command in (run, reprocess, verify, documents, capture, status):
        app.command()(command)
