from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text

from scrapeline.pipeline.runner import Runner
from scrapeline.registry.config import SourceConfig
from scrapeline.settings import Settings
from scrapeline.store.raw import FilesystemRawStore
from tests.conftest import ROOT, SNAPSHOTS, public_resolver

ADMIN_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://scrapeline:scrapeline@localhost:5432/scrapeline"
)
# Parallel workers (one per agent worktree) each set their own database name.
TEST_DB = os.environ.get("SCRAPELINE_TEST_DB", "scrapeline_test")


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    admin.dispose()
    url = ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    eng.alembic_config = cfg  # type: ignore[attr-defined]
    yield eng
    eng.dispose()


def truncate_all(engine: Engine) -> None:
    """Empty every table the tests write to; the seeded site categories stay."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE data.record_versions, data.records, ops.event, ops.check_result, "
                "ops.incident, pipeline.rejects, raw.fetch_log, agenda_items, documents, "
                "meetings, bodies, ops.job, ops.run, scraper_versions, sources, jurisdictions, "
                "catalog.classification_log, catalog.site, catalog.dataset_type "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def clean_db(engine: Engine) -> Engine:
    truncate_all(engine)
    return engine


class FakeLegistar:
    """Serves real Seattle snapshots through httpx.MockTransport. Mutable per test."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = json.loads(
            (SNAPSHOTS / "legistar/seattle_bodies.json").read_text()
        )
        self.events: list[dict[str, Any]] = json.loads(
            (SNAPSHOTS / "legistar/seattle_events_page.json").read_text()
        )
        self.requests: list[httpx.Request] = []
        self.override: Callable[[httpx.Request], httpx.Response | None] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override and (resp := self.override(request)) is not None:
            return resp
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if path.endswith("/bodies"):
            return httpx.Response(200, json=self.bodies)
        if path.endswith("/events"):
            return httpx.Response(200, json=self.events)
        return httpx.Response(404)


@pytest.fixture
def fake_legistar() -> FakeLegistar:
    return FakeLegistar()


def legistar_test_source() -> SourceConfig:
    return SourceConfig.model_validate(
        {
            "id": "legistar-testcity",
            "name": "Test City",
            "platform": "legistar",
            "tenant": "testcity",
            "kind": "api",
            "entry": "https://webapi.legistar.com/v1/testcity",
            "jurisdiction": {"id": "us-wa-testcity", "name": "Test City", "state": "WA"},
            "allowed_hosts": ["webapi.legistar.com"],
            "legal": {"terms_checked": "2026-11-17", "notes": "test"},
            "adapter": "scrapeline.adapters.platforms.legistar:LegistarAdapter",
            "politeness": {"rps": 5},
        }
    )


@pytest.fixture
def test_source() -> SourceConfig:
    return legistar_test_source()


@pytest.fixture
def runner(
    clean_db: Engine, tmp_path: Path, fake_legistar: FakeLegistar, test_source: SourceConfig
) -> Runner:
    return Runner(
        Settings(_env_file=None, raw_store_dir=tmp_path / "raw"),
        clean_db,
        FilesystemRawStore(tmp_path / "raw"),
        transport=httpx.MockTransport(fake_legistar.handler),
        resolver=public_resolver,
        sleep=lambda s: None,
        sources={test_source.id: test_source},
    )
