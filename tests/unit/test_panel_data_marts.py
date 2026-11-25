"""Marts pages over a small DuckDB warehouse with the dbt mart tables; no Postgres needed.

`WAREHOUSE_DDL` mirrors the dbt models column for column, and a test parses the models to keep
it, and the columns the panel reads, in step with dbt.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from scrapeline.serve.panel.app import create_app
from scrapeline.serve.panel.pages.marts import MART_COLUMNS, MINUS, product_href
from scrapeline.settings import Settings
from tests.conftest import ROOT

MARTS_DIR = ROOT / "dbt" / "models" / "marts"

WAREHOUSE_DDL: dict[str, list[tuple[str, str]]] = {
    "product_price_history": [
        ("version_id", "BIGINT"),
        ("record_id", "BIGINT"),
        ("source_id", "VARCHAR"),
        ("external_id", "VARCHAR"),
        ("title", "VARCHAR"),
        ("url", "VARCHAR"),
        ("dataset_version", "INTEGER"),
        ("version", "INTEGER"),
        ("change_type", "VARCHAR"),
        ("valid_from", "TIMESTAMPTZ"),
        ("valid_to", "TIMESTAMPTZ"),
        ("is_current", "BOOLEAN"),
        ("is_removed", "BOOLEAN"),
        ("price_amount", "DECIMAL(18,4)"),
        ("price_currency", "VARCHAR"),
        ("list_price_amount", "DECIMAL(18,4)"),
        ("previous_price_amount", "DECIMAL(18,4)"),
        ("previous_price_currency", "VARCHAR"),
        ("price_change", "DECIMAL(19,4)"),
        ("price_change_pct", "DOUBLE"),
        ("price_changed", "BOOLEAN"),
        ("in_stock", "BOOLEAN"),
        ("previous_in_stock", "BOOLEAN"),
        ("stock_changed", "BOOLEAN"),
        ("stock_quantity", "BIGINT"),
        ("previous_stock_quantity", "BIGINT"),
        ("stock_quantity_change", "BIGINT"),
        ("availability", "VARCHAR"),
        ("source_name", "VARCHAR"),
        ("site_id", "BIGINT"),
        ("site_host", "VARCHAR"),
        ("site_category", "VARCHAR"),
        ("run_id", "VARCHAR"),
        ("raw_sha256", "VARCHAR"),
    ],
    "scraper_health_daily": [
        ("source_id", "VARCHAR"),
        ("date_day", "DATE"),
        ("runs", "BIGINT"),
        ("runs_succeeded", "BIGINT"),
        ("runs_failed", "BIGINT"),
        ("success_rate", "DOUBLE"),
        ("pages", "BIGINT"),
        ("records_loaded", "BIGINT"),
        ("records_inserted", "BIGINT"),
        ("records_updated", "BIGINT"),
        ("records_restored", "BIGINT"),
        ("records_removed", "BIGINT"),
        ("rejects", "BIGINT"),
        ("fetches", "BIGINT"),
        ("fetch_errors", "BIGINT"),
        ("blocked_responses", "BIGINT"),
        ("blocked_runs", "BIGINT"),
        ("p95_fetch_ms", "DOUBLE"),
        ("checks_warned", "BIGINT"),
        ("checks_failed", "BIGINT"),
        ("agent_runs", "BIGINT"),
        ("agent_cost_usd", "DOUBLE"),
    ],
}

T0 = datetime(2026, 11, 1, 9, tzinfo=UTC)
LAST_DAY = date(2026, 11, 20)
URL_ID = "https://books.example/catalogue/a-light/index.html?ref=1"
XSS_TITLE = "<script>alert('mart')</script>"


def output_columns(sql: str) -> list[str]:
    """Output column names of a dbt model: the final top-level select, up to its from."""
    text = re.sub(r"--[^\n]*", "", sql)
    text = re.sub(r"\{\{.*?\}\}", "x", text, flags=re.DOTALL)
    final = list(re.finditer(r"(?m)^select\b", text))[-1]
    body = text[final.end() :]
    end = re.search(r"(?m)^from\b", body)
    assert end is not None
    items, depth, current = [], 0, ""
    for char in body[: end.start()]:
        depth += char in "([{"
        depth -= char in ")]}"
        if char == "," and depth == 0:
            items.append(current)
            current = ""
        else:
            current += char
    items.append(current)
    names = []
    for item in (" ".join(i.split()) for i in items):
        alias = re.search(r"\bas\s+(\w+)$", item, flags=re.IGNORECASE)
        names.append(alias.group(1) if alias else item.split(".")[-1])
    return names


def model_columns(name: str) -> list[str]:
    (path,) = MARTS_DIR.glob(f"**/{name}.sql")
    return output_columns(path.read_text())


def documented_columns(name: str) -> list[str]:
    for path in MARTS_DIR.glob("**/*.yml"):
        for model in yaml.safe_load(path.read_text()).get("models", []):
            if model["name"] == name:
                return [column["name"] for column in model.get("columns", [])]
    raise AssertionError(f"{name} is not documented in any marts yml")


@pytest.mark.parametrize("mart", sorted(MART_COLUMNS))
def test_panel_reads_only_columns_the_dbt_model_produces(mart: str) -> None:
    produced = model_columns(mart)
    # The parser is trusted only if it finds every column the model's yml documents.
    assert set(documented_columns(mart)) <= set(produced)
    missing = set(MART_COLUMNS[mart]) - set(produced)
    assert not missing, f"{mart} no longer has {sorted(missing)}: update the panel"


@pytest.mark.parametrize("mart", sorted(WAREHOUSE_DDL))
def test_test_warehouse_matches_the_dbt_model(mart: str) -> None:
    assert [name for name, _ in WAREHOUSE_DDL[mart]] == model_columns(mart)


def _history_row(**values: Any) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(name for name, _ in WAREHOUSE_DDL["product_price_history"])
    row.update(source_id="shop", source_name="Shop", site_category="ecommerce", dataset_version=1)
    row.update(values)
    return row


def _product(
    record_id: int, external_id: str, title: str, steps: list[tuple[str, str | None, bool | None]]
) -> list[dict[str, Any]]:
    """Versions of one product, a day apart: (change_type, price, in_stock)."""
    rows: list[dict[str, Any]] = []
    previous: tuple[str | None, bool | None] | None = None
    for index, (change, price, in_stock) in enumerate(steps):
        removed = change == "removed"
        last = index == len(steps) - 1
        comparable = not removed and previous is not None
        old_price = previous[0] if previous else None
        rows.append(
            _history_row(
                version_id=record_id * 100 + index,
                record_id=record_id,
                external_id=external_id,
                title=title,
                url=f"https://shop.example/{record_id}",
                version=index + 1,
                change_type=change,
                valid_from=T0 + timedelta(days=index),
                valid_to=None if last else T0 + timedelta(days=index + 1),
                is_current=last,
                is_removed=removed,
                price_amount=price,
                price_currency="GBP" if price else None,
                previous_price_amount=old_price,
                previous_price_currency="GBP" if old_price else None,
                price_change=(float(price) - float(old_price))
                if comparable and price and old_price
                else None,
                price_change_pct=(
                    round(100 * (float(price) - float(old_price)) / float(old_price), 2)
                    if comparable and price and old_price
                    else None
                ),
                price_changed=(price != old_price) if comparable else None,
                in_stock=in_stock,
                previous_in_stock=previous[1] if previous else None,
                stock_changed=(in_stock != previous[1]) if comparable and previous else None,
                raw_sha256=None if removed else f"{record_id:02d}{index}".ljust(64, "a"),
                run_id=f"run-{index}",
            )
        )
        if not removed:
            previous = (price, in_stock)
    return rows


HISTORY = [
    *_product(
        1,
        "SIM-0002",
        "Espresso cup set (4)",
        [("insert", "14.99", True), ("update", "16.49", True), ("update", "13.99", True)],
    ),
    *_product(
        2,
        "SIM-0007",
        "Adjustable desk lamp",
        [("insert", "39.99", True), ("removed", None, None), ("restored", "39.99", True)],
    ),
    *_product(
        3, "SIM-0004", "Chef's knife", [("insert", "49.95", True), ("update", "49.95", False)]
    ),
    *_product(
        4, URL_ID, "A Light in the Attic", [("insert", "51.77", True), ("update", "47.00", True)]
    ),
    *_product(5, "XSS-1", XSS_TITLE, [("insert", "1.00", True), ("update", "2.00", True)]),
]


def _health_rows() -> list[dict[str, Any]]:
    rows = []
    for source_id, days_back, runs, succeeded in (
        ("shop", 0, 4, 3),
        ("shop", 1, 2, 2),
        ("books", 0, 1, 1),
        ("shop", 45, 9, 0),  # outside the 30-day window
    ):
        row: dict[str, Any] = dict.fromkeys(
            name for name, _ in WAREHOUSE_DDL["scraper_health_daily"]
        )
        row.update(
            source_id=source_id,
            date_day=LAST_DAY - timedelta(days=days_back),
            runs=runs,
            runs_succeeded=succeeded,
            runs_failed=runs - succeeded,
            success_rate=round(succeeded / runs, 4),
            pages=runs * 2,
            records_loaded=20 * runs,
            records_inserted=1,
            records_updated=8,
            records_restored=0,
            records_removed=1,
            rejects=0,
            fetches=runs * 2,
            fetch_errors=runs - succeeded,
            blocked_responses=0,
            blocked_runs=0,
            p95_fetch_ms=1234.0,
            checks_warned=0,
            checks_failed=0,
            agent_runs=1,
            agent_cost_usd=0.4321,
        )
        rows.append(row)
    return rows


def build_warehouse(path: Path, marts: tuple[str, ...] = tuple(WAREHOUSE_DDL)) -> None:
    with duckdb.connect(str(path)) as con:
        con.execute("SET TimeZone = 'UTC'")
        for mart in marts:
            columns = WAREHOUSE_DDL[mart]
            con.execute(f"CREATE TABLE {mart} ({', '.join(f'{n} {t}' for n, t in columns)})")
            rows = HISTORY if mart == "product_price_history" else _health_rows()
            names = [n for n, _ in columns]
            con.executemany(
                f"INSERT INTO {mart} VALUES ({', '.join('?' for _ in names)})",
                [[row[n] for n in names] for row in rows],
            )


def client_for(warehouse: Path) -> TestClient:
    # Marts pages never touch Postgres; port 1 refuses connections if one tried.
    engine = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    settings = Settings(
        _env_file=None, panel_allowed_hosts=["testserver"], warehouse_path=warehouse
    )
    return TestClient(create_app(engine=engine, settings=settings))


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "warehouse.duckdb"
    build_warehouse(path)
    return path


@pytest.fixture
def client(warehouse: Path) -> TestClient:
    return client_for(warehouse)


def main(html: str) -> str:
    return re.split(r"<main\b[^>]*>", html, maxsplit=1)[1].split("</main>", 1)[0]


def test_marts_index_lists_tables_with_rows_and_columns(client: TestClient) -> None:
    page = client.get("/marts")
    assert page.status_code == 200
    body = main(page.text)
    assert '<a href="/marts/price-history">product_price_history</a>' in body
    assert '<a href="/marts/scraper-health">scraper_health_daily</a>' in body
    assert f'<td class="num">{len(HISTORY)}</td>' in body
    assert "34 columns" in body and "<code>price_change_pct</code>" in body


def test_price_history_lists_changes_and_movers(client: TestClient) -> None:
    page = client.get("/marts/price-history")
    assert page.status_code == 200
    body = main(page.text)
    movers = body.split("<h2>Biggest movers</h2>", 1)[1].split("<h2>", 1)[0]
    changes = body.split("<h2>Latest price changes</h2>", 1)[1].split("<h2>", 1)[0]
    # Movers compare first and latest price: the XSS product doubled, the lamp never moved.
    assert movers.index("&lt;script&gt;") < movers.index("A Light in the Attic")
    assert "Adjustable desk lamp" not in movers and "Chef" not in movers
    assert "14.99 GBP</td>" in movers and "13.99 GBP</td>" in movers and MINUS + "6.7%" in movers
    # Latest changes, newest first, with signed absolute and relative change.
    assert changes.index('16.49 GBP</td>\n      <td class="num">13.99 GBP') < changes.index(
        '14.99 GBP</td>\n      <td class="num">16.49 GBP'
    )
    assert "+1.50" in changes and "+10.0%" in changes and MINUS + "2.50" in changes
    assert f'href="{product_href("shop", URL_ID)}"' in changes


def test_price_history_filters_by_source_and_text(client: TestClient) -> None:
    body = main(client.get("/marts/price-history", params={"q": "espresso"}).text)
    assert "Espresso cup set (4)" in body and "A Light in the Attic" not in body
    body = main(client.get("/marts/price-history", params={"source_id": "other"}).text)
    assert "No price changes match." in body and "No products match." in body
    # Wildcards are literal: "%" matches only a title that contains one.
    body = main(client.get("/marts/price-history", params={"q": "%"}).text)
    assert "No products match." in body


def test_product_page_draws_an_accessible_step_chart(client: TestClient) -> None:
    page = client.get("/marts/price-history/shop/SIM-0002")
    assert page.status_code == 200
    body = main(page.text)
    svg = body.split("<svg", 1)[1].split("</svg>", 1)[0]
    assert 'role="img"' in svg and 'aria-labelledby="price-chart-title price-chart-desc"' in svg
    assert '<title id="price-chart-title">Price of Espresso cup set (4) over time</title>' in svg
    assert "From 14.99 GBP on 2026-11-01 09:00 UTC to 13.99 GBP" in svg
    assert svg.count("<path ") == 1 and svg.count(" V ") == 2
    assert "16.49 GBP</text>" in svg and "13.99 GBP</text>" in svg
    # The strict CSP allows no inline styles or scripts.
    assert "style=" not in body and "<script" not in body and " on" + "load=" not in body
    for price in ("14.99 GBP", "16.49 GBP", "13.99 GBP"):
        assert f'<td class="num">{price}</td>' in body
    assert '<a href="/data/product/1">record lineage</a>' in body
    assert 'href="https://shop.example/1" rel="noopener noreferrer nofollow"' in body


def test_product_page_shows_removals_and_stock_changes(client: TestClient) -> None:
    lamp = main(client.get("/marts/price-history/shop/SIM-0007").text)
    assert "<title>Removed: not found in a complete crawl</title>" in lamp
    assert lamp.count("<path ") == 2  # the line breaks while the lamp was gone
    assert "removed 1 time" in lamp and "still removed" not in lamp
    knife = main(client.get("/marts/price-history/shop/SIM-0004").text)
    stock = knife.split("<h2>Stock changes</h2>", 1)[1].split("<h2>", 1)[0]
    assert "yes → no" in stock


def test_product_ids_that_are_urls_work(client: TestClient) -> None:
    page = client.get(product_href("shop", URL_ID))
    assert page.status_code == 200
    assert "A Light in the Attic" in page.text and "51.77 GBP" in page.text


def test_unknown_product_is_not_found(client: TestClient) -> None:
    page = client.get("/marts/price-history/shop/NOPE")
    assert page.status_code == 404 and "No product" in page.text


def test_mart_values_are_escaped(client: TestClient) -> None:
    for path in ("/marts/price-history", "/marts/price-history/shop/XSS-1"):
        page = client.get(path)
        assert "<script>alert" not in page.text
        assert "&lt;script&gt;alert(&#39;mart&#39;)&lt;/script&gt;" in page.text


def test_scraper_health_covers_the_last_30_days(client: TestClient) -> None:
    page = client.get("/marts/scraper-health")
    assert page.status_code == 200
    body = main(page.text)
    assert "up to 2026-11-20" in body
    days = body.split("<h2>By day</h2>", 1)[1]
    assert days.count("<tr>") == 1 + 3  # header and three days; the 45-day-old row is out
    assert "2026-10-06" not in days
    assert "75.0%" in days and "1.2 s" in days and "$0.43" in days
    totals = body.split("<h2>Totals for the period</h2>", 1)[1].split("<h2>", 1)[0]
    assert '<a href="/sources/shop">shop</a></th>\n      <td class="num">6</td>' in totals
    assert "83.3%" in totals  # 5 of 6 runs


def test_scraper_health_filters_by_source(client: TestClient) -> None:
    body = main(client.get("/marts/scraper-health", params={"source_id": "books"}).text)
    days = body.split("<h2>By day</h2>", 1)[1]
    assert days.count("<tr>") == 2 and "<td>books</td>" in days and "<td>shop</td>" not in days
    assert '<option value="books" selected>' in body


MART_PAGES = (
    "/marts",
    "/marts/price-history",
    "/marts/price-history/shop/SIM-0002",
    "/marts/scraper-health",
)


@pytest.mark.parametrize("path", MART_PAGES)
def test_a_missing_warehouse_says_how_to_build_it(tmp_path: Path, path: str) -> None:
    page = client_for(tmp_path / "none.duckdb").get(path)
    assert page.status_code == 200
    assert "No warehouse yet" in page.text and "make lake &amp;&amp; make dbt" in page.text


@pytest.mark.parametrize("path", MART_PAGES)
def test_a_corrupt_warehouse_is_explained_not_a_crash(tmp_path: Path, path: str) -> None:
    broken = tmp_path / "warehouse.duckdb"
    broken.write_bytes(b"this is not a duckdb file\n" * 500)
    page = client_for(broken).get(path)
    assert page.status_code == 503
    assert "could not be read" in page.text and "make dbt" in page.text


def test_a_warehouse_without_the_marts_asks_for_dbt(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.duckdb"
    build_warehouse(path, marts=())
    client = client_for(path)
    assert "The warehouse has no tables yet" in client.get("/marts").text
    for page_path in MART_PAGES[1:]:
        page = client.get(page_path)
        assert page.status_code == 200
        assert "does not contain this mart yet" in page.text


def test_a_mart_without_an_expected_column_is_explained(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.duckdb"
    build_warehouse(path)
    with duckdb.connect(str(path)) as con:
        con.execute("ALTER TABLE product_price_history DROP COLUMN price_change_pct")
    page = client_for(path).get("/marts/price-history")
    assert page.status_code == 503 and "could not be read" in page.text


@pytest.fixture
def locked(warehouse: Path) -> Iterator[Path]:
    """Another process holds the warehouse open for writing, as dbt does during a build."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys, time\n"
            "con = duckdb.connect(sys.argv[1])\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n",
            str(warehouse),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "ready"
        yield warehouse
    finally:
        holder.kill()
        holder.wait()


def test_a_locked_warehouse_is_explained_not_a_crash(locked: Path) -> None:
    page = client_for(locked).get("/marts/price-history")
    assert page.status_code == 503
    assert "If dbt is building it right now, try again in a moment" in page.text
