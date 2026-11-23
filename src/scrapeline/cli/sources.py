"""`scrapeline source ...`: inspect and manage registered sources."""

from __future__ import annotations

import typer

from scrapeline.cli._common import echo

source_app = typer.Typer(help="Inspect and manage registered sources.", no_args_is_help=True)


@source_app.command("list")
def source_list() -> None:
    """List sources from config/sources/*.yaml (status from DB when available)."""
    from scrapeline.registry import load_sources
    from scrapeline.settings import get_settings

    sources = load_sources(get_settings().sources_dir)
    echo(
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
    echo(statuses)


@source_app.command("set-status")
def source_set_status(
    source_id: str, status: str, note: str = typer.Option(None, help="Why.")
) -> None:
    """Move a source through draft -> active -> broken -> retired (validated transitions)."""
    from scrapeline.ops.runs import set_status
    from scrapeline.store.db import get_engine

    with get_engine().begin() as conn:
        set_status(conn, source_id, status, note)
    echo({"source_id": source_id, "status": status})


def register(app: typer.Typer) -> None:
    app.add_typer(source_app, name="source")
