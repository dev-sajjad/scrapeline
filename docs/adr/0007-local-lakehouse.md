# ADR-0007: Local lakehouse for analytics (Parquet, DuckDB, dbt)

Status: accepted · Date: 2026-11-24

## Context
PostgreSQL is the operational store: many small writes, constraints, a job queue. Analytics scans and aggregates history across all sources. Running both on one Postgres works at small scale but mixes workloads, and the data should stay local (no cloud warehouse).

## Decision
- An exporter writes Parquet files to `data/lake/` following `analytics/layout.py`: append-only tables partitioned Hive-style by date (record versions also by dataset), and small snapshot tables for dimensions.
- Export is incremental per table, with progress in `data/lake/_state/exports.json`. Files are never rewritten, so a failed export can be retried safely.
- dbt with the DuckDB adapter reads the Parquet files and builds staging models (typed columns generated from the schema registry), dimensions and marts. dbt tests and docs run in CI.
- Personal-data fields are removed during export unless the source's usage allows them.

## Alternatives
- **dbt on Postgres**: no export step, but analytics competes with the pipeline for the same database. Kept as a fallback.
- **Iceberg or Delta tables**: the right next step for multiple writers or cloud storage; unnecessary for one local writer.
- **A cloud warehouse**: contradicts the local-only requirement.

## Consequences
Analytics is reproducible from files alone and can be moved to object storage later without changing dbt models. Freshness is bounded by the export schedule.
