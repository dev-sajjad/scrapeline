"""scrapeline command line. Thin: parses args, calls library code, prints JSON summaries.

Each module in this package owns one command area and exposes `register(app)`; modules are
discovered automatically, so adding a command area never edits a shared file.
"""

from __future__ import annotations

import importlib
import pkgutil

import typer

from scrapeline import __version__
from scrapeline.ops.logging import configure_logging

app = typer.Typer(
    help="scrapeline: polite scraping runtime, ETL, quality checks and repair agents.",
    no_args_is_help=True,
)


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


def _register_all() -> None:
    for module in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}").register(app)


_register_all()
