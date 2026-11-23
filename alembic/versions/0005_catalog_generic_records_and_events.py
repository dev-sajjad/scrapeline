"""catalog, generic records with history, events

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-24 00:29:10.546596
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Taxonomy v1, frozen with this migration. A later taxonomy version gets its own migration.
SITE_CATEGORIES_V1 = [
    ("government", "Government", "Public authorities at any level, courts, agencies"),
    ("education", "Education", "Schools, universities, academic and research bodies"),
    ("ecommerce", "E-commerce", "Online shops and marketplaces"),
    ("news_media", "News and media", "News outlets, magazines, broadcasters"),
    ("personal", "Personal", "Personal sites, blogs and portfolios"),
    ("business", "Business", "Company and organisation sites that do not sell online"),
    ("nonprofit", "Non-profit", "Charities, NGOs, associations, foundations"),
    ("jobs", "Jobs", "Job boards and career sites"),
    ("real_estate", "Real estate", "Property listings and agents"),
    ("health", "Health", "Healthcare providers and health information"),
    ("finance", "Finance", "Banks, insurers, financial data and services"),
    ("forum_social", "Forums and social", "Forums, communities, social platforms"),
    ("reference", "Reference", "Encyclopaedias, documentation, open data portals"),
    ("travel", "Travel", "Travel, transport, hospitality and events"),
    ("other", "Other", "Classified, but fits no other category"),
    ("unclassified", "Unclassified", "Not yet classified or below confidence threshold"),
]


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS catalog")
    op.execute("CREATE SCHEMA IF NOT EXISTS data")
    op.create_table(
        "dataset_type",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("pack", sa.String(length=50), nullable=False),
        sa.Column("storage", sa.String(length=10), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("key_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("storage IN ('generic', 'typed')", name=op.f("ck_dataset_type_storage")),
        sa.PrimaryKeyConstraint("name", "version", name=op.f("pk_dataset_type")),
        schema="catalog",
    )
    op.create_table(
        "site_category",
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_site_category")),
        schema="catalog",
    )
    op.bulk_insert(
        sa.table(
            "site_category",
            sa.column("code", sa.String),
            sa.column("label", sa.Text),
            sa.column("description", sa.Text),
            sa.column("taxonomy_version", sa.Integer),
            schema="catalog",
        ),
        [
            {"code": c, "label": label, "description": d, "taxonomy_version": 1}
            for c, label, d in SITE_CATEGORIES_V1
        ],
    )
    op.create_table(
        "event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("level", sa.String(length=10), server_default="info", nullable=False),
        sa.Column("source_id", sa.String(length=100), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.CheckConstraint(
            "level IN ('debug', 'info', 'warning', 'error')", name=op.f("ck_event_level")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event")),
        schema="ops",
    )
    op.create_index("ix_event_at", "event", ["at"], unique=False, schema="ops")
    op.create_index("ix_event_run_id", "event", ["run_id"], unique=False, schema="ops")
    op.create_index(
        "ix_event_source_id_at", "event", ["source_id", "at"], unique=False, schema="ops"
    )
    op.create_table(
        "site",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("registered_domain", sa.String(length=255), nullable=True),
        sa.Column("category", sa.String(length=30), server_default="unclassified", nullable=False),
        sa.Column("category_confidence", sa.Float(), nullable=True),
        sa.Column("category_method", sa.String(length=20), server_default="none", nullable=False),
        sa.Column(
            "category_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "tags", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("language", sa.String(length=35), nullable=True),
        sa.Column("platform", sa.String(length=50), nullable=True),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category_method IN ('rule', 'evidence', 'llm', 'human', 'none')",
            name=op.f("ck_site_category_method"),
        ),
        sa.CheckConstraint(
            "category_confidence BETWEEN 0 AND 1", name=op.f("ck_site_category_confidence")
        ),
        sa.ForeignKeyConstraint(
            ["category"],
            ["catalog.site_category.code"],
            name=op.f("fk_site_category_site_category"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site")),
        sa.UniqueConstraint("host", name=op.f("uq_site_host")),
        schema="catalog",
    )
    op.create_table(
        "classification_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("site_id", sa.BigInteger(), nullable=False),
        sa.Column("old_category", sa.String(length=30), nullable=True),
        sa.Column("new_category", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column(
            "evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("taxonomy_version", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["new_category"],
            ["catalog.site_category.code"],
            name=op.f("fk_classification_log_new_category_site_category"),
        ),
        sa.ForeignKeyConstraint(
            ["old_category"],
            ["catalog.site_category.code"],
            name=op.f("fk_classification_log_old_category_site_category"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["catalog.site.id"],
            name=op.f("fk_classification_log_site_id_site"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_classification_log")),
        schema="catalog",
    )
    op.create_index(
        op.f("ix_catalog_classification_log_site_id"),
        "classification_log",
        ["site_id"],
        unique=False,
        schema="catalog",
    )
    op.create_table(
        "records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dataset", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("current_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["run_id"], ["ops.run.id"], name=op.f("fk_records_run_id_run")),
        sa.ForeignKeyConstraint(
            ["scraper_version_id"],
            ["scraper_versions.id"],
            name=op.f("fk_records_scraper_version_id_scraper_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_records_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_records")),
        sa.UniqueConstraint("source_id", "dataset", "external_id", name="uq_records_identity"),
        schema="data",
    )
    op.create_index(
        "ix_records_dataset_last_seen",
        "records",
        ["dataset", "last_seen_at"],
        unique=False,
        schema="data",
    )
    op.create_table(
        "record_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("record_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(length=10), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("scraper_version_id", sa.Integer(), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(payload IS NULL) = (change_type = 'removed')",
            name=op.f("ck_record_versions_payload_presence"),
        ),
        sa.CheckConstraint(
            "change_type IN ('insert', 'update', 'removed', 'restored')",
            name=op.f("ck_record_versions_change_type"),
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["data.records.id"],
            name=op.f("fk_record_versions_record_id_records"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["ops.run.id"], name=op.f("fk_record_versions_run_id_run")
        ),
        sa.ForeignKeyConstraint(
            ["scraper_version_id"],
            ["scraper_versions.id"],
            name=op.f("fk_record_versions_scraper_version_id_scraper_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_record_versions")),
        sa.UniqueConstraint("record_id", "version", name="uq_record_versions_record_version"),
        schema="data",
    )
    op.add_column("sources", sa.Column("site_id", sa.BigInteger(), nullable=True))
    op.add_column("sources", sa.Column("dataset", sa.String(length=100), nullable=True))
    op.create_foreign_key(
        op.f("fk_sources_site_id_site"),
        "sources",
        "site",
        ["site_id"],
        ["id"],
        referent_schema="catalog",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_sources_site_id_site"), "sources", type_="foreignkey")
    op.drop_column("sources", "dataset")
    op.drop_column("sources", "site_id")
    op.drop_table("record_versions", schema="data")
    op.drop_index("ix_records_dataset_last_seen", table_name="records", schema="data")
    op.drop_table("records", schema="data")
    op.drop_index(
        op.f("ix_catalog_classification_log_site_id"),
        table_name="classification_log",
        schema="catalog",
    )
    op.drop_table("classification_log", schema="catalog")
    op.drop_table("site", schema="catalog")
    op.drop_index("ix_event_source_id_at", table_name="event", schema="ops")
    op.drop_index("ix_event_run_id", table_name="event", schema="ops")
    op.drop_index("ix_event_at", table_name="event", schema="ops")
    op.drop_table("event", schema="ops")
    op.drop_table("site_category", schema="catalog")
    op.drop_table("dataset_type", schema="catalog")
    op.execute("DROP SCHEMA IF EXISTS data")
    op.execute("DROP SCHEMA IF EXISTS catalog")
