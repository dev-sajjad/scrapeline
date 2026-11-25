"""Panel application factory.

    uv run uvicorn scrapeline.serve.panel.app:create_app --factory --port 8000

Every module in `pages/` exposes `router` and is included automatically. The JSON read API
is mounted under /api. Pages only read; actions are queued as jobs for the worker (ADR-0008).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from scrapeline import __version__
from scrapeline.ops.logging import get_logger
from scrapeline.serve import api
from scrapeline.serve.panel import live, pages
from scrapeline.serve.panel.deps import STATIC_DIR, render
from scrapeline.settings import Settings, get_settings
from scrapeline.store.db import get_engine

log = get_logger(__name__)

# Pages load scripts and styles from /static only; nothing inline, nothing third party.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
UNDEFINED_TABLE = "42P01"


class SecurityHeaders:
    """Adds browser hardening headers. Plain ASGI, so streamed responses pass through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The mounted API serves its own docs page, which needs its own policy.
        with_csp = not scope["path"].startswith("/api")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                if with_csp:
                    headers.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def page_routers() -> list[APIRouter]:
    """The router of every module in `pages/`, in module name order."""
    names = sorted(info.name for info in pkgutil.iter_modules(pages.__path__))
    return [importlib.import_module(f"{pages.__name__}.{name}").router for name in names]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await live.shutdown(app)


def create_app(engine: Engine | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="scrapeline panel",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine or get_engine(settings.database_url)

    app.add_middleware(SecurityHeaders)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.panel_allowed_hosts)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/api", api.app)
    for router in page_routers():
        app.include_router(router)

    @app.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
    def healthz() -> str:
        """Liveness for the container healthcheck; does not touch the database."""
        return "ok"

    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_exception_handler(OperationalError, _database_unavailable)
    app.add_exception_handler(ProgrammingError, _schema_missing)
    return app


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    return render(
        request,
        "error.html",
        {"title": f"{exc.status_code}", "status": exc.status_code, "message": exc.detail},
        status_code=exc.status_code,
    )


async def _invalid_request(request: Request, exc: Exception) -> HTMLResponse:
    assert isinstance(exc, RequestValidationError)
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'][1:])}: {error['msg']}"
        for error in exc.errors()
    )
    return render(
        request,
        "error.html",
        {"title": "Invalid request", "status": 422, "message": problems},
        status_code=422,
    )


async def _database_unavailable(request: Request, exc: Exception) -> HTMLResponse:
    log.warning("panel.database_unavailable", path=request.url.path, error=repr(exc))
    return render(
        request,
        "error.html",
        {
            "title": "Database unavailable",
            "status": 503,
            "message": "The database is not reachable. Is it running? Try: make up",
        },
        status_code=503,
    )


async def _schema_missing(request: Request, exc: Exception) -> HTMLResponse:
    assert isinstance(exc, ProgrammingError)
    if getattr(exc.orig, "sqlstate", None) != UNDEFINED_TABLE:
        raise exc  # a real query bug: let it surface as a 500 and in the logs
    return render(
        request,
        "error.html",
        {
            "title": "Database not migrated",
            "status": 503,
            "message": "A table the panel reads does not exist yet. Run: make migrate",
        },
        status_code=503,
    )
