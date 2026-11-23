"""`scrapeline instruction ...`: turn a scrape instruction into a draft source."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import typer
import yaml

from scrapeline.cli._common import echo
from scrapeline.errors import ConfigError, DatasetError

if TYPE_CHECKING:
    from scrapeline.catalog.datasets import DatasetRegistry
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
    compiled = _compile(file, _registry(_datasets_dir(datasets_dir)))
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

    An existing spec.yaml is never replaced: it may already hold real selectors. When it
    belongs to another source, nothing is written.
    """
    from scrapeline.adapters.declarative import SITES_DIR
    from scrapeline.settings import get_settings

    datasets = _datasets_dir(datasets_dir)
    registry = _registry(datasets)
    compiled = _compile(file, registry)
    sources = sources_dir or get_settings().sources_dir
    spec_path = (sites_dir or SITES_DIR) / compiled.site_dir / "spec.yaml"
    conflict = site_dir_conflict(compiled, sources, spec_path, registry)
    if conflict:
        _fail(conflict, spec=str(spec_path))

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


def site_dir_conflict(
    compiled: CompiledInstruction, sources: Path, spec_path: Path, registry: DatasetRegistry
) -> str | None:
    """Why the source's site adapter directory belongs to another source, or None.

    Ids such as `shop-a` and `shop_a` share one directory, and an existing spec is kept, so
    the second source would silently run on the first one's selectors.
    """
    from scrapeline.registry.instruction import site_dir_name

    sid = compiled.source.id
    others = sorted(
        path.stem
        for path in sources.glob("*.yaml")
        if not path.name.startswith("_")
        and path.stem != sid
        and site_dir_name(path.stem) == compiled.site_dir
    )
    if others:
        return (
            f"source {others[0]!r} already uses the site adapter directory "
            f"{spec_path.parent}; choose another id"
        )
    if not spec_path.exists():
        return None
    try:
        spec: Any = yaml.safe_load(spec_path.read_text())
    except yaml.YAMLError:
        return None  # an unfinished hand-written spec; it is kept as it is
    if not isinstance(spec, dict):
        return None
    named = str(spec.get("schema", "Meeting"))  # SiteSpec's default schema
    try:
        named = registry.canonical_name(named)
    except DatasetError:
        named = named.lower()
    wanted = compiled.source.dataset_name
    if named == wanted:
        return None
    return (
        f"{spec_path} is a spec for dataset {named!r}, not {wanted!r}, so it may belong to "
        "another source; choose another id, or fix or delete that spec"
    )


def _datasets_dir(option: Path | None) -> Path:
    from scrapeline.settings import get_settings

    return option or get_settings().datasets_dir


def _registry(datasets_dir: Path) -> DatasetRegistry:
    from scrapeline.catalog.datasets import DatasetRegistry

    try:
        return DatasetRegistry.load((datasets_dir,))
    except ConfigError as exc:  # an invalid user dataset file
        _fail(str(exc))


def _compile(file: Path, registry: DatasetRegistry) -> CompiledInstruction:
    from scrapeline.registry.instruction import ScrapeInstruction, compile_instruction

    try:
        return compile_instruction(ScrapeInstruction.from_yaml(file.read_text()), registry)
    except ConfigError as exc:
        _fail(str(exc))


def _fail(message: str, **details: object) -> NoReturn:
    echo({"error": message, **details})
    raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    app.add_typer(instruction_app, name="instruction")
