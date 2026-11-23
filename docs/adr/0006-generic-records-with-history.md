# ADR-0006: Generic records with append-only history and a schema registry

Status: accepted · Date: 2026-11-24

## Context
scrapeline started with civic meetings: one hand-written pydantic model and one table per record type. It must now collect any kind of data (products, articles, job postings, listings) and feed analytics, which is mostly about change over time. An upsert that overwrites a row keeps only the latest value; history then exists only as raw bytes, which is costly to query.

## Decision
- **Schema registry** (`catalog/datasets.py`): a dataset type is a versioned data contract in YAML (fields with types, key fields, personal-data flags). Built-in packs live in `catalog/packs/`; user types in `config/datasets/`. Record models are built at runtime from the definition, so validation and rejects work the same as for hand-written models. Redefining a version is refused; a change needs a version bump.
- **Generic storage**: every generic dataset lands in one table, `data.records`, with the validated payload as JSONB plus the standard identity and lineage columns. Identity is (source, dataset, external_id); external_id comes from the parser or from the key fields.
- **History**: `data.record_versions` is append-only. A new content hash adds an `update` version; a record missing from a complete crawl gets a `removed` version; a removed record seen again gets `restored`. Validity intervals are derived downstream, so no version row is ever updated.
- **Typed packs stay**: the civic tables keep working and are registered as `storage: typed` dataset types, so the catalog and analytics see every dataset the same way.
- **Catalog**: `catalog.site` gives every source a site (host) with a category from a small versioned taxonomy; `catalog.classification_log` audits every change.

## Alternatives
- **One table per dataset, created dynamically**: typed columns, but DDL at runtime, migrations per user dataset, and harder lineage. Rejected; typed tables are produced in the analytics layer instead.
- **EAV (one row per field)**: flexible, but slow and awkward for every consumer. Rejected.
- **Type 2 columns (valid_to, is_current) maintained in Postgres**: needs an update per change and makes the history table mutable. Rejected in favour of append-only versions with validity derived in dbt.

## Consequences
Analysts read typed staging models (generated from the registry) rather than JSONB. Removal detection is only correct after complete crawls, so it is gated on crawl completeness. Civic typed tables have no version history for now.
