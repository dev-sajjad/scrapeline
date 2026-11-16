"""civic data and rejects

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS pipeline")
    op.create_table(
        "jurisdictions",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=True),
        sa.Column("country", sa.String(length=2), server_default="US", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jurisdictions")),
    )
    op.create_table(
        "bodies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("jurisdiction_id", sa.String(length=100), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("body_type", sa.Text(), nullable=True),
        sa.Column("active", sa.Integer(), nullable=True),
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("scraper_version_id", sa.Integer(), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["jurisdiction_id"],
            ["jurisdictions.id"],
            name=op.f("fk_bodies_jurisdiction_id_jurisdictions"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["ops.run.id"], name=op.f("fk_bodies_run_id_run")),
        sa.ForeignKeyConstraint(
            ["scraper_version_id"],
            ["scraper_versions.id"],
            name=op.f("fk_bodies_scraper_version_id_scraper_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_bodies_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bodies")),
        sa.UniqueConstraint("source_id", "external_id", name="uq_bodies_source_external"),
    )
    op.create_table(
        "meetings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("jurisdiction_id", sa.String(length=100), nullable=False),
        sa.Column("body_external_id", sa.Text(), nullable=True),
        sa.Column("body_name", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("meeting_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("agenda_url", sa.Text(), nullable=True),
        sa.Column("minutes_url", sa.Text(), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("scraper_version_id", sa.Integer(), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["jurisdiction_id"],
            ["jurisdictions.id"],
            name=op.f("fk_meetings_jurisdiction_id_jurisdictions"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["ops.run.id"], name=op.f("fk_meetings_run_id_run")),
        sa.ForeignKeyConstraint(
            ["scraper_version_id"],
            ["scraper_versions.id"],
            name=op.f("fk_meetings_scraper_version_id_scraper_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_meetings_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meetings")),
        sa.UniqueConstraint("source_id", "external_id", name="uq_meetings_source_external"),
    )
    op.create_table(
        "agenda_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("meeting_external_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("item_number", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("matter_type", sa.Text(), nullable=True),
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("scraper_version_id", sa.Integer(), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["ops.run.id"], name=op.f("fk_agenda_items_run_id_run")
        ),
        sa.ForeignKeyConstraint(
            ["scraper_version_id"],
            ["scraper_versions.id"],
            name=op.f("fk_agenda_items_scraper_version_id_scraper_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_agenda_items_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agenda_items")),
        sa.UniqueConstraint("source_id", "external_id", name="uq_agenda_items_source_external"),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("meeting_external_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("doc_sha256", sa.String(length=64), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("text_chars", sa.Integer(), nullable=True),
        sa.Column("needs_ocr", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("scraper_version_id", sa.Integer(), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["ops.run.id"], name=op.f("fk_documents_run_id_run")),
        sa.ForeignKeyConstraint(
            ["scraper_version_id"],
            ["scraper_versions.id"],
            name=op.f("fk_documents_scraper_version_id_scraper_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_documents_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
    )
    op.create_table(
        "rejects",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("record_type", sa.String(length=50), nullable=False),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.Column("record", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["ops.run.id"], name=op.f("fk_rejects_run_id_run")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rejects")),
        schema="pipeline",
    )


def downgrade() -> None:
    op.drop_table("rejects", schema="pipeline")
    op.drop_table("documents")
    op.drop_table("agenda_items")
    op.drop_table("meetings")
    op.drop_table("bodies")
    op.drop_table("jurisdictions")
    op.execute("DROP SCHEMA IF EXISTS pipeline")
