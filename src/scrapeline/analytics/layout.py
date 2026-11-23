"""Local lakehouse layout: the contract between the lake exporter and the dbt project.

Parquet files live under `settings.lake_dir` (default `data/lake/`). Append tables are
immutable files partitioned Hive-style (read with `hive_partitioning = true`); each export
writes new files and never rewrites old ones. Snapshot tables are rewritten whole on every
export. A `date` partition is the UTC date of the table's `partition_time` column.

Incremental export selects new rows by each table's `watermark` column, kept per table in
`STATE_PATH`, using one of two rules so that rows committed late are never skipped:

- "cutoff": the watermark column is set by the database at insert (`now()`, the transaction
  start). The export's cutoff is the earliest of now() and the start time of every other open
  transaction; rows with watermark <= cutoff are all committed, so the export reads
  (previous cutoff, cutoff] exactly.
- "overlap": the watermark column is set by the application, so commits can arrive late. The
  export reads rows with watermark > previous watermark - OVERLAP.

Duplicates across exports are possible either way: staging models dedupe on the primary key
(version_id, run_id, id). `dataset_version` comes from record_versions.schema_version.
Validity intervals (valid_to, is_current) are derived in dbt by ordering each record's
versions by `version`, so the lake stays append-only. Personal-data fields are removed from
payloads unless the source's usage allows them.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

STATE_PATH = "_state/exports.json"
OVERLAP = timedelta(hours=1)
LAKE_LAYOUT_VERSION = 1


@dataclass(frozen=True)
class LakeColumn:
    name: str
    type: str  # DuckDB type


@dataclass(frozen=True)
class LakeTable:
    name: str
    mode: Literal["append", "snapshot"]
    path: str  # relative to the lake root; {placeholders} are partition values or export_id
    columns: tuple[LakeColumn, ...]
    partition_by: tuple[str, ...] = ()
    partition_time: str | None = None  # DB column whose UTC date is the `date` partition
    watermark: str | None = None  # DB column that selects new rows for incremental export
    late_commits: Literal["cutoff", "overlap"] | None = None

    @property
    def glob(self) -> str:
        """Glob for readers (DuckDB `read_parquet`) relative to the lake root."""
        if self.mode == "snapshot":
            return self.path
        return f"{self.name}/**/*.parquet"


def _cols(spec: str) -> tuple[LakeColumn, ...]:
    pairs = (item.split(":") for item in spec.split())
    return tuple(LakeColumn(name, type_) for name, type_ in pairs)


TABLES: dict[str, LakeTable] = {
    table.name: table
    for table in (
        LakeTable(
            "record_versions",
            "append",
            "record_versions/dataset={dataset}/date={date}/{export_id}.parquet",
            _cols(
                "version_id:BIGINT record_id:BIGINT source_id:VARCHAR dataset:VARCHAR "
                "dataset_version:INTEGER external_id:VARCHAR version:INTEGER "
                "change_type:VARCHAR valid_from:TIMESTAMPTZ content_hash:VARCHAR payload:JSON "
                "run_id:VARCHAR raw_sha256:VARCHAR scraper_version_id:INTEGER "
                "created_at:TIMESTAMPTZ"
            ),
            ("dataset", "date"),
            partition_time="valid_from",
            watermark="created_at",
            late_commits="cutoff",
        ),
        LakeTable(
            "runs",
            "append",
            "runs/date={date}/{export_id}.parquet",
            _cols(
                "run_id:VARCHAR source_id:VARCHAR kind:VARCHAR status:VARCHAR "
                "started_at:TIMESTAMPTZ finished_at:TIMESTAMPTZ scraper_version_id:INTEGER "
                "stats:JSON error:VARCHAR"
            ),
            ("date",),
            partition_time="finished_at",
            watermark="finished_at",
            late_commits="overlap",
        ),
        LakeTable(
            "check_results",
            "append",
            "check_results/date={date}/{export_id}.parquet",
            _cols(
                "id:BIGINT run_id:VARCHAR source_id:VARCHAR check_name:VARCHAR status:VARCHAR "
                "value:DOUBLE threshold:VARCHAR created_at:TIMESTAMPTZ"
            ),
            ("date",),
            partition_time="created_at",
            watermark="created_at",
            late_commits="cutoff",
        ),
        LakeTable(
            "fetch_log",
            "append",
            "fetch_log/date={date}/{export_id}.parquet",
            _cols(
                "id:BIGINT run_id:VARCHAR source_id:VARCHAR host:VARCHAR status:INTEGER "
                "attempt:INTEGER elapsed_ms:INTEGER nbytes:INTEGER kind:VARCHAR "
                "fetched_at:TIMESTAMPTZ error:VARCHAR"
            ),
            ("date",),
            partition_time="fetched_at",
            watermark="fetched_at",
            late_commits="overlap",
        ),
        LakeTable(
            "agent_runs",
            "append",
            "agent_runs/date={date}/{export_id}.parquet",
            _cols(
                "id:VARCHAR role:VARCHAR source_id:VARCHAR model:VARCHAR prompt_version:VARCHAR "
                "attempt:INTEGER outcome:VARCHAR input_tokens:INTEGER output_tokens:INTEGER "
                "cost_usd:DOUBLE started_at:TIMESTAMPTZ finished_at:TIMESTAMPTZ"
            ),
            ("date",),
            partition_time="started_at",
            watermark="finished_at",
            late_commits="overlap",
        ),
        LakeTable(
            "classification_log",
            "append",
            "classification_log/date={date}/{export_id}.parquet",
            _cols(
                "id:BIGINT site_id:BIGINT old_category:VARCHAR new_category:VARCHAR "
                "confidence:DOUBLE method:VARCHAR taxonomy_version:INTEGER actor:VARCHAR "
                "created_at:TIMESTAMPTZ"
            ),
            ("date",),
            partition_time="created_at",
            watermark="created_at",
            late_commits="cutoff",
        ),
        LakeTable(
            "incidents",
            "snapshot",
            "snapshots/incidents.parquet",
            _cols(
                "id:BIGINT source_id:VARCHAR kind:VARCHAR status:VARCHAR summary:VARCHAR "
                "opened_at:TIMESTAMPTZ closed_at:TIMESTAMPTZ"
            ),
        ),
        LakeTable(
            "sources",
            "snapshot",
            "snapshots/sources.parquet",
            _cols(
                "id:VARCHAR name:VARCHAR platform:VARCHAR dataset:VARCHAR site_id:BIGINT "
                "status:VARCHAR cadence:VARCHAR created_at:TIMESTAMPTZ"
            ),
        ),
        LakeTable(
            "sites",
            "snapshot",
            "snapshots/sites.parquet",
            _cols(
                "id:BIGINT host:VARCHAR registered_domain:VARCHAR category:VARCHAR "
                "category_confidence:DOUBLE category_method:VARCHAR tags:JSON country:VARCHAR "
                "language:VARCHAR platform:VARCHAR classified_at:TIMESTAMPTZ"
            ),
        ),
        LakeTable(
            "site_categories",
            "snapshot",
            "snapshots/site_categories.parquet",
            _cols("code:VARCHAR label:VARCHAR description:VARCHAR taxonomy_version:INTEGER"),
        ),
        LakeTable(
            "dataset_types",
            "snapshot",
            "snapshots/dataset_types.parquet",
            _cols(
                "name:VARCHAR version:INTEGER pack:VARCHAR storage:VARCHAR "
                "description:VARCHAR fields:JSON key_fields:JSON"
            ),
        ),
    )
}


@dataclass
class ExportReport:
    export_id: str
    started_at: datetime
    rows: dict[str, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)


class LakeExporter(Protocol):
    def export(self, *, tables: Collection[str] | None = None) -> ExportReport: ...
