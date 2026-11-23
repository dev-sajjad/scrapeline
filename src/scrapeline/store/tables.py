"""SQLAlchemy Core tables, grouped by the migration that creates them.

`alembic check` (run in the integration tests) fails if these and the migrations diverge.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def _ts(name: str, *, nullable: bool = False, default: bool = False) -> Column[object]:
    return Column(
        name,
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default else None,
    )


# Migration 0001: registry, runs, jobs, fetch log

sources = Table(
    "sources",
    metadata,
    Column("id", String(100), primary_key=True),
    Column("name", Text, nullable=False),
    Column("platform", String(50), nullable=False),
    Column("tenant", String(100)),
    Column("jurisdiction_id", String(100)),
    Column("status", String(20), nullable=False),
    Column("status_note", Text),
    Column("config_version", Integer, nullable=False),
    Column("config", JSONB, nullable=False),
    Column("watermark", DateTime(timezone=True)),
    _ts("created_at", default=True),
    _ts("updated_at", default=True),
    Column("site_id", BigInteger, ForeignKey("catalog.site.id")),
    Column("dataset", String(100)),
)

scraper_versions = Table(
    "scraper_versions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_id", String(100), ForeignKey("sources.id"), nullable=False),
    Column("adapter", Text, nullable=False),
    Column("code_hash", String(64), nullable=False),
    Column("author", String(100), nullable=False, server_default="human"),
    _ts("created_at", default=True),
    UniqueConstraint("source_id", "code_hash", name="uq_scraper_versions_source_code"),
)

run = Table(
    "run",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("source_id", String(100), ForeignKey("sources.id"), nullable=False),
    Column("kind", String(20), nullable=False),  # incremental | backfill | reprocess
    Column("status", String(20), nullable=False),  # running | succeeded | failed
    Column("scraper_version_id", Integer, ForeignKey("scraper_versions.id")),
    Column("since", DateTime(timezone=True)),
    Column("until", DateTime(timezone=True)),
    Column("stats", JSONB, nullable=False, server_default="{}"),
    Column("error", Text),
    _ts("started_at", default=True),
    _ts("finished_at", nullable=True),
    schema="ops",
)

job = Table(
    "job",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("kind", String(50), nullable=False),
    Column("source_id", String(100), ForeignKey("sources.id")),
    Column("payload", JSONB, nullable=False, server_default="{}"),
    Column("status", String(20), nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="3"),
    _ts("run_after", default=True),
    _ts("lease_until", nullable=True),
    Column("worker_id", String(100)),
    Column("last_error", Text),
    _ts("created_at", default=True),
    _ts("updated_at", default=True),
    schema="ops",
)

fetch_log = Table(
    "fetch_log",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("ops.run.id")),
    Column("source_id", String(100), nullable=False),
    Column("url", Text, nullable=False),
    Column("final_url", Text),
    Column("status", Integer),
    Column("attempt", Integer, nullable=False, server_default="1"),
    Column("elapsed_ms", Integer),
    Column("nbytes", Integer),
    Column("content_type", Text),
    Column("kind", String(50)),
    Column("raw_sha256", String(64)),
    Column("raw_key", Text),
    Column("meta", JSONB, nullable=False, server_default="{}"),
    Column("error", Text),
    _ts("fetched_at"),
    schema="raw",
)

# Migration 0002: normalised civic data


def _lineage() -> list[Column[object]]:
    """Provenance and identity columns on every normalised row."""
    return [
        Column("source_id", String(100), ForeignKey("sources.id"), nullable=False),
        Column("external_id", Text, nullable=False),
        Column("canonical_url", Text),
        Column("content_hash", String(64), nullable=False),
        Column("schema_version", Integer, nullable=False),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        _ts("fetched_at"),
        Column("run_id", UUID(as_uuid=True), ForeignKey("ops.run.id"), nullable=False),
        Column("scraper_version_id", Integer, ForeignKey("scraper_versions.id")),
        Column("raw_sha256", String(64)),
    ]


jurisdictions = Table(
    "jurisdictions",
    metadata,
    Column("id", String(100), primary_key=True),
    Column("name", Text, nullable=False),
    Column("state", String(10)),
    Column("country", String(2), nullable=False, server_default="US"),
    _ts("created_at", default=True),
)

bodies = Table(
    "bodies",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("jurisdiction_id", String(100), ForeignKey("jurisdictions.id"), nullable=False),
    Column("name", Text, nullable=False),
    Column("body_type", Text),
    Column("active", Integer),
    *_lineage(),
    UniqueConstraint("source_id", "external_id", name="uq_bodies_source_external"),
)

meetings = Table(
    "meetings",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("jurisdiction_id", String(100), ForeignKey("jurisdictions.id"), nullable=False),
    Column("body_external_id", Text),
    Column("body_name", Text),
    Column("title", Text, nullable=False),
    Column("meeting_date", Date, nullable=False),
    Column("start_time", Time),
    Column("location", Text),
    Column("status", String(20), nullable=False),
    Column("agenda_url", Text),
    Column("minutes_url", Text),
    Column("source_modified_at", DateTime(timezone=True)),
    *_lineage(),
    UniqueConstraint("source_id", "external_id", name="uq_meetings_source_external"),
)

agenda_items = Table(
    "agenda_items",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("meeting_external_id", Text, nullable=False),
    Column("sequence", Integer),
    Column("item_number", Text),
    Column("title", Text, nullable=False),
    Column("action", Text),
    Column("matter_type", Text),
    *_lineage(),
    UniqueConstraint("source_id", "external_id", name="uq_agenda_items_source_external"),
)

documents = Table(
    "documents",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("meeting_external_id", Text),
    Column("kind", String(30), nullable=False),  # agenda | minutes | packet | other
    Column("title", Text),
    Column("url", Text, nullable=False),
    Column("doc_sha256", String(64)),
    Column("page_count", Integer),
    Column("text_chars", Integer),
    Column("needs_ocr", Integer),
    Column("text", Text),
    *_lineage(),
    UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
)

rejects = Table(
    "rejects",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("ops.run.id"), nullable=False),
    Column("source_id", String(100), nullable=False),
    Column("record_type", String(50), nullable=False),
    Column("raw_sha256", String(64)),
    Column("record", JSONB, nullable=False),
    Column("reason", Text, nullable=False),
    _ts("created_at", default=True),
    schema="pipeline",
)

# Migration 0003: quality and incidents

check_result = Table(
    "check_result",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("ops.run.id"), nullable=False),
    Column("source_id", String(100), nullable=False),
    Column("check_name", String(100), nullable=False),
    Column("status", String(10), nullable=False),  # pass | warn | fail
    Column("value", Float),
    Column("threshold", Text),
    Column("details", JSONB, nullable=False, server_default="{}"),
    _ts("created_at", default=True),
    schema="ops",
)

incident = Table(
    "incident",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("source_id", String(100), ForeignKey("sources.id"), nullable=False),
    Column("run_id", UUID(as_uuid=True), ForeignKey("ops.run.id")),
    Column("kind", String(50), nullable=False),  # drift | blocked | quality | repair_exhausted
    Column("status", String(10), nullable=False, server_default="open"),  # open | ack | closed
    Column("summary", Text, nullable=False),
    Column("details", JSONB, nullable=False, server_default="{}"),
    _ts("opened_at", default=True),
    _ts("closed_at", nullable=True),
    schema="ops",
)

metric_daily = Table(
    "metric_daily",
    metadata,
    Column("day", Date, primary_key=True),
    Column("source_id", String(100), primary_key=True),
    Column("metric", String(100), primary_key=True),
    Column("value", Float, nullable=False),
    schema="ops",
)

# Migration 0004: agents and evals

agent_run = Table(
    "agent_run",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("role", String(20), nullable=False),  # repair | generate
    Column("source_id", String(100), nullable=False),
    Column("case_id", String(100)),
    Column("eval_run_id", UUID(as_uuid=True)),
    Column("model", String(100), nullable=False),
    Column("prompt_version", String(50), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("iterations", Integer, nullable=False, server_default="0"),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("cache_read_tokens", Integer, nullable=False, server_default="0"),
    Column("cache_write_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=False, server_default="0"),
    Column("outcome", String(30), nullable=False),  # passed | failed | budget_exceeded | error
    Column("tools_called", JSONB, nullable=False, server_default="[]"),
    Column("error", Text),
    _ts("started_at", default=True),
    _ts("finished_at", nullable=True),
    schema="ops",
)

agent_tool_call = Table(
    "agent_tool_call",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("agent_run_id", UUID(as_uuid=True), ForeignKey("ops.agent_run.id"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("tool", String(50), nullable=False),
    Column("args", JSONB, nullable=False, server_default="{}"),
    Column("ok", Integer, nullable=False),
    Column("error", Text),
    Column("duration_ms", Integer, nullable=False, server_default="0"),
    _ts("created_at", default=True),
    schema="ops",
)

eval_run = Table(
    "run",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("suite", String(30), nullable=False),
    Column("driver", String(30), nullable=False),
    Column("model", String(100)),
    Column("prompt_version", String(50)),
    Column("summary", JSONB, nullable=False, server_default="{}"),
    _ts("started_at", default=True),
    _ts("finished_at", nullable=True),
    schema="evals",
)

eval_case_result = Table(
    "case_result",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("eval_run_id", UUID(as_uuid=True), ForeignKey("evals.run.id"), nullable=False),
    Column("case_id", String(100), nullable=False),
    Column("passed", Integer, nullable=False),
    Column("repair_needed", Integer, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("first_attempt_pass", Integer, nullable=False),
    Column("false_success", Integer, nullable=False),
    Column("regression", Integer, nullable=False),
    Column("field_accuracy_before", Float),
    Column("field_accuracy_after", Float),
    Column("tool_calls", Integer, nullable=False, server_default="0"),
    Column("tool_errors", Integer, nullable=False, server_default="0"),
    Column("tokens", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=False, server_default="0"),
    Column("details", JSONB, nullable=False, server_default="{}"),
    _ts("created_at", default=True),
    schema="evals",
)

# Migration 0005: catalog, generic records with history, events

site_category = Table(
    "site_category",
    metadata,
    Column("code", String(30), primary_key=True),
    Column("label", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("taxonomy_version", Integer, nullable=False),
    schema="catalog",
)

site = Table(
    "site",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("host", String(255), nullable=False, unique=True),
    Column("registered_domain", String(255)),
    Column(
        "category",
        String(30),
        ForeignKey("catalog.site_category.code"),
        nullable=False,
        server_default="unclassified",
    ),
    Column("category_confidence", Float),
    Column("category_method", String(20), nullable=False, server_default="none"),
    Column("category_evidence", JSONB, nullable=False, server_default="[]"),
    Column("tags", JSONB, nullable=False, server_default="[]"),
    Column("country", String(2)),
    Column("language", String(35)),
    Column("platform", String(50)),
    _ts("classified_at", nullable=True),
    _ts("created_at", default=True),
    _ts("updated_at", default=True),
    CheckConstraint(
        "category_method IN ('rule', 'evidence', 'llm', 'human', 'none')", name="category_method"
    ),
    CheckConstraint("category_confidence BETWEEN 0 AND 1", name="category_confidence"),
    schema="catalog",
)

classification_log = Table(
    "classification_log",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "site_id",
        BigInteger,
        ForeignKey("catalog.site.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("old_category", String(30), ForeignKey("catalog.site_category.code")),
    Column("new_category", String(30), ForeignKey("catalog.site_category.code"), nullable=False),
    Column("confidence", Float),
    Column("method", String(20), nullable=False),
    Column("evidence", JSONB, nullable=False, server_default="[]"),
    Column("taxonomy_version", Integer, nullable=False),
    Column("actor", String(100), nullable=False),
    _ts("created_at", default=True),
    schema="catalog",
)

dataset_type = Table(
    "dataset_type",
    metadata,
    Column("name", String(100), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("pack", String(50), nullable=False),
    Column("storage", String(10), nullable=False),
    Column("description", Text, nullable=False),
    Column("fields", JSONB, nullable=False),
    Column("key_fields", JSONB, nullable=False),
    Column("definition_hash", String(64), nullable=False),
    _ts("created_at", default=True),
    CheckConstraint("storage IN ('generic', 'typed')", name="storage"),
    schema="catalog",
)

records = Table(
    "records",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("dataset", String(100), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("current_version", Integer, nullable=False, server_default="1"),
    _ts("removed_at", nullable=True),
    *_lineage(),
    UniqueConstraint("source_id", "dataset", "external_id", name="uq_records_identity"),
    Index("ix_records_dataset_last_seen", "dataset", "last_seen_at"),
    schema="data",
)

record_versions = Table(
    "record_versions",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "record_id", BigInteger, ForeignKey("data.records.id", ondelete="CASCADE"), nullable=False
    ),
    Column("version", Integer, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("change_type", String(10), nullable=False),
    Column("payload", JSONB),
    Column("content_hash", String(64)),
    _ts("valid_from"),
    Column("run_id", UUID(as_uuid=True), ForeignKey("ops.run.id"), nullable=False),
    Column("scraper_version_id", Integer, ForeignKey("scraper_versions.id")),
    Column("raw_sha256", String(64)),
    _ts("created_at", default=True),
    UniqueConstraint("record_id", "version", name="uq_record_versions_record_version"),
    CheckConstraint(
        "change_type IN ('insert', 'update', 'removed', 'restored')", name="change_type"
    ),
    CheckConstraint("(payload IS NULL) = (change_type = 'removed')", name="payload_presence"),
    schema="data",
)

event = Table(
    "event",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _ts("at", default=True),
    Column("kind", String(50), nullable=False),
    Column("level", String(10), nullable=False, server_default="info"),
    Column("source_id", String(100)),
    Column("run_id", UUID(as_uuid=True)),
    Column("job_id", BigInteger),
    Column("message", Text, nullable=False, server_default=""),
    Column("data", JSONB, nullable=False, server_default="{}"),
    Index("ix_event_at", "at"),
    Index("ix_event_run_id", "run_id"),
    Index("ix_event_source_id_at", "source_id", "at"),
    CheckConstraint("level IN ('debug', 'info', 'warning', 'error')", name="level"),
    schema="ops",
)
