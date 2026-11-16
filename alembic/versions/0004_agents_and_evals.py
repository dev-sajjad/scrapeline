"""agents and evals

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17 01:11:31.652300
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS evals")
    op.create_table(
        "run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("suite", sa.String(length=30), nullable=False),
        sa.Column("driver", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.String(length=50), nullable=True),
        sa.Column(
            "summary", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run")),
        schema="evals",
    )
    op.create_table(
        "agent_run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("case_id", sa.String(length=100), nullable=True),
        sa.Column("eval_run_id", sa.UUID(), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("iterations", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Float(), server_default="0", nullable=False),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column(
            "tools_called",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_run")),
        schema="ops",
    )
    op.create_table(
        "case_result",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("eval_run_id", sa.UUID(), nullable=False),
        sa.Column("case_id", sa.String(length=100), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("repair_needed", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("first_attempt_pass", sa.Integer(), nullable=False),
        sa.Column("false_success", sa.Integer(), nullable=False),
        sa.Column("regression", sa.Integer(), nullable=False),
        sa.Column("field_accuracy_before", sa.Float(), nullable=True),
        sa.Column("field_accuracy_after", sa.Float(), nullable=True),
        sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Float(), server_default="0", nullable=False),
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
            ["eval_run_id"], ["evals.run.id"], name=op.f("fk_case_result_eval_run_id_run")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_result")),
        schema="evals",
    )
    op.create_table(
        "agent_tool_call",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(length=50), nullable=False),
        sa.Column(
            "args", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("ok", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["ops.agent_run.id"],
            name=op.f("fk_agent_tool_call_agent_run_id_agent_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_tool_call")),
        schema="ops",
    )


def downgrade() -> None:
    op.drop_table("agent_tool_call", schema="ops")
    op.drop_table("case_result", schema="evals")
    op.drop_table("agent_run", schema="ops")
    op.drop_table("run", schema="evals")
    op.execute("DROP SCHEMA IF EXISTS evals")
