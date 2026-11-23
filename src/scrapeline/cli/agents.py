"""Drift diagnosis, AI agents and agent evals."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from scrapeline.cli._common import driver as driver_for
from scrapeline.cli._common import echo
from scrapeline.errors import ConfigError

if TYPE_CHECKING:
    from scrapeline.registry.instruction import CompiledInstruction

agent_app = typer.Typer(
    help="AI agents (repair, generate). Agents open branches; humans merge.", no_args_is_help=True
)
eval_app = typer.Typer(help="Agent evals over benchmark cases.", no_args_is_help=True)
drift_app = typer.Typer(help="Drift diagnosis (deterministic).", no_args_is_help=True)


@drift_app.command("diagnose")
def drift_diagnose(
    source_id: str, case: str = typer.Option(..., help="Directory of new pages.")
) -> None:
    """Which selectors of the source's spec stop matching on the given pages."""
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
    source_id: str = typer.Argument(None, help="Source id. Default: the instruction's id."),
    pages: str = typer.Option(..., help="Directory of captured sample pages (scrapeline capture)."),
    instruction: Path = typer.Option(
        None, exists=True, dir_okay=False, help="Scrape instruction YAML (docs/instructions.md)."
    ),
    instruction_text: str = typer.Option(
        None, help="What to collect, as free text (instead of --instruction)."
    ),
    driver: str = typer.Option("anthropic", help="anthropic | noop"),
    apply: bool = typer.Option(False, help="Commit spec + draft source YAML on an agent/* branch."),
    entry: str = typer.Option(
        None, help="Entry URL for the draft source YAML (with --instruction-text and --apply)."
    ),
    local_sandbox: bool = typer.Option(False, help="Run the tester in-process (no docker)."),
) -> None:
    """Generate a declarative site adapter for a new (long-tail) source from sample pages."""
    from scrapeline.agents.generator import generate_site
    from scrapeline.settings import get_settings
    from scrapeline.store.db import get_engine

    source_yaml = None
    if instruction is not None:
        if instruction_text or entry:
            raise typer.BadParameter("use --instruction, or --instruction-text with --entry")
        compiled = _compile_for_agent(instruction, source_id, apply)
        source_id = compiled.source.id
        brief = _generator_brief(compiled)
        if apply:
            source_yaml = agent_source_yaml(compiled)
    else:
        if not source_id or not instruction_text:
            raise typer.BadParameter("give SOURCE_ID and --instruction-text, or --instruction")
        brief = instruction_text
        if apply:
            if not entry:
                raise typer.BadParameter("--apply needs --entry")
            try:
                source_yaml = draft_source_yaml(source_id, entry, instruction_text)
            except ConfigError as exc:
                raise typer.BadParameter(str(exc)) from exc
    result = generate_site(
        source_id,
        Path(pages),
        instruction=brief,
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


def _compile_for_agent(path: Path, source_id: str | None, apply: bool) -> CompiledInstruction:
    from scrapeline.adapters.declarative import spec_path_for
    from scrapeline.catalog.datasets import default_registry
    from scrapeline.cli.instructions import site_dir_conflict
    from scrapeline.registry.instruction import (
        GENERATOR_DATASETS,
        ScrapeInstruction,
        compile_instruction,
    )
    from scrapeline.settings import get_settings

    registry = default_registry()
    try:
        compiled = compile_instruction(ScrapeInstruction.from_yaml(path.read_text()), registry)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if source_id and source_id != compiled.source.id:
        raise typer.BadParameter(
            f"SOURCE_ID {source_id!r} differs from the instruction's id {compiled.source.id!r}"
        )
    # The generator starts from a Meeting skeleton and its tools validate Meeting specs, so
    # any other dataset would burn paid attempts on a spec that cannot pass. A new or proposed
    # dataset type is never supported, so it is also never committed before its approval.
    if not compiled.generator_supported:
        raise typer.BadParameter(
            f"the generator agent writes only {', '.join(sorted(GENERATOR_DATASETS))} specs "
            f"for now, not {compiled.source.dataset_name!r}. Run `scrapeline instruction apply "
            f"{path}` and fill the spec.yaml skeleton by hand, or wait until the generator "
            "works from the dataset registry"
        )
    if apply:
        spec = spec_path_for(compiled.source.id)
        conflict = site_dir_conflict(compiled, get_settings().sources_dir, spec, registry)
        if conflict:
            raise typer.BadParameter(conflict)
    return compiled


def _generator_brief(compiled: CompiledInstruction) -> str:
    """The instruction text plus the trusted dataset and field names the spec must produce."""
    collect = compiled.source.instruction.collect if compiled.source.instruction else ""
    if not compiled.fields:
        return collect
    return (
        f"{collect}\n\nDataset type: {compiled.source.dataset_name}. "
        f"Fields to extract: {', '.join(compiled.fields)}."
    )


def agent_source_yaml(compiled: CompiledInstruction) -> str:
    """Draft source YAML for an agent branch: the adapter's author is the agent."""
    from scrapeline.registry.instruction import render_source_yaml

    return render_source_yaml(compiled.source.model_copy(update={"owner": "agent"}))


def draft_source_yaml(source_id: str, entry: str, instruction: str) -> str:
    """Draft meeting source for the free-text flags, compiled like any instruction.

    Raises ConfigError when the text or URL is not a valid instruction.
    """
    from pydantic import ValidationError

    from scrapeline.catalog.datasets import default_registry
    from scrapeline.registry.instruction import ScrapeInstruction, compile_instruction

    try:
        ins = ScrapeInstruction.model_validate(
            {
                "id": source_id,
                "name": f"{source_id} (agent-generated, draft)",
                "start_urls": [entry],
                "collect": instruction,
                "dataset": "meeting",
                "usage": {},
            }
        )
    except ValidationError as exc:
        raise ConfigError(f"invalid instruction: {exc}") from exc
    return agent_source_yaml(compile_instruction(ins, default_registry()))


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
