"""Live events for the panel: one LISTEN connection per process, fanned out to subscribers."""

from __future__ import annotations

from fastapi import FastAPI


async def shutdown(app: FastAPI) -> None:
    """Stop the event hub if one was started. Called when the app shuts down."""
    return None
