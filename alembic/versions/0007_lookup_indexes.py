"""indexes for lookups by run, by source and by raw file

The panel and lineage queries find a source's latest runs, everything one run wrote,
and every row that came from one raw file. Without these they scan whole tables.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-25 11:40:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = [
    ("ix_run_source_id_started_at", "run", ["source_id", "started_at"], "ops"),
    ("ix_run_started_at", "run", ["started_at"], "ops"),
    ("ix_fetch_log_run_id", "fetch_log", ["run_id"], "raw"),
    ("ix_fetch_log_raw_sha256", "fetch_log", ["raw_sha256"], "raw"),
    ("ix_rejects_run_id", "rejects", ["run_id"], "pipeline"),
    ("ix_check_result_run_id", "check_result", ["run_id"], "ops"),
    ("ix_records_raw_sha256", "records", ["raw_sha256"], "data"),
    ("ix_record_versions_raw_sha256", "record_versions", ["raw_sha256"], "data"),
]


def upgrade() -> None:
    for name, table, columns, schema in INDEXES:
        op.create_index(name, table, columns, schema=schema)


def downgrade() -> None:
    for name, table, _, schema in reversed(INDEXES):
        op.drop_index(name, table_name=table, schema=schema)
