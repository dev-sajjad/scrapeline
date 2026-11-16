"""scrapeline command line. Thin: parses args, calls library code, prints JSON summaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import typer

from scrapeline import __version__
from scrapeline.ops.logging import configure_logging

app = typer.Typer(
    help="scrapeline: polite scraping runtime, ETL, quality checks and repair agents.",
    no_args_is_help=True,
)
source_app = typer.Typer(help="Inspect and manage registered sources.", no_args_is_help=True)
agent_app = typer.Typer(
    help="AI agents (repair). Agents open branches; humans merge.", no_args_is_help=True
)
eval_app = typer.Typer(help="Agent evals over benchmark cases.", no_args_is_help=True)
drift_app = typer.Typer(help="Drift diagnosis (deterministic).", no_args_is_help=True)
app.add_typer(source_app, name="source")
app.add_typer(agent_app, name="agent")
app.add_typer(eval_app, name="eval")
app.add_typer(drift_app, name="drift")


def _driver(name: str) -> Any:
    from scrapeline.agents.llm import AnthropicDriver, NoopDriver
    from scrapeline.settings import get_settings

    settings = get_settings()
    if name == "noop":
        return NoopDriver()
    if name != "anthropic":
        raise typer.BadParameter("driver must be 'anthropic' or 'noop'")
    if settings.anthropic_api_key is None:
        raise typer.BadParameter(
            "ANTHROPIC_API_KEY is not set (.env); use --driver noop for a baseline"
        )
    return AnthropicDriver(settings, settings.model_repair, max_usd=settings.agent_max_usd_per_run)


def _echo(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _runner() -> Any:
    from scrapeline.pipeline.runner import Runner
    from scrapeline.settings import get_settings
    from scrapeline.store.db import get_engine
    from scrapeline.store.raw import FilesystemRawStore

    settings = get_settings()
    return Runner(settings, get_engine(), FilesystemRawStore(settings.raw_store_dir))


@app.callback()
def _main(
    log_level: str = typer.Option("INFO", help="Log level."),
    pretty: bool = typer.Option(False, help="Human-readable logs instead of JSON."),
) -> None:
    configure_logging(log_level, json=not pretty)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


# -- sources ---------------------------------------------------------------------------------


@source_app.command("list")
def source_list() -> None:
    """List sources from config/sources/*.yaml (status from DB when available)."""
    from scrapeline.registry import load_sources
    from scrapeline.settings import get_settings

    sources = load_sources(get_settings().sources_dir)
    _echo(
        [
            {"id": s.id, "platform": s.platform, "tenant": s.tenant, "yaml_status": s.status}
            for s in sources.values()
        ]
    )


@source_app.command("sync")
def source_sync() -> None:
    """Upsert every source config into the `sources` table."""
    from scrapeline.ops.runs import sync_source
    from scrapeline.registry import load_sources
    from scrapeline.settings import get_settings
    from scrapeline.store.db import get_engine

    with get_engine().begin() as conn:
        statuses = {
            sid: sync_source(conn, cfg)
            for sid, cfg in load_sources(get_settings().sources_dir).items()
        }
    _echo(statuses)


@source_app.command("set-status")
def source_set_status(
    source_id: str, status: str, note: str = typer.Option(None, help="Why.")
) -> None:
    """Move a source through draft -> active -> broken -> retired (validated transitions)."""
    from scrapeline.ops.runs import set_status
    from scrapeline.store.db import get_engine

    with get_engine().begin() as conn:
        set_status(conn, source_id, status, note)
    _echo({"source_id": source_id, "status": status})


# -- runs ------------------------------------------------------------------------------------


@app.command()
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
        summary = _runner().run(
            source_id, kind="backfill", since=_dt(date_from), until=_dt(date_to), force=force
        )
    else:
        summary = _runner().run(source_id, since=_dt(since), force=force)
    _echo(summary.as_dict())
    if summary.status != "succeeded":
        raise typer.Exit(code=1)


@app.command()
def reprocess(
    source_id: str,
    from_raw: bool = typer.Option(True, "--from-raw", help="Rebuild rows from stored raw."),
    since: str = typer.Option(None, help="Only raw fetched at/after this ISO datetime."),
) -> None:
    """Re-parse stored raw responses with the current adapter and reload (idempotent)."""
    if not from_raw:
        raise typer.BadParameter("only --from-raw is supported")
    summary = _runner().reprocess(source_id, since=_dt(since))
    _echo(summary.as_dict())
    if summary.status != "succeeded":
        raise typer.Exit(code=1)


@app.command()
def verify(source_id: str) -> None:
    """Parse the latest run's raw docs offline and compare with golden rows (deterministic)."""
    from scrapeline.adapters.base import load_adapter
    from scrapeline.pipeline.models import SCHEMAS
    from scrapeline.quality.golden import compare, load_golden
    from scrapeline.settings import PROJECT_ROOT, get_settings

    runner = _runner()
    cfg = runner.source(source_id)
    golden_name = cfg.options.get("golden") or cfg.id.removeprefix("fixture-")
    golden_path = PROJECT_ROOT / "tests" / "fixtures" / "golden" / f"{golden_name}.json"
    if not golden_path.exists():
        raise typer.BadParameter(f"no golden file {golden_path}")
    adapter = load_adapter(cfg)
    model = SCHEMAS[cfg.schema_]
    records = [
        rec
        for raw in runner.latest_raw_docs(source_id)
        for rec in adapter.parse(raw).records
        if type(rec) is model
    ]
    report = compare(records, load_golden(golden_path, get_settings().fixture_base_url))
    _echo(report.as_dict())
    if not report.passed:
        raise typer.Exit(code=1)


@app.command()
def documents(
    source_id: str, limit: int = typer.Option(20, help="Max documents per call.")
) -> None:
    """Download linked documents (PDF) through FetchClient and extract text (+OCR flag)."""
    from scrapeline.pipeline.documents import download_documents

    runner = _runner()
    cfg = runner.source(source_id)
    with runner._client(cfg, []) as client:
        stats = download_documents(
            runner.engine, client, runner.raw_store, source_id=cfg.id, limit=limit
        )
    _echo({"source_id": cfg.id, **stats})


@drift_app.command("diagnose")
def drift_diagnose(
    source_id: str, case: str = typer.Option(..., help="Directory of new pages.")
) -> None:
    """Which selectors of the source's spec stop matching on the given pages."""
    from pathlib import Path

    from scrapeline.adapters.declarative import SiteSpec, spec_path_for
    from scrapeline.quality.drift import diagnose

    spec = SiteSpec.from_yaml(spec_path_for(source_id).read_text())
    _echo(diagnose(spec, (Path(case) / "index.html").read_text()))


@agent_app.command("repair")
def agent_repair(
    source_id: str,
    case: str = typer.Option(..., help="Directory with the captured (drifted) pages."),
    driver: str = typer.Option("anthropic", help="anthropic | noop"),
    apply: bool = typer.Option(False, help="Commit the repaired spec on a new agent/* branch."),
    local_sandbox: bool = typer.Option(False, help="Run the tester in-process (no docker)."),
) -> None:
    """Repair a broken declarative adapter (max 3 attempts, then source -> broken)."""
    from pathlib import Path

    from scrapeline.agents.repairer import repair_case
    from scrapeline.settings import get_settings
    from scrapeline.store.db import get_engine

    result = repair_case(
        source_id,
        Path(case),
        driver=_driver(driver),
        settings=get_settings(),
        engine=get_engine(),
        apply=apply,
        sandbox_mode="local" if local_sandbox else "docker",
    )
    _echo(result.as_dict())
    if not result.passed:
        raise typer.Exit(code=1)


@eval_app.command("run")
def eval_run(
    suite: str = typer.Option("repair", help="Only 'repair' in v1."),
    driver: str = typer.Option("anthropic", help="anthropic | noop (baseline, no LLM)"),
    limit: int = typer.Option(None, help="Run only the first N cases."),
    local_sandbox: bool = typer.Option(False, help="Run the tester in-process (no docker)."),
    no_db: bool = typer.Option(False, help="Do not write evals.* / ops.agent_run rows."),
) -> None:
    """Run the repair benchmark (tests/sites/drift) and print §5.4 metrics."""
    from scrapeline.evals.runner import run_repair_suite
    from scrapeline.settings import get_settings
    from scrapeline.store.db import get_engine

    if suite != "repair":
        raise typer.BadParameter("only --suite repair exists in v1")
    result = run_repair_suite(
        driver=_driver(driver),
        settings=get_settings(),
        engine=None if no_db else get_engine(),
        limit=limit,
        sandbox_mode="local" if local_sandbox else "docker",
    )
    typer.echo(result.markdown())


@app.command()
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
    _echo({"runs": runs_, "open_incidents": incidents})


if __name__ == "__main__":
    app()
