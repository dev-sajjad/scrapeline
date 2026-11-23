"""`scrapeline instruction ...`: turn a scrape instruction into a draft source."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer

from scrapeline.cli._common import echo
from scrapeline.errors import ConfigError

if TYPE_CHECKING:
    from scrapeline.registry.instruction import CompiledInstruction

instruction_app = typer.Typer(
    help="Scrape instructions: validate and compile them into draft sources.",
    no_args_is_help=True,
)


@instruction_app.command("validate")
def instruction_validate(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Instruction YAML."),
    datasets_dir: Path | None = typer.Option(
        None, help="User dataset types. Default: settings.datasets_dir."
    ),
) -> None:
    """Compile an instruction and print what it would become. Writes nothing."""
    compiled = _compile(file, _datasets_dir(datasets_dir))
    echo({"valid": True, **compiled.summary()})


@instruction_app.command("apply")
def instruction_apply(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Instruction YAML."),
    force: bool = typer.Option(False, help="Overwrite existing config files (never a spec)."),
    sources_dir: Path | None = typer.Option(
        None, help="Source configs. Default: settings.sources_dir."
    ),
    datasets_dir: Path | None = typer.Option(
        None, help="User dataset types. Default: settings.datasets_dir."
    ),
    sites_dir: Path | None = typer.Option(
        None, help="Site adapters. Default: the declarative adapters' sites directory."
    ),
) -> None:
    """Write the draft source config, the new dataset type (if any) and a spec skeleton.

    An existing spec.yaml is never replaced: it may already hold real selectors.
    """
    from scrapeline.adapters.declarative import SITES_DIR
    from scrapeline.settings import get_settings

    datasets = _datasets_dir(datasets_dir)
    compiled = _compile(file, datasets)
    sources = sources_dir or get_settings().sources_dir
    spec_path = (sites_dir or SITES_DIR) / compiled.site_dir / "spec.yaml"

    files = {sources / f"{compiled.source.id}.yaml": compiled.source_yaml()}
    dataset_yaml = compiled.dataset_yaml()
    if dataset_yaml is not None:
        files[datasets / f"{compiled.source.dataset_name}.yaml"] = dataset_yaml
    existing = [str(path) for path in files if path.exists()]
    if existing and not force:
        _fail("refusing to overwrite existing files; use --force", existing=existing)

    kept: list[str] = []
    if spec_path.exists():
        kept.append(str(spec_path))
    else:
        files[spec_path] = compiled.spec_skeleton
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    echo(
        {
            "applied": True,
            "written": [str(path) for path in files],
            "kept": kept,
            **compiled.summary(),
        }
    )


def _datasets_dir(option: Path | None) -> Path:
    from scrapeline.settings import get_settings

    return option or get_settings().datasets_dir


def _compile(file: Path, datasets_dir: Path) -> CompiledInstruction:
    from scrapeline.catalog.datasets import DatasetRegistry
    from scrapeline.registry.instruction import ScrapeInstruction, compile_instruction

    try:
        instruction = ScrapeInstruction.from_yaml(file.read_text())
        return compile_instruction(instruction, DatasetRegistry.load((datasets_dir,)))
    except ConfigError as exc:
        _fail(str(exc))


def _fail(message: str, **details: object) -> NoReturn:
    echo({"error": message, **details})
    raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    app.add_typer(instruction_app, name="instruction")
