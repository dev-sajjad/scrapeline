"""`scrapeline lake ...`: export operational data to the Parquet lake and inspect it."""

from __future__ import annotations

from dataclasses import asdict

import typer

from scrapeline.cli._common import echo
from scrapeline.errors import ConfigError

lake_app = typer.Typer(help="Export to the Parquet lake that dbt reads.", no_args_is_help=True)


@lake_app.command("export")
def lake_export(
    table: list[str] | None = typer.Option(
        None, "--table", help="Export only this table (repeatable); every table by default."
    ),
) -> None:
    """Append new rows of the append tables and rewrite the snapshot tables."""
    from scrapeline.analytics.export import ParquetLakeExporter
    from scrapeline.analytics.layout import TABLES
    from scrapeline.catalog.datasets import default_registry
    from scrapeline.settings import get_settings
    from scrapeline.store.db import get_engine

    unknown = sorted(set(table or ()) - set(TABLES))
    if unknown:
        raise typer.BadParameter(f"unknown table(s) {unknown}; known: {sorted(TABLES)}")
    try:
        exporter = ParquetLakeExporter(get_engine(), get_settings().lake_dir, default_registry())
        report = exporter.export(tables=table or None)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    echo(asdict(report))


@lake_app.command("status")
def lake_status() -> None:
    """Files and rows per lake table, with each table's export state."""
    from scrapeline.analytics.export import lake_status as status
    from scrapeline.settings import get_settings

    try:
        echo(status(get_settings().lake_dir))
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


def register(app: typer.Typer) -> None:
    app.add_typer(lake_app, name="lake")
