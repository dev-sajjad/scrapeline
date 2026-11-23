"""`scrapeline dataset ...`: inspect dataset types and record them in the catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

import typer

from scrapeline.cli._common import echo
from scrapeline.errors import DatasetError

if TYPE_CHECKING:
    from scrapeline.catalog.datasets import DatasetRegistry, DatasetType

dataset_app = typer.Typer(help="Inspect and sync dataset types.", no_args_is_help=True)


def _summary(ds: DatasetType) -> dict[str, Any]:
    return {
        "name": ds.name,
        "version": ds.version,
        "pack": ds.pack,
        "storage": ds.storage,
        "description": ds.description,
        "fields": len(ds.fields),
        "key_fields": list(ds.key_fields),
    }


def _exit_with(exc: DatasetError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1) from exc


def _registry() -> DatasetRegistry:
    """The dataset registry; a malformed dataset file is a clean error, not a traceback."""
    from scrapeline.catalog.datasets import default_registry

    try:
        return default_registry()
    except DatasetError as exc:
        _exit_with(exc)


@dataset_app.command("list")
def dataset_list() -> None:
    """List every known dataset type (built-in packs, typed models, user types)."""
    echo([_summary(ds) for ds in _registry()])


@dataset_app.command("show")
def dataset_show(
    name: str,
    version: int = typer.Option(None, help="A specific version; the latest by default."),
) -> None:
    """Show one dataset type's full definition."""
    registry = _registry()
    try:
        ds = registry.get(name, version)
    except DatasetError as exc:
        raise typer.BadParameter(str(exc)) from exc
    echo(
        ds.model_dump(mode="json")
        | {
            "key_fields": list(ds.key_fields),
            "pii_fields": list(ds.pii_fields),
            "definition_hash": ds.definition_hash,
        }
    )


@dataset_app.command("sync")
def dataset_sync() -> None:
    """Record every dataset type in the catalog; a changed definition must bump its version."""
    from scrapeline.catalog.repository import sync_dataset_types
    from scrapeline.store.db import get_engine

    registry = _registry()
    try:
        with get_engine().begin() as conn:
            added = sync_dataset_types(conn, registry)
    except DatasetError as exc:
        _exit_with(exc)
    echo({"added": added, "known": len(list(registry))})


def register(app: typer.Typer) -> None:
    app.add_typer(dataset_app, name="dataset")
