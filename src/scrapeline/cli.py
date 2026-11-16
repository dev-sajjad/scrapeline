"""scrapeline command line. Thin: parses args, calls library code, prints JSON-ish summaries."""

from __future__ import annotations

import typer

from scrapeline import __version__
from scrapeline.ops.logging import configure_logging

app = typer.Typer(
    help="scrapeline: polite scraping runtime, ETL, quality checks and repair agents."
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


if __name__ == "__main__":
    app()
