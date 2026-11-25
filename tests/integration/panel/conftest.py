"""Panel test data: a database seeded once per test module and read by many page tests.

The seed is real pipeline output: a Legistar run (typed meetings), three shop-sim days
(generic products with updates, a removal and a restore), a failed run, an open incident,
a human site classification and a queued job. Page tests only read it. A test that writes
(publishes events, changes rows) uses `clean_db` in a module of its own, because
`clean_db` empties the seeded tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from scrapeline.catalog.classify import override_category
from scrapeline.ops.queue import enqueue
from scrapeline.pipeline.runner import Runner, RunSummary
from scrapeline.quality.incidents import open_incident
from scrapeline.registry.config import SourceConfig, load_source_file
from scrapeline.serve.panel.app import create_app
from scrapeline.settings import Settings
from scrapeline.store.raw import FilesystemRawStore
from tests.conftest import ROOT, public_resolver
from tests.integration.conftest import FakeLegistar, legistar_test_source, truncate_all

SITES = ROOT / "tests" / "sites"
SHOP_SIM_CONFIG = ROOT / "config" / "sources" / "shop-sim.yaml"
SHOP_SIM_BASE = "http://shop-sim.test"
SHOP_SIM_HOST = "shop-sim.test"


@dataclass(frozen=True)
class Seeded:
    engine: Engine
    settings: Settings
    legistar_run: uuid.UUID
    shop_runs: tuple[uuid.UUID, uuid.UUID, uuid.UUID]  # days 1, 2, 3
    failed_run: uuid.UUID  # shop-sim day 4, which does not exist
    incident_id: int
    job_id: int


def panel_settings(tmp: Path) -> Settings:
    return Settings(
        _env_file=None,
        raw_store_dir=tmp / "raw",
        lake_dir=tmp / "lake",
        warehouse_path=tmp / "warehouse.duckdb",
        panel_allowed_hosts=["testserver"],
    )


class FixtureWeb:
    """Answers for both seeded sources through httpx.MockTransport."""

    def __init__(self) -> None:
        self.legistar = FakeLegistar()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "webapi.legistar.com":
            return self.legistar.handler(request)
        path = request.url.path
        file = (SITES / path.lstrip("/")).resolve()
        if path.endswith("/"):
            file = file / "index.html"
        if path.startswith("/shop-sim/") and file.is_relative_to(SITES) and file.is_file():
            return httpx.Response(
                200, content=file.read_bytes(), headers={"content-type": "text/html; charset=utf-8"}
            )
        return httpx.Response(404)


def seed(engine: Engine, settings: Settings) -> Seeded:
    truncate_all(engine)
    web = FixtureWeb()
    raw_store = FilesystemRawStore(settings.raw_store_dir)

    def run(source: SourceConfig) -> RunSummary:
        runner = Runner(
            settings,
            engine,
            raw_store,
            transport=httpx.MockTransport(web.handler),
            resolver=public_resolver,
            sleep=lambda s: None,
            sources={source.id: source},
        )
        return runner.run(source.id)

    def shop_day(day: int) -> SourceConfig:
        env = {"FIXTURE_BASE_URL": SHOP_SIM_BASE, "SHOP_SIM_DAY": str(day)}
        return load_source_file(SHOP_SIM_CONFIG, env)

    legistar = run(legistar_test_source())
    days = [run(shop_day(day)) for day in (1, 2, 3)]
    failed = run(shop_day(4))
    assert legistar.status == "succeeded", legistar.error
    assert all(day.status == "succeeded" for day in days), [day.error for day in days]
    assert failed.status == "failed"

    with engine.begin() as conn:
        incident_id = open_incident(
            conn,
            source_id="shop-sim",
            kind="quality",
            summary="Price missing on 2 of 20 products",
            run_id=days[2].run_id,
            details={"check": "required_fields"},
        )
        override_category(conn, SHOP_SIM_HOST, "ecommerce", "test", "Simulated shop for tests")
    job_id = enqueue(engine, "run", "legistar-testcity")
    assert job_id is not None
    return Seeded(
        engine=engine,
        settings=settings,
        legistar_run=legistar.run_id,
        shop_runs=(days[0].run_id, days[1].run_id, days[2].run_id),
        failed_run=failed.run_id,
        incident_id=incident_id,
        job_id=job_id,
    )


@pytest.fixture(scope="module")
def seeded(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Seeded:
    return seed(engine, panel_settings(tmp_path_factory.mktemp("panel")))


@pytest.fixture(scope="module")
def client(seeded: Seeded) -> Iterator[TestClient]:
    """A panel client over the seeded database. Lifespan is not started."""
    yield TestClient(create_app(engine=seeded.engine, settings=seeded.settings))
