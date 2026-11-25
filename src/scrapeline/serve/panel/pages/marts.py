"""Marts from the dbt warehouse: price history per product and scraper health per day.

The warehouse is a DuckDB file that dbt rebuilds from the lake. The panel opens it read-only
with one short-lived connection per request, so a dbt build is never blocked for long, and a
missing, locked or broken file becomes a page that says what to do instead of an error.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import duckdb
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from scrapeline.serve.panel import filters
from scrapeline.serve.panel.deps import SettingsDep, render
from scrapeline.serve.panel.pages.data import like_pattern

router = APIRouter()

# The mart columns the panel reads. A unit test checks them against the dbt models.
HISTORY_COLUMNS = (
    "version_id",
    "record_id",
    "source_id",
    "external_id",
    "title",
    "url",
    "version",
    "change_type",
    "valid_from",
    "valid_to",
    "is_current",
    "is_removed",
    "price_amount",
    "price_currency",
    "previous_price_amount",
    "previous_price_currency",
    "price_change",
    "price_change_pct",
    "price_changed",
    "in_stock",
    "previous_in_stock",
    "stock_changed",
    "stock_quantity",
    "stock_quantity_change",
    "source_name",
    "raw_sha256",
)
HEALTH_COLUMNS = (
    "source_id",
    "date_day",
    "runs",
    "runs_succeeded",
    "success_rate",
    "records_loaded",
    "records_updated",
    "records_removed",
    "rejects",
    "fetch_errors",
    "blocked_responses",
    "p95_fetch_ms",
    "agent_cost_usd",
)
MART_COLUMNS: dict[str, tuple[str, ...]] = {
    "product_price_history": HISTORY_COLUMNS,
    "scraper_health_daily": HEALTH_COLUMNS,
}

# What each mart is for, and the panel page that shows it.
MARTS: dict[str, tuple[str, str | None]] = {
    "product_price_history": (
        "Every product version: price and stock changes.",
        "/marts/price-history",
    ),
    "product_price_daily": (
        "One row per product per day with that day's price.",
        "/marts/price-history",
    ),
    "scraper_health_daily": (
        "Runs, records, errors and cost per source per day.",
        "/marts/scraper-health",
    ),
    "dim_source": ("Registered sources with their dataset and site category.", None),
    "dim_site": ("Sites (hosts) with their category.", None),
    "dim_dataset": ("Dataset types by their newest version.", None),
    "dim_date": ("Calendar of the days the lake covers.", None),
}

MAX_TABLES = 100
CHANGES_SHOWN = 50
MOVERS_SHOWN = 10
PRODUCTS_SHOWN = 50
MAX_HISTORY = 1000
HEALTH_DAYS = 30
MAX_SOURCE_OPTIONS = 200
MINUS = chr(0x2212)  # a true minus sign, as wide as the plus

# Close to the content width on a laptop, so 13-unit labels render at about 13 px there.
CHART_WIDTH = 960
CHART_HEIGHT = 300
CHART_LEFT, CHART_RIGHT, CHART_TOP, CHART_BOTTOM = 100, 16, 20, 40

# Optional filters as fixed SQL: a NULL parameter switches its condition off.
_PRODUCT_FILTERS = (
    "($source is null or source_id = $source) "
    "and ($q is null or title ilike $q escape '\\' or external_id ilike $q escape '\\')"
)
# Built from the fixed column tuples above; every value is a parameter.
_HISTORY_SELECT = "select " + ", ".join(HISTORY_COLUMNS) + " from product_price_history"  # noqa: S608
_HEALTH_SELECT = "select " + ", ".join(HEALTH_COLUMNS) + " from scraper_health_daily"  # noqa: S608
_HEALTH_WINDOW = (
    "date_day > (select max(date_day) from scraper_health_daily) - $days * interval 1 day "
    "and ($source is null or source_id = $source)"
)

MARTS_SQL = """
select table_name, estimated_size as row_count
from duckdb_tables()
where schema_name = 'main'
order by table_name
limit $limit
"""
COLUMNS_SQL = """
select table_name, column_name, data_type
from information_schema.columns
where table_schema = 'main'
order by table_name, ordinal_position
"""
LATEST_CHANGES_SQL = (
    _HISTORY_SELECT
    + " where price_changed and "
    + _PRODUCT_FILTERS
    + " order by valid_from desc, version_id desc limit $limit"
)
MOVERS_SQL = """
with observed as (
    select record_id, source_id, external_id, title, version, valid_from,
           price_amount, price_currency, price_changed
    from product_price_history
    where not is_removed and price_amount is not null
      and ($source is null or source_id = $source)
      and ($q is null or title ilike $q escape '\\' or external_id ilike $q escape '\\')
),
products as (
    select
        record_id, source_id, external_id,
        arg_max(title, version) as title,
        arg_min(price_amount, version) as first_amount,
        arg_min(price_currency, version) as first_currency,
        arg_max(price_amount, version) as last_amount,
        arg_max(price_currency, version) as last_currency,
        max(valid_from) filter (where price_changed) as last_change
    from observed
    group by record_id, source_id, external_id
)
select
    *,
    last_amount - first_amount as change,
    round(100 * (last_amount - first_amount) / nullif(first_amount, 0), 2) as change_pct
from products
where first_currency = last_currency and first_amount <> last_amount
order by abs(change_pct) desc nulls last, last_change desc
limit $limit
"""
PRODUCTS_SQL = """
select record_id, source_id, external_id, title, price_amount, price_currency,
       in_stock, is_removed, valid_from
from product_price_history
where is_current
  and ($source is null or source_id = $source)
  and ($q is null or title ilike $q escape '\\' or external_id ilike $q escape '\\')
order by title, external_id
limit $limit
"""
PRODUCT_SOURCES_SQL = (
    "select distinct source_id from product_price_history order by source_id limit $limit"
)
# Newest first, so a long history keeps its latest versions; the page puts them back in order.
PRODUCT_SQL = (
    _HISTORY_SELECT
    + " where source_id = $source and external_id = $external_id"
    + " order by version desc limit $limit"
)
HEALTH_LAST_DAY_SQL = "select max(date_day) from scraper_health_daily"
HEALTH_DAYS_SQL = (
    _HEALTH_SELECT + " where " + _HEALTH_WINDOW + " order by date_day desc, source_id limit $limit"
)
HEALTH_TOTALS_SQL = (
    """
select
    source_id,
    sum(runs) as runs,
    sum(runs_succeeded) as runs_succeeded,
    sum(records_loaded) as records_loaded,
    sum(records_updated) as records_updated,
    sum(records_removed) as records_removed,
    sum(rejects) as rejects,
    sum(fetch_errors) as fetch_errors,
    sum(blocked_responses) as blocked_responses,
    max(p95_fetch_ms) as worst_p95_fetch_ms,
    sum(agent_cost_usd) as agent_cost_usd
from scraper_health_daily
where """
    + _HEALTH_WINDOW
    + " group by source_id order by source_id limit $limit"
)
HEALTH_SOURCES_SQL = (
    "select distinct source_id from scraper_health_daily order by source_id limit $limit"
)

SourceFilter = Annotated[str | None, Query(max_length=100)]
TextFilter = Annotated[str | None, Query(max_length=100)]


class WarehouseMissingError(Exception):
    """No warehouse file yet: dbt has not been run."""


@contextmanager
def warehouse(path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-only connection to the warehouse, closed when the block ends."""
    if not path.is_file():
        raise WarehouseMissingError(str(path))
    con = duckdb.connect(str(path), read_only=True)
    try:
        con.execute("SET TimeZone = 'UTC'")
        yield con
    finally:
        con.close()


def fetch_rows(
    con: duckdb.DuckDBPyConnection, sql: str, params: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    result = con.execute(sql, dict(params or {}))
    names = [d[0] for d in result.description]
    return [dict(zip(names, row, strict=True)) for row in result.fetchall()]


def money(amount: Decimal | float | None, currency: str | None) -> str:
    if amount is None:
        return filters.EMPTY
    return filters.money({"amount": amount, "currency": currency})


def signed(value: Decimal | float | None, suffix: str = "", digits: int = 2) -> str:
    """A change with its sign, using a true minus sign for decreases."""
    if value is None:
        return filters.EMPTY
    sign = "+" if value > 0 else MINUS if value < 0 else "±"
    return f"{sign}{abs(value):,.{digits}f}{suffix}"


def product_href(source_id: str, external_id: str) -> str:
    # External ids can be URLs; quote everything so the id stays one path segment.
    return f"/marts/price-history/{quote(source_id, safe='')}/{quote(external_id, safe='')}"


def _unavailable(request: Request, title: str, exc: Exception, path: Path) -> HTMLResponse:
    """The page shown instead of a mart: missing warehouse, missing mart, or unreadable file."""
    if isinstance(exc, WarehouseMissingError):
        kind, status = "missing", 200
    elif isinstance(exc, duckdb.CatalogException):
        kind, status = "incomplete", 200
    else:
        kind, status = "unreadable", 503
    return render(
        request,
        "marts/unavailable.html",
        {"title": title, "kind": kind, "path": path, "detail": str(exc)},
        status_code=status,
    )


@router.get("/marts", response_class=HTMLResponse)
def marts(request: Request, settings: SettingsDep) -> HTMLResponse:
    path = settings.warehouse_path
    try:
        with warehouse(path) as con:
            # dbt rebuilds every mart whole, so the table's estimate is its row count.
            tables = fetch_rows(con, MARTS_SQL, {"limit": MAX_TABLES})
            columns = fetch_rows(con, COLUMNS_SQL)
    except (WarehouseMissingError, duckdb.Error) as exc:
        return _unavailable(request, "Marts", exc, path)
    by_table: dict[str, list[dict[str, Any]]] = {}
    for column in columns:
        by_table.setdefault(column["table_name"], []).append(column)
    stat = path.stat()
    rows = [
        {
            "name": table["table_name"],
            "rows": table["row_count"],
            "columns": by_table.get(table["table_name"], []),
            "description": MARTS.get(table["table_name"], ("", None))[0],
            "page": MARTS.get(table["table_name"], ("", None))[1],
        }
        for table in tables
    ]
    return render(
        request,
        "marts/index.html",
        {
            "title": "Marts",
            "path": path,
            "size": stat.st_size,
            "built": datetime.fromtimestamp(stat.st_mtime, UTC),
            "rows": rows,
        },
    )


def _change_view(row: Mapping[str, Any]) -> dict[str, Any]:
    pct = row.get("price_change_pct")
    return {
        **row,
        "href": product_href(row["source_id"], row["external_id"]),
        "old": money(row.get("previous_price_amount"), row.get("previous_price_currency")),
        "new": money(row.get("price_amount"), row.get("price_currency")),
        "change": signed(row.get("price_change")),
        "change_pct": signed(pct, "%", 1),
    }


@router.get("/marts/price-history", response_class=HTMLResponse)
def price_history(
    request: Request, settings: SettingsDep, source_id: SourceFilter = None, q: TextFilter = None
) -> HTMLResponse:
    source_id, q = source_id or None, (q or "").strip() or None
    path = settings.warehouse_path
    params = {"source": source_id, "q": like_pattern(q) if q else None}
    try:
        with warehouse(path) as con:
            changes = fetch_rows(con, LATEST_CHANGES_SQL, {**params, "limit": CHANGES_SHOWN})
            movers = fetch_rows(con, MOVERS_SQL, {**params, "limit": MOVERS_SHOWN})
            products = fetch_rows(con, PRODUCTS_SQL, {**params, "limit": PRODUCTS_SHOWN + 1})
            sources = fetch_rows(con, PRODUCT_SOURCES_SQL, {"limit": MAX_SOURCE_OPTIONS})
    except (WarehouseMissingError, duckdb.Error) as exc:
        return _unavailable(request, "Price history", exc, path)
    mover_views = [
        {
            **row,
            "href": product_href(row["source_id"], row["external_id"]),
            "old": money(row["first_amount"], row["first_currency"]),
            "new": money(row["last_amount"], row["last_currency"]),
            "change": signed(row["change"]),
            "change_pct": signed(row["change_pct"], "%", 1),
        }
        for row in movers
    ]
    product_views = [
        {
            **row,
            "href": product_href(row["source_id"], row["external_id"]),
            "price": money(row["price_amount"], row["price_currency"]),
        }
        for row in products[:PRODUCTS_SHOWN]
    ]
    return render(
        request,
        "marts/price_history.html",
        {
            "title": "Price history",
            "changes": [_change_view(row) for row in changes],
            "movers": mover_views,
            "products": product_views,
            "more_products": len(products) > PRODUCTS_SHOWN,
            "sources": [row["source_id"] for row in sources],
            "filters": {"source_id": source_id or "", "q": q or ""},
        },
    )


@dataclass(frozen=True)
class ChartPoint:
    x: float
    y: float
    label: str


@dataclass(frozen=True)
class Chart:
    """A step chart laid out in SVG user units; the template only prints it."""

    width: int
    height: int
    paths: list[str]
    points: list[ChartPoint]
    gaps: list[tuple[float, float]]
    y_ticks: list[tuple[float, str]]
    x_ticks: list[tuple[float, str, str]]  # x, label, text-anchor
    plot: tuple[float, float, float, float]  # left, top, right, bottom
    title: str
    description: str


def price_chart(
    history: Sequence[Mapping[str, Any]], title: str, now: datetime | None = None
) -> Chart | None:
    """A step chart of price over time, with gaps where the product was removed.

    `history` is one product's versions in order. The current version runs to `now`, or a
    little past the last change when the history ends later than that.
    """
    priced = [h for h in history if not h["is_removed"] and h["price_amount"] is not None]
    if not priced:
        return None
    start: datetime = history[0]["valid_from"]
    last: datetime = max(v for h in history for v in (h["valid_from"], h["valid_to"]) if v)
    span = (last - start).total_seconds()
    end = max(now or datetime.now(UTC), last + timedelta(seconds=max(span * 0.1, 60)))
    total = (end - start).total_seconds()

    amounts = [float(h["price_amount"]) for h in priced]
    cheapest, dearest = min(amounts), max(amounts)
    pad = (dearest - cheapest) * 0.15 or max(abs(dearest) * 0.1, 1.0)
    low = max(cheapest - pad, 0.0) if cheapest >= 0 else cheapest - pad
    high = dearest + pad

    left, top = float(CHART_LEFT), float(CHART_TOP)
    right, bottom = float(CHART_WIDTH - CHART_RIGHT), float(CHART_HEIGHT - CHART_BOTTOM)

    def x_at(when: datetime) -> float:
        return left + (right - left) * (when - start).total_seconds() / total

    def y_at(amount: float) -> float:
        return bottom - (bottom - top) * (amount - low) / (high - low)

    paths: list[str] = []
    points: list[ChartPoint] = []
    gaps: list[tuple[float, float]] = []
    segment: list[str] = []
    for row in history:
        x1, x2 = x_at(row["valid_from"]), x_at(row["valid_to"] or end)
        if row["is_removed"] or row["price_amount"] is None:
            if segment:
                paths.append(" ".join(segment))
                segment = []
            if row["is_removed"]:
                gaps.append((round(x1, 1), round(x2, 1)))
            continue
        y = y_at(float(row["price_amount"]))
        # Each version is a horizontal step; a change is a vertical jump at its start.
        segment.append(f"V {y:.1f} H {x2:.1f}" if segment else f"M {x1:.1f} {y:.1f} H {x2:.1f}")
        label = f"{money(row['price_amount'], row['price_currency'])} from "
        points.append(ChartPoint(round(x1, 1), round(y, 1), label + filters.dt(row["valid_from"])))
    if segment:
        paths.append(" ".join(segment))

    # Label the real extremes rather than round numbers: they are the prices people look for.
    currency = priced[-1]["price_currency"] or ""
    y_ticks = [
        (round(y_at(v), 1), f"{v:,.2f} {currency}".strip())
        for v in sorted({cheapest, dearest}, reverse=True)
    ]
    x_ticks = [(left, filters.dt(start), "start"), (right, filters.dt(end), "end")]
    changes = sum(1 for h in history if h.get("price_changed"))
    removals = sum(1 for h in history if h["is_removed"])
    first, latest = priced[0], priced[-1]
    description = (
        f"From {money(first['price_amount'], first['price_currency'])} "
        f"on {filters.dt(first['valid_from'])} "
        f"to {money(latest['price_amount'], latest['price_currency'])} "
        f"on {filters.dt(latest['valid_from'])}; "
        f"{changes} price {'change' if changes == 1 else 'changes'}"
    )
    if removals:
        description += f", removed {removals} {'time' if removals == 1 else 'times'}"
    # The axis labels shrink with the chart on a phone; the caption keeps the range legible.
    if dearest != cheapest:
        description += f"; highest {y_ticks[0][1]}, lowest {y_ticks[-1][1]}"
    return Chart(
        width=CHART_WIDTH,
        height=CHART_HEIGHT,
        paths=paths,
        points=points,
        gaps=gaps,
        y_ticks=y_ticks,
        x_ticks=x_ticks,
        plot=(left, top, right, bottom),
        title=f"Price of {title} over time",
        description=description + ".",
    )


@router.get("/marts/price-history/{source_id}/{external_id:path}", response_class=HTMLResponse)
def product_history(
    request: Request, source_id: str, external_id: str, settings: SettingsDep
) -> HTMLResponse:
    path = settings.warehouse_path
    params = {"source": source_id, "external_id": external_id, "limit": MAX_HISTORY}
    try:
        with warehouse(path) as con:
            history = fetch_rows(con, PRODUCT_SQL, params)
    except (WarehouseMissingError, duckdb.Error) as exc:
        return _unavailable(request, "Price history", exc, path)
    if not history:
        raise HTTPException(404, f"No product {external_id!r} from {source_id!r} in the marts.")
    latest = history[0]
    title = latest["title"] or external_id
    views = [_change_view(row) for row in history]
    return render(
        request,
        "marts/product.html",
        {
            "title": f"{title} · Price history",
            "product": latest,
            "product_title": title,
            "chart": price_chart(history[::-1], title),
            "history": views,
            # Versions are numbered from 1 without gaps, so the latest number is the count.
            "total_versions": latest["version"],
            "stock_changes": [row for row in views if row.get("stock_changed")],
        },
    )


@router.get("/marts/scraper-health", response_class=HTMLResponse)
def scraper_health(
    request: Request, settings: SettingsDep, source_id: SourceFilter = None
) -> HTMLResponse:
    source_id = source_id or None
    path = settings.warehouse_path
    window = {"days": HEALTH_DAYS, "source": source_id}
    try:
        with warehouse(path) as con:
            last_day = fetch_rows(con, HEALTH_LAST_DAY_SQL)
            days = fetch_rows(
                con, HEALTH_DAYS_SQL, {**window, "limit": HEALTH_DAYS * MAX_SOURCE_OPTIONS}
            )
            totals = fetch_rows(con, HEALTH_TOTALS_SQL, {**window, "limit": MAX_SOURCE_OPTIONS})
            sources = fetch_rows(con, HEALTH_SOURCES_SQL, {"limit": MAX_SOURCE_OPTIONS})
    except (WarehouseMissingError, duckdb.Error) as exc:
        return _unavailable(request, "Scraper health", exc, path)
    for row in totals:
        row["success_rate"] = row["runs_succeeded"] / row["runs"] if row["runs"] else None
    return render(
        request,
        "marts/scraper_health.html",
        {
            "title": "Scraper health",
            "days": days,
            "totals": totals,
            "last_day": next(iter(last_day[0].values())) if last_day else None,
            "window": HEALTH_DAYS,
            "sources": [row["source_id"] for row in sources],
            "filters": {"source_id": source_id or ""},
        },
    )
