"""Local lakehouse layout: the contract between the lake exporter and the dbt project.

Parquet files live under `settings.lake_dir` (default `data/lake/`). Append tables are
immutable files partitioned Hive-style (read with `hive_partitioning = true`); each export
writes new files and never rewrites old ones. Snapshot tables are rewritten whole on every
export. Personal-data fields (`FieldDef.pii`) are removed from payloads unless the source's
usage allows them. Export progress per append table is kept in `STATE_PATH`.

Validity intervals (valid_to, is_current) are derived in dbt from `record_versions`, so the
lake stays append-only.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

STATE_PATH = "_state/exports.json"
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
                "run_id:VARCHAR raw_sha256:VARCHAR scraper_version_id:INTEGER"
            ),
            ("dataset", "date"),
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
