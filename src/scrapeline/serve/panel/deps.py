"""What every panel page shares: the engine, settings, paging and template rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine

from scrapeline import __version__
from scrapeline.serve.panel.filters import FILTERS
from scrapeline.settings import Settings

PANEL_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PANEL_DIR / "templates"
STATIC_DIR = PANEL_DIR / "static"

# Top navigation, in display order: (path, label).
NAV: tuple[tuple[str, str], ...] = (
    ("/", "Health"),
    ("/sources", "Sources"),
    ("/sites", "Sites"),
    ("/runs", "Runs"),
    ("/incidents", "Incidents"),
    ("/data", "Data"),
    ("/storage", "Storage"),
    ("/marts", "Marts"),
    ("/live", "Live"),
)


def nav_active(path: str, href: str) -> bool:
    return path == href if href == "/" else path == href or path.startswith(href + "/")


templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.filters.update(FILTERS)
templates.env.globals.update(version=__version__, nav=NAV, nav_active=nav_active)


def current_engine(request: Request) -> Engine:
    return cast(Engine, request.app.state.engine)


def current_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


EngineDep = Annotated[Engine, Depends(current_engine)]
SettingsDep = Annotated[Settings, Depends(current_settings)]


@dataclass(frozen=True)
class Page:
    """Offset paging for list pages."""

    limit: int
    offset: int

    def has_next(self, total: int) -> bool:
        return self.offset + self.limit < total

    @property
    def previous_offset(self) -> int | None:
        return max(self.offset - self.limit, 0) if self.offset > 0 else None


def page_params(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page:
    return Page(limit, offset)


PageDep = Annotated[Page, Depends(page_params)]


def is_htmx(request: Request) -> bool:
    """True for requests made by htmx, which expect a fragment rather than a full page."""
    return request.headers.get("HX-Request") == "true"


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)
