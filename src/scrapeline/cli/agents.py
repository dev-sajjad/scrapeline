"""Drift diagnosis, AI agents and agent evals."""

from __future__ import annotations

import typer

from scrapeline.cli._common import driver as driver_for
from scrapeline.cli._common import echo

agent_app = typer.Typer(
    help="AI agents (repair). Agents open branches; humans merge.", no_args_is_help=True
)
eval_app = typer.Typer(help="Agent evals over benchmark cases.", no_args_is_help=True)
drift_app = typer.Typer(help="Drift diagnosis (deterministic).", no_args_is_help=True)


@drift_app.command("diagnose")
def drift_diagnose(
    source_id: str, case: str = typer.Option(..., help="Directory of new pages.")
) -> None:
    """Which selectors of the source's spec stop matching on the given pages."""
    from pathlib import Path

    from scrapeline.adapters.declarative import SiteSpec, spec_path_for
    from scrapeline.quality.drift import diagnose

    spec = SiteSpec.from_yaml(spec_path_for(source_id).read_text())
    echo(diagnose(spec, (Path(case) / "index.html").read_text()))


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
        driver=driver_for(driver),
        settings=get_settings(),
        engine=get_engine(),
        apply=apply,
        sandbox_mode="local" if local_sandbox else "docker",
    )
    echo(result.as_dict())
    if not result.passed:
        raise typer.Exit(code=1)


@agent_app.command("generate")
def agent_generate(
    source_id: str,
    pages: str = typer.Option(..., help="Directory of captured sample pages (scrapeline capture)."),
    instruction: str = typer.Option(..., help="What to collect."),
    driver: str = typer.Option("anthropic", help="anthropic | noop"),
    apply: bool = typer.Option(False, help="Commit spec + draft source YAML on an agent/* branch."),
    entry: str = typer.Option(None, help="Entry URL for the draft source YAML (with --apply)."),
    local_sandbox: bool = typer.Option(False, help="Run the tester in-process (no docker)."),
) -> None:
    """Generate a declarative site adapter for a new (long-tail) source from sample pages."""
    from pathlib import Path

    from scrapeline.agents.generator import generate_site
    from scrapeline.settings import get_settings
    from scrapeline.store.db import get_engine

    source_yaml = None
    if apply:
        if not entry:
            raise typer.BadParameter("--apply needs --entry")
        source_yaml = draft_source_yaml(source_id, entry, instruction)
    result = generate_site(
        source_id,
        Path(pages),
        instruction=instruction,
        driver=driver_for(driver),
        settings=get_settings(),
        engine=get_engine(),
        apply=apply,
        source_yaml=source_yaml,
        sandbox_mode="local" if local_sandbox else "docker",
    )
    echo(result.as_dict())
    if not result.passed:
        raise typer.Exit(code=1)


def draft_source_yaml(source_id: str, entry: str, instruction: str) -> str:
    """Draft source config for human completion. safe_dump: instruction text is untrusted."""
    from urllib.parse import urlsplit

    import yaml

    doc = {
        "id": source_id,
        "name": f"{source_id} (agent-generated, draft)",
        "platform": "custom",
        "kind": "html",
        "entry": entry,
        "jurisdiction": {"id": "xx-unknown", "name": "TODO-human", "country": "US"},
        "allowed_hosts": [urlsplit(entry).hostname or ""],
        "cadence": "daily",
        "politeness": {"rps": 1, "concurrency": 1, "respect_robots": True},
        "legal": {
            "terms_checked": "1970-01-01",
            "notes": "TODO(human): review terms + robots.txt before activating.",
        },
        "schema": "Meeting",
        "adapter": "scrapeline.adapters.declarative:DeclarativeAdapter",
        "status": "draft",
        "owner": "agent",
        "config_version": 1,
        "instruction": {"collect": instruction, "schema": "Meeting"},
    }
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


@eval_app.command("run")
def eval_run(
    suite: str = typer.Option("repair", help="repair | generation"),
    driver: str = typer.Option("anthropic", help="anthropic | noop (baseline, no LLM)"),
    limit: int = typer.Option(None, help="Run only the first N cases."),
    local_sandbox: bool = typer.Option(False, help="Run the tester in-process (no docker)."),
    no_db: bool = typer.Option(False, help="Do not write evals.* / ops.agent_run rows."),
) -> None:
    """Run the repair benchmark (tests/sites/drift) and print eval metrics."""
    from scrapeline.evals.runner import run_repair_suite
    from scrapeline.settings import get_settings
    from scrapeline.store.db import get_engine

    if suite == "generation":
        from scrapeline.evals.runner import run_generation_suite

        echo(
            run_generation_suite(
                driver=driver_for(driver),
                settings=get_settings(),
                engine=None if no_db else get_engine(),
                limit=limit,
                sandbox_mode="local" if local_sandbox else "docker",
            )["metrics"]
        )
        return
    if suite != "repair":
        raise typer.BadParameter("--suite must be repair or generation")
    result = run_repair_suite(
        driver=driver_for(driver),
        settings=get_settings(),
        engine=None if no_db else get_engine(),
        limit=limit,
        sandbox_mode="local" if local_sandbox else "docker",
    )
    typer.echo(result.markdown())


def register(app: typer.Typer) -> None:
    app.add_typer(agent_app, name="agent")
    app.add_typer(eval_app, name="eval")
    app.add_typer(drift_app, name="drift")
