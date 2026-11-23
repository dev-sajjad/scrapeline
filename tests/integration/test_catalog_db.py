from __future__ import annotations

import json
import select as io_select

import psycopg
import pytest
from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.exc import IntegrityError

from scrapeline.catalog.datasets import DatasetRegistry, DatasetType, FieldDef, default_registry
from scrapeline.catalog.repository import ensure_site, sync_dataset_types
from scrapeline.catalog.sites import SITE_CATEGORIES
from scrapeline.errors import DatasetError, StatusTransitionError
from scrapeline.ops import events
from scrapeline.ops.events import EVENTS_CHANNEL, Event, PostgresEventBus
from scrapeline.ops.runs import set_status, sync_source
from scrapeline.registry.config import SourceConfig
from scrapeline.store import tables as t

pytestmark = pytest.mark.integration


def test_site_categories_are_seeded(clean_db: Engine) -> None:
    with clean_db.connect() as conn:
        codes = set(conn.execute(select(t.site_category.c.code)).scalars())
    assert codes == {c.code for c in SITE_CATEGORIES}


def test_ensure_site_is_idempotent_and_unclassified(clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        first = ensure_site(conn, "https://www.Books.toscrape.com/catalogue/")
        second = ensure_site(conn, "books.toscrape.com")
        row = conn.execute(select(t.site)).one()
    assert first == second
    assert (row.host, row.category, row.category_method) == (
        "books.toscrape.com",
        "unclassified",
        "none",
    )


def test_dataset_types_sync_once_and_refuse_silent_changes(clean_db: Engine) -> None:
    registry = default_registry()
    with clean_db.begin() as conn:
        assert sync_dataset_types(conn, registry) == len(list(registry))
        assert sync_dataset_types(conn, registry) == 0
    changed = DatasetRegistry(
        [
            DatasetType(
                name="product",
                description="changed",
                fields={"url": FieldDef(type="url", key=True)},
            )
        ]
    )
    with pytest.raises(DatasetError, match="bump its version"), clean_db.begin() as conn:
        sync_dataset_types(conn, changed)


def test_sync_source_links_site_and_dataset(clean_db: Engine) -> None:
    cfg = SourceConfig.model_validate(
        {
            "id": "books",
            "name": "Books",
            "platform": "custom",
            "kind": "html",
            "entry": "https://books.toscrape.com/",
            "dataset": "product",
            "legal": {"terms_checked": "2026-11-24", "notes": "Practice site."},
            "adapter": "scrapeline.adapters.declarative:DeclarativeAdapter",
        }
    )
    with clean_db.begin() as conn:
        sync_source(conn, cfg)
        row = conn.execute(select(t.sources)).one()
        host = conn.execute(select(t.site.c.host).where(t.site.c.id == row.site_id)).scalar_one()
    assert (row.dataset, row.jurisdiction_id, host) == ("product", None, "books.toscrape.com")


def test_a_failed_event_never_breaks_the_callers_transaction(
    clean_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_insert(conn: Connection, event: Event) -> int:
        conn.execute(text("SELECT 1 / 0"))
        return 0

    monkeypatch.setattr(events, "_insert_and_notify", broken_insert)
    with clean_db.begin() as conn:
        assert events.publish_in(conn, Event(kind="run.started")) is None
        ensure_site(conn, "example.org")  # the transaction is still usable
    with clean_db.connect() as conn:
        assert conn.execute(select(t.site.c.host)).scalar_one() == "example.org"


def test_history_rows_obey_their_constraints(clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        site_id = ensure_site(conn, "example.org")
    with pytest.raises(IntegrityError), clean_db.begin() as conn:
        conn.execute(
            t.classification_log.insert().values(
                site_id=site_id,
                new_category="shopping",
                method="rule",
                taxonomy_version=1,
                actor="test",
            )
        )
    with pytest.raises(IntegrityError), clean_db.begin() as conn:
        conn.execute(t.site.update().values(category_method="guess"))


def test_published_events_are_stored_and_notified(clean_db: Engine) -> None:
    url = clean_db.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(url, autocommit=True) as listener:
        listener.execute(f"LISTEN {EVENTS_CHANNEL}")
        PostgresEventBus(clean_db).publish(
            Event(kind="run.started", source_id="books", data={"pages": 3})
        )
        ready, _, _ = io_select.select([listener.fileno()], [], [], 5)
        assert ready, "no notification within 5s"
        note = next(listener.notifies(timeout=1))
    payload = json.loads(note.payload)
    with clean_db.connect() as conn:
        row = conn.execute(select(t.event).where(t.event.c.id == payload["id"])).one()
    assert payload["kind"] == row.kind == "run.started"
    assert row.data == {"pages": 3} and row.source_id == "books"


def test_a_source_with_unreviewed_terms_cannot_be_activated(clean_db: Engine) -> None:
    base = {
        "name": "Books",
        "platform": "custom",
        "kind": "html",
        "entry": "https://books.toscrape.com/",
        "dataset": "product",
        "adapter": "scrapeline.adapters.declarative:DeclarativeAdapter",
    }
    unreviewed = {"terms_checked": "1970-01-01", "notes": "Needs review."}
    reviewed = {"terms_checked": "2026-11-24", "notes": "Practice site; robots allows."}
    with clean_db.begin() as conn:
        sync_source(
            conn, SourceConfig.model_validate({**base, "id": "draft-a", "legal": unreviewed})
        )
        sync_source(conn, SourceConfig.model_validate({**base, "id": "draft-b", "legal": reviewed}))
    with pytest.raises(StatusTransitionError, match="terms"), clean_db.begin() as conn:
        set_status(conn, "draft-a", "active")
    with clean_db.begin() as conn:
        set_status(conn, "draft-b", "active")
