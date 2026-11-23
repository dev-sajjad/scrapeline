"""Helpers shared by command modules."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import typer


def driver(name: str) -> Any:
    from scrapeline.agents.llm import AnthropicDriver, NoopDriver
    from scrapeline.settings import get_settings

    settings = get_settings()
    if name == "noop":
        return NoopDriver()
    if name != "anthropic":
        raise typer.BadParameter("driver must be 'anthropic' or 'noop'")
    if settings.anthropic_api_key is None:
        raise typer.BadParameter(
            "ANTHROPIC_API_KEY is not set (.env); use --driver noop for a baseline"
        )
    return AnthropicDriver(settings, settings.model_repair, max_usd=settings.agent_max_usd_per_run)


def echo(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def make_runner() -> Any:
    from scrapeline.pipeline.runner import Runner
    from scrapeline.settings import get_settings
    from scrapeline.store.db import get_engine
    from scrapeline.store.raw import FilesystemRawStore

    settings = get_settings()
    return Runner(settings, get_engine(), FilesystemRawStore(settings.raw_store_dir))
