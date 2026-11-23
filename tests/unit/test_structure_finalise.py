"""The finalise stage's pure parts: duplicate counting, incident kinds, run events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from scrapeline.catalog.datasets import resolve_model
from scrapeline.errors import (
    BlockedError,
    CircuitOpenError,
    DatasetError,
    EmptyResultError,
    FetchError,
    HTTPStatusError,
    ParseError,
    RobotsDisallowedError,
    URLPolicyError,
)
from scrapeline.ops.events import Event
from scrapeline.pipeline.finalise import (
    RunSummary,
    duplicates_within_raw,
    incident_kind,
    publish,
    run_outcome,
)
from scrapeline.pipeline.load import Provenance

AT = datetime(2026, 11, 24, tzinfo=UTC)


def test_duplicates_count_only_repeats_inside_one_raw_document() -> None:
    product = resolve_model("product")
    meeting = resolve_model("meeting")
    one = product.model_validate({"url": "https://s.example/p/1", "title": "One"})
    two = product.model_validate({"url": "https://s.example/p/2", "title": "Two"})
    page_a, page_b = Provenance("a" * 64, AT), Provenance("b" * 64, AT)
    # The same identity on two pages is overlap, not a duplicate.
    assert duplicates_within_raw([(one, page_a), (two, page_a), (one, page_b)], "product") == 0
    assert duplicates_within_raw([(one, page_a), (one, page_a), (one, page_a)], "product") == 2
    other = meeting.model_validate(
        {
            "external_id": "m-1",
            "canonical_url": "https://s.example/m/1",
            "title": "Council",
            "meeting_date": "2026-11-24",
        }
    )
    assert duplicates_within_raw([(other, page_a), (other, page_a)], "product") == 0
    assert duplicates_within_raw([], "product") == 0


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (BlockedError("https://s.example/", "challenge", 403), "blocked"),
        (RobotsDisallowedError("robots"), "blocked"),
        (CircuitOpenError("open"), "blocked"),
        (ParseError("selector"), "drift"),
        (EmptyResultError("no rows"), "drift"),
        (HTTPStatusError("https://s.example/x", 404), "fetch_error"),
        (URLPolicyError("http://10.0.0.1/", "private address"), "fetch_error"),
        (FetchError("timeout"), "fetch_error"),
        (DatasetError("unknown type"), "error"),
        (IntegrityError("INSERT", {}, Exception("duplicate key")), "error"),
        (RuntimeError("bug"), "error"),
    ],
)
def test_incident_kind_follows_the_failure(exc: Exception, kind: str) -> None:
    assert incident_kind(exc) == kind


def summary() -> RunSummary:
    return RunSummary(
        run_id=uuid.UUID(int=3),
        source_id="s",
        kind="incremental",
        status="succeeded",
        pages=2,
        loaded={"product": {"inserted": 1}},
        checks={"record_count": "pass"},
    )


def test_run_outcome_leaves_out_what_the_event_or_other_events_carry() -> None:
    outcome = run_outcome(summary())
    carried_elsewhere = {"run_id", "source_id", "loaded", "checks"}
    assert outcome.keys() == summary().as_dict().keys() - carried_elsewhere
    assert outcome["status"] == "succeeded" and outcome["pages"] == 2


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


class BrokenBus:
    def publish(self, event: Event) -> None:
        raise RuntimeError("event store down")


def test_publish_tags_the_event_with_the_run() -> None:
    bus = RecordingBus()
    publish(bus, summary(), "run.finished", "run succeeded", {"pages": 2})
    [event] = bus.events
    assert (event.kind, event.level, event.message) == ("run.finished", "info", "run succeeded")
    assert (event.run_id, event.source_id, event.data) == (uuid.UUID(int=3), "s", {"pages": 2})


def test_publish_never_raises() -> None:
    publish(BrokenBus(), summary(), "run.failed", "boom", {}, level="error")
    bus = RecordingBus()
    publish(bus, summary(), "Not A Kind", "invalid events are dropped, not raised", {})
    assert bus.events == []
