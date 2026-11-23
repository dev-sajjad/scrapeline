"""Build the dbt project on the sample lake, the way CI does, and check the marts' values.

Needs the analytics extra (dbt-duckdb) but no compose services.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from tests.conftest import ROOT

pytestmark = pytest.mark.integration
pytest.importorskip("dbt.adapters.duckdb", reason="needs the analytics extra")

AS_OF = "2026-11-06"


def dbt(workdir: Path, lake: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "LAKE_DIR": str(lake),
        "WAREHOUSE_PATH": str(workdir / "warehouse.duckdb"),
        "DBT_TARGET_PATH": str(workdir / "target"),
        "DBT_LOG_PATH": str(workdir / "logs"),
    }
    command = [
        str(Path(sys.executable).with_name("dbt")),
        *args,
        "--project-dir",
        "dbt",
        "--profiles-dir",
        "dbt",
    ]
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    return result


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Path]]:
    workdir = tmp_path_factory.mktemp("dbt")
    lake = workdir / "lake"
    subprocess.run([sys.executable, "scripts/make_sample_lake.py", str(lake)], cwd=ROOT, check=True)
    dbt(workdir, lake, "build", "--vars", json.dumps({"as_of_date": AS_OF}))
    yield workdir, lake


@pytest.fixture
def warehouse(built: tuple[Path, Path]) -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect(str(built[0] / "warehouse.duckdb"), read_only=True)
    con.execute("set TimeZone = 'UTC'")
    yield con
    con.close()


def rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    result = con.execute(sql)
    names = [d[0] for d in result.description]
    return [dict(zip(names, row, strict=True)) for row in result.fetchall()]


def history(con: duckdb.DuckDBPyConnection, title: str) -> list[dict[str, Any]]:
    return rows(
        con,
        f"select * from product_price_history where title = '{title}' order by version",
    )


def test_price_changes_are_measured_against_the_previous_version(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    kettle = history(warehouse, "Electric kettle")
    assert [v["price_amount"] for v in kettle] == [Decimal("20"), Decimal("18"), Decimal("19.8")]
    assert [v["previous_price_amount"] for v in kettle] == [None, Decimal("20"), Decimal("18")]
    assert [v["price_change"] for v in kettle] == [None, Decimal("-2"), Decimal("1.8")]
    assert [v["price_change_pct"] for v in kettle] == [None, -10.0, 10.0]
    assert [v["is_current"] for v in kettle] == [False, False, True]
    assert kettle[0]["valid_to"] == kettle[1]["valid_from"]
    assert kettle[2]["valid_to"] is None
    assert {v["site_category"] for v in kettle} == {"ecommerce"}
    assert {v["source_name"] for v in kettle} == {"Simulated shop"}

    attic = history(warehouse, "A Light in the Attic")
    assert attic[1]["price_change"] == Decimal("-4.77")
    assert attic[1]["price_change_pct"] == -9.21


def test_stock_changes_are_tracked_separately_from_price(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    toaster = history(warehouse, "Two-slice toaster")
    assert [v["in_stock"] for v in toaster] == [True, False, True]
    assert [v["stock_changed"] for v in toaster] == [None, True, True]
    assert [v["price_changed"] for v in toaster] == [None, False, True]
    assert [v["stock_quantity_change"] for v in toaster] == [None, -5, 4]
    assert toaster[2]["price_change_pct"] == -14.29


def test_a_restore_is_compared_with_the_state_before_the_removal(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    blender = history(warehouse, "Glass blender")
    assert [v["change_type"] for v in blender] == ["insert", "removed", "restored"]
    removed, restored = blender[1], blender[2]
    assert removed["is_removed"] and removed["price_amount"] is None
    assert removed["valid_from"] == datetime(2026, 11, 3, 6, tzinfo=UTC)
    assert removed["valid_to"] == restored["valid_from"]
    assert restored["previous_price_amount"] == Decimal("50")
    assert restored["price_change"] == Decimal("-5")
    assert restored["price_change_pct"] == -10.0
    assert [v["is_current"] for v in blender] == [False, False, True]


def test_daily_prices_cover_the_days_each_product_existed(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    daily = rows(
        warehouse,
        "select title, list(date_day order by date_day) as days, "
        "list(price_amount order by date_day) as prices "
        "from product_price_daily group by title",
    )
    by_title = {r["title"]: r for r in daily}
    # Removed during day 3 (so it existed that day), back on day 5.
    assert by_title["Glass blender"]["days"] == [date(2026, 11, d) for d in (1, 2, 3, 5, 6)]
    assert by_title["Glass blender"]["prices"] == [Decimal(p) for p in (50, 50, 50, 45, 45)]
    assert by_title["Electric kettle"]["prices"] == [
        Decimal(p) for p in ("20", "20", "18", "18", "19.8", "19.8")
    ]
    assert by_title["Stand mixer"]["days"][0] == date(2026, 11, 2)
    assert by_title["Stand mixer"]["days"][-1] == date.fromisoformat(AS_OF)


def test_scraper_health_counts_runs_fetches_and_costs(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    health = {
        (r["source_id"], r["date_day"].day): r
        for r in rows(warehouse, "select * from scraper_health_daily")
    }
    blocked_day = health[("shop-sim", 4)]
    assert blocked_day["runs"] == 2
    assert blocked_day["success_rate"] == 0.5
    # Three 429 attempts; the run they blocked is not counted again.
    assert blocked_day["blocked_responses"] == 3
    assert blocked_day["blocked_runs"] == 1
    assert blocked_day["fetch_errors"] == 3
    assert blocked_day["p95_fetch_ms"] == 230.0  # attempts of 40, 45, 50, 150 and 250 ms
    assert blocked_day["records_updated"] == 1

    assert health[("shop-sim", 3)]["records_removed"] == 1
    assert health[("shop-sim", 5)]["records_restored"] == 1

    failed_checks = health[("books-toscrape", 2)]
    assert failed_checks["success_rate"] == 0.0
    assert failed_checks["rejects"] == 1
    assert failed_checks["fetch_errors"] == 1
    assert failed_checks["checks_failed"] == 1
    assert failed_checks["agent_runs"] == 2
    assert failed_checks["agent_cost_usd"] == 0.6

    quiet_day = health[("books-toscrape", 3)]
    assert quiet_day["runs"] == 0 and quiet_day["success_rate"] is None

    assert health[("legistar-seattle", 3)]["records_loaded"] == 15
    assert max(day for _, day in health) == date.fromisoformat(AS_OF).day


def test_a_challenge_page_served_with_200_counts_as_blocked(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    (challenge,) = rows(
        warehouse,
        "select * from scraper_health_daily "
        "where source_id = 'books-toscrape' and date_day = date '2026-11-05'",
    )
    assert challenge["fetch_errors"] == 0  # the fetch log only saw a 200
    assert challenge["blocked_runs"] == 1
    assert challenge["blocked_responses"] == 1


def test_scraper_health_covers_registered_sources_only(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    sources = {r[0] for r in warehouse.execute("select source_id from dim_source").fetchall()}
    health = {
        r[0] for r in warehouse.execute("select source_id from scraper_health_daily").fetchall()
    }
    assert health == sources
    eval_runs = warehouse.execute(
        "select count(*), sum(cost_usd) from stg_lake__agent_runs "
        "where source_id = 'gen-g01-static-table'"
    ).fetchone()
    assert eval_runs == (1, 0.9)
    total = warehouse.execute("select sum(agent_cost_usd) from scraper_health_daily").fetchone()
    assert total is not None and round(total[0], 6) == 0.6


def test_scraper_health_counters_are_bigint(warehouse: duckdb.DuckDBPyConnection) -> None:
    types = dict(
        (name, type_)
        for name, type_, *_ in warehouse.execute("describe scraper_health_daily").fetchall()
    )
    counters = [
        "runs",
        "pages",
        "records_loaded",
        "records_inserted",
        "records_removed",
        "rejects",
        "fetches",
        "blocked_responses",
        "blocked_runs",
        "agent_runs",
    ]
    assert {name: types[name] for name in counters} == dict.fromkeys(counters, "BIGINT")


def test_staging_removes_rows_repeated_across_exports(
    built: tuple[Path, Path], warehouse: duckdb.DuckDBPyConnection
) -> None:
    lake = built[1]
    raw_runs = warehouse.execute(
        f"select count(*), count(distinct run_id) from read_parquet('{lake}/runs/**/*.parquet')"
    ).fetchone()
    assert raw_runs is not None and raw_runs[0] == raw_runs[1] + 1
    assert warehouse.execute("select count(*) from stg_lake__runs").fetchone() == (raw_runs[1],)


def test_dimensions(warehouse: duckdb.DuckDBPyConnection) -> None:
    sources = {r["source_id"]: r for r in rows(warehouse, "select * from dim_source")}
    assert sources["legistar-seattle"]["site_category"] == "government"
    assert sources["shop-sim"]["dataset"] == "product"
    datasets = {r["dataset"]: r for r in rows(warehouse, "select * from dim_dataset")}
    assert datasets["product"]["storage"] == "generic"
    assert datasets["meeting"]["storage"] == "typed"
    days = warehouse.execute("select min(date_day), max(date_day) from dim_date").fetchone()
    assert days == (date(2026, 11, 1), date.fromisoformat(AS_OF))


def test_docs_generate(built: tuple[Path, Path]) -> None:
    workdir, lake = built
    dbt(workdir, lake, "docs", "generate", "--vars", json.dumps({"as_of_date": AS_OF}))
    catalog = json.loads((workdir / "target" / "catalog.json").read_text())
    assert "model.scrapeline.product_price_history" in catalog["nodes"]


def test_an_empty_lake_builds_empty_marts(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    lake.mkdir()
    dbt(tmp_path, lake, "build")
    con = duckdb.connect(str(tmp_path / "warehouse.duckdb"), read_only=True)
    try:
        for mart in ("product_price_history", "product_price_daily", "scraper_health_daily"):
            assert con.execute(f"select count(*) from {mart}").fetchone() == (0,)
    finally:
        con.close()
