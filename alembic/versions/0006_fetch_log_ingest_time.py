"""fetch log ingest time (lets the lake export use the exact cutoff rule)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24 02:08:02.988555
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fetch_log",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema="raw",
    )


def downgrade() -> None:
    op.drop_column("fetch_log", "created_at", schema="raw")
