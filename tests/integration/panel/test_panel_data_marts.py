"""Marts pages on a warehouse that dbt builds from the seeded pipeline output.

The whole chain runs: seeded Postgres -> lake export -> dbt -> panel. Needs the analytics
extra (dbt-duckdb).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scrapeline.analytics.export import ParquetLakeExporter
from scrapeline.catalog.datasets import default_registry
from scrapeline.serve.panel.app import create_app
from scrapeline.serve.panel.pages.marts import MINUS
from tests.conftest import ROOT
from tests.integration.panel.conftest import Seeded

pytestmark = pytest.mark.integration
pytest.importorskip("dbt.adapters.duckdb", reason="needs the analytics extra")


def main(html: str) -> str:
    return re.split(r"<main\b[^>]*>", html, maxsplit=1)[1].split("</main>", 1)[0]


@pytest.fixture(scope="module")
def built(seeded: Seeded, tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    """Exports the seed to a lake (files only) and builds the marts with dbt."""
    work = tmp_path_factory.mktemp("marts")
    settings = seeded.settings.model_copy(
        update={"lake_dir": work / "lake", "warehouse_path": work / "warehouse.duckdb"}
    )
    ParquetLakeExporter(seeded.engine, settings.lake_dir, default_registry()).export()
    env = {
        **os.environ,
        "LAKE_DIR": str(settings.lake_dir),
        "WAREHOUSE_PATH": str(settings.warehouse_path),
        "DBT_TARGET_PATH": str(work / "target"),
        "DBT_LOG_PATH": str(work / "logs"),
    }
    as_of = datetime.now(UTC).date().isoformat()
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("dbt")),
            "run",
            "--project-dir",
            "dbt",
            "--profiles-dir",
            "dbt",
            "--vars",
            json.dumps({"as_of_date": as_of}),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    return TestClient(create_app(engine=seeded.engine, settings=settings))


def test_marts_index_lists_what_dbt_built(built: TestClient) -> None:
    body = main(built.get("/marts").text)
    assert '<a href="/marts/price-history">product_price_history</a>' in body
    history = body.split("product_price_history</a>", 1)[1].split("</tr>", 1)[0]
    assert '<td class="num">32</td>' in history  # one row per product version
    assert '<a href="/marts/scraper-health">scraper_health_daily</a>' in body
    assert "dim_source" in body and "dim_date" in body


def test_espresso_cups_price_history_from_the_real_marts(built: TestClient) -> None:
    page = built.get("/marts/price-history/shop-sim/SIM-0002")
    assert page.status_code == 200
    body = main(page.text)
    assert "<h1>Espresso cup set (4)</h1>" in body
    versions = body.split("<h2>Versions</h2>", 1)[1]
    prices = [versions.index(f'<td class="num">{p} GBP</td>') for p in ("13.99", "16.49", "14.99")]
    assert prices == sorted(prices)  # newest first: 14.99 -> 16.49 -> 13.99
    assert "+1.50" in versions and MINUS + "2.50" in versions
    svg = body.split("<svg", 1)[1].split("</svg>", 1)[0]
    assert svg.count("<circle") == 6  # a visible point and a hover target per version
    assert "From 14.99 GBP" in svg and "to 13.99 GBP" in svg


def test_stock_changes_from_the_real_marts(built: TestClient) -> None:
    body = main(built.get("/marts/price-history/shop-sim/SIM-0004").text)
    stock = body.split("<h2>Stock changes</h2>", 1)[1].split("<h2>", 1)[0]
    assert "yes → no" in stock


def test_desk_lamp_shows_its_removal_from_the_real_marts(built: TestClient) -> None:
    body = main(built.get("/marts/price-history/shop-sim/SIM-0007").text)
    assert "removed 1 time" in body
    versions = body.split("<h2>Versions</h2>", 1)[1]
    assert versions.count("badge-ok") == 2 and versions.count("badge-error") == 1


def test_price_history_overview_from_the_real_marts(built: TestClient) -> None:
    body = main(built.get("/marts/price-history").text)
    changes = body.split("<h2>Latest price changes</h2>", 1)[1].split("<h2>", 1)[0]
    # Of the nine updates, six changed the price; the knife, pen and candle changed stock.
    assert changes.count('<th scope="row"><a href="/marts/price-history/shop-sim/') == 6
    assert changes.count("Espresso cup set (4)") == 2
    assert "Chef" not in changes and "Fountain pen" not in changes
    products = body.split("<h2>Products</h2>", 1)[1]
    assert products.count("<tr>") == 1 + 21


def test_scraper_health_from_the_real_marts(built: TestClient) -> None:
    body = main(built.get("/marts/scraper-health", params={"source_id": "shop-sim"}).text)
    days = body.split("<h2>By day</h2>", 1)[1]
    row = days.split("<td>shop-sim</td>", 1)[1].split("</tr>", 1)[0]
    # Four shop-sim runs today; the day-4 run failed.
    assert '<td class="num">4</td>' in row and "75.0%" in row
    assert "legistar-testcity" not in days


def test_storage_shows_the_built_warehouse(built: TestClient) -> None:
    warehouse = main(built.get("/storage").text).split('<h2 id="warehouse">', 1)[1]
    assert "warehouse.duckdb" in warehouse and '<a href="/marts">Browse the marts</a>' in warehouse
