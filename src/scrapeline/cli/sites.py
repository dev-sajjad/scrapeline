"""`scrapeline site ...`: list sites, classify them from stored pages, override by hand."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

import typer

from scrapeline.cli._common import echo
from scrapeline.errors import ConfigError

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

    from scrapeline.catalog.sites import Classification
    from scrapeline.store.raw import RawStore

site_app = typer.Typer(help="Inspect, classify and override site categories.", no_args_is_help=True)


def _exit_with(exc: ConfigError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1) from exc


def _sources_by_site(conn: Connection) -> dict[int, list[str]]:
    from sqlalchemy import select

    from scrapeline.store import tables as t

    grouped: dict[int, list[str]] = {}
    rows = conn.execute(
        select(t.sources.c.site_id, t.sources.c.id)
        .where(t.sources.c.site_id.is_not(None))
        .order_by(t.sources.c.id)
    )
    for site_id, source_id in rows:
        grouped.setdefault(site_id, []).append(source_id)
    return grouped


def _result(c: Classification) -> dict[str, Any]:
    return {
        "category": c.category,
        "confidence": c.confidence,
        "method": c.method,
        "tags": list(c.tags),
        "platform": c.platform,
        "country": c.country,
        "language": c.language,
        "evidence": [
            {"signal": e.signal, "value": e.value, "weight": e.weight} for e in c.evidence
        ],
    }


@site_app.command("list")
def site_list(
    category: str | None = typer.Option(None, help="Only sites in this category."),
) -> None:
    """List known sites with their category, how it was decided, and their sources."""
    from sqlalchemy import select

    from scrapeline.store import tables as t
    from scrapeline.store.db import get_engine

    query = select(t.site).order_by(t.site.c.host)
    if category is not None:
        query = query.where(t.site.c.category == category)
    with get_engine().connect() as conn:
        sources = _sources_by_site(conn)
        rows = conn.execute(query).fetchall()
    echo(
        [
            {
                "host": r.host,
                "category": r.category,
                "confidence": r.category_confidence,
                "method": r.category_method,
                "tags": r.tags,
                "platform": r.platform,
                "country": r.country,
                "language": r.language,
                "classified_at": r.classified_at,
                "sources": sources.get(r.id, []),
            }
            for r in rows
        ]
    )


def _classify_one(
    engine: Engine, site_id: int, host: str, raw_store: RawStore, *, pages: int, dry_run: bool
) -> dict[str, Any]:
    from scrapeline.catalog.classify import (
        CLASSIFIER_ACTOR,
        DeterministicClassifier,
        apply_classification,
        stored_pages,
    )
    from scrapeline.catalog.sites import UNCLASSIFIED

    with engine.begin() as conn:
        docs = stored_pages(conn, site_id, raw_store, per_source=pages)
        classification = DeterministicClassifier().classify(host, docs)
        if dry_run:
            outcome = "dry_run"
        elif not docs and classification.category == UNCLASSIFIED:
            # Pages that are no longer stored are not evidence against an earlier result.
            outcome = "no_pages"
        else:
            outcome = apply_classification(conn, site_id, classification, CLASSIFIER_ACTOR)
        stored = _stored(conn, site_id) if outcome in ("locked", "kept") else {}
    return {"host": host, "outcome": outcome, "pages": len(docs)} | _result(classification) | stored


def _stored(conn: Connection, site_id: int) -> dict[str, Any]:
    """What the site row keeps when the computed result was not stored (human or LLM call)."""
    from sqlalchemy import select

    from scrapeline.store import tables as t

    row = conn.execute(
        select(t.site.c.category, t.site.c.category_method).where(t.site.c.id == site_id)
    ).one()
    return {"stored_category": row.category, "stored_method": row.category_method}


@site_app.command("classify")
def site_classify(
    host: str | None = typer.Argument(None, help="The site's host, e.g. books.toscrape.com."),
    all_sites: bool = typer.Option(False, "--all", help="Classify every known site."),
    pages: int = typer.Option(5, min=1, help="Latest stored pages to read per source."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the result; store nothing."),
) -> None:
    """Classify sites from domain rules and the pages their sources already captured.

    Reads raw.fetch_log and the raw store; never fetches.
    A category set by a human is kept.
    A site with no stored pages and no domain rule is left as it is.
    """
    from sqlalchemy import select

    from scrapeline.catalog.sites import site_host
    from scrapeline.settings import get_settings
    from scrapeline.store import tables as t
    from scrapeline.store.db import get_engine
    from scrapeline.store.raw import FilesystemRawStore

    if (host is None) == (not all_sites):
        raise typer.BadParameter("give either a host or --all")
    query = select(t.site.c.id, t.site.c.host).order_by(t.site.c.host)
    if host is not None:
        try:
            query = query.where(t.site.c.host == site_host(host))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    engine = get_engine()
    with engine.connect() as conn:
        sites = conn.execute(query).fetchall()
    if host is not None and not sites:
        _exit_with(ConfigError(f"unknown site {host!r}; register or sync a source on it first"))
    raw_store = FilesystemRawStore(get_settings().raw_store_dir)
    results = [
        _classify_one(engine, s.id, s.host, raw_store, pages=pages, dry_run=dry_run) for s in sites
    ]
    echo(results[0] if host is not None else results)


@site_app.command("override")
def site_override(
    host: str,
    category: str,
    note: str = typer.Option(..., help="Why; kept as the evidence for the decision."),
    actor: str = typer.Option("cli", help="Who decided, as recorded in the audit log."),
) -> None:
    """Set a site's category by hand. Automatic classification never changes it afterwards."""
    from scrapeline.catalog.classify import override_category
    from scrapeline.catalog.sites import site_host
    from scrapeline.store.db import get_engine

    try:
        with get_engine().begin() as conn:
            site_id = override_category(conn, host, category, f"human:{actor}", note)
    except ConfigError as exc:
        _exit_with(exc)
    echo({"site_id": site_id, "host": site_host(host), "category": category, "method": "human"})


def register(app: typer.Typer) -> None:
    app.add_typer(site_app, name="site")
