"""quality and incidents

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "check_result",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("check_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Text(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["ops.run.id"], name=op.f("fk_check_result_run_id_run")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_check_result")),
        schema="ops",
    )
    op.create_table(
        "incident",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="open", nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["ops.run.id"], name=op.f("fk_incident_run_id_run")),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_incident_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incident")),
        schema="ops",
    )
    op.create_table(
        "metric_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("metric", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("day", "source_id", "metric", name=op.f("pk_metric_daily")),
        schema="ops",
    )


def downgrade() -> None:
    op.drop_table("metric_daily", schema="ops")
    op.drop_table("incident", schema="ops")
    op.drop_table("check_result", schema="ops")
