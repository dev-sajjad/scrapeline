# scrapeline dbt project

Analytics over the Parquet lake (ADR-0007) with DuckDB. The lake layout is defined in
`src/scrapeline/analytics/layout.py`; this project reads it directly, so no database server
is involved.

## Run

From the repository root (relative paths resolve from where dbt runs):

```bash
uv run --extra analytics dbt build --project-dir dbt --profiles-dir dbt
uv run --extra analytics dbt docs generate --project-dir dbt --profiles-dir dbt
```

| Variable | Default | Meaning |
|---|---|---|
| `LAKE_DIR` | `data/lake` | Lake root, the same setting the exporter uses |
| `WAREHOUSE_PATH` | `data/warehouse.duckdb` | DuckDB file dbt builds into; its directory must exist |

The daily marts run to the `as_of_date` var (`--vars '{as_of_date: 2026-11-06}'`), or to
today in UTC. All dates are UTC days.

Without an exported lake, build against the sample lake:

```bash
uv run --extra analytics python scripts/make_sample_lake.py /tmp/lake
LAKE_DIR=/tmp/lake uv run --extra analytics dbt build --project-dir dbt --profiles-dir dbt
```

## Layers

- **Sources** (`models/staging/lake/_lake__sources.yml`): one per lake table, generated from
  the layout. An append table the exporter has not written yet reads as empty. Every export
  writes all the snapshots (`snapshots/*.parquet`), so a missing snapshot fails the build:
  `LAKE_DIR` is wrong, or dbt runs from a directory the relative default does not resolve
  from. Staging views keep the path as given, so use an absolute `LAKE_DIR` to query them
  from elsewhere.
- **Staging**: `stg_lake__<table>` casts columns to the layout types and drops rows repeated
  across exports (keyed on `version_id`, `run_id`, `id`, or `code`).
  `stg_<dataset>__versions` gives each generic dataset type typed columns: money as
  `<field>_amount` (DECIMAL(18, 4)) and `<field>_currency`, dates and datetimes cast, enums
  as text.
- **Intermediate**: `int_record_versions` turns the append-only history into SCD type 2
  intervals: `valid_to` is the next version's `valid_from` (exclusive), plus `is_current`
  and `is_removed`.
- **Marts**: `dim_source`, `dim_site`, `dim_dataset`, `dim_date`; `product_price_history`
  (one row per product version with price and stock changes), `product_price_daily` (one row
  per product per day it existed, with the last price known that day) and
  `scraper_health_daily` (runs, success rate, record counts, rejects, fetch errors, blocked
  responses and runs, p95 fetch latency and agent cost per registered source per day; agent
  runs of generation eval cases are not source health and are left out).

## New dataset types

The sources file and the dataset staging models are generated. After adding or changing a
dataset type, or changing the lake layout, regenerate and commit them:

```bash
uv run python scripts/gen_dbt_staging.py
```

`--check` exits 1 when the committed files are stale; a unit test runs it.
