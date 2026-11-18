from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scrapeline.adapters.base import Window, load_adapter
from scrapeline.adapters.platforms.legistar import LegistarAdapter
from scrapeline.adapters.rawdoc import RawDoc
from scrapeline.errors import ConfigError, ParseError, StatusTransitionError
from scrapeline.pipeline.models import Body, Document, Meeting
from scrapeline.quality.checks import run_checks, worst
from scrapeline.registry import check_transition, load_sources
from scrapeline.registry.config import QualityConfig, SourceConfig

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def seattle() -> SourceConfig:
    return load_sources(ROOT / "config" / "sources")["legistar-seattle"]


def raw(content: bytes, kind: str, meta: dict | None = None) -> RawDoc:
    return RawDoc(
        source_id="legistar-seattle",
        url="https://webapi.legistar.com/v1/seattle/events",
        final_url="https://webapi.legistar.com/v1/seattle/events",
        status=200,
        content_type="application/json",
        content=content,
        sha256=RawDoc.hash_bytes(content),
        fetched_at=datetime(2026, 11, 17, tzinfo=UTC),
        kind=kind,
        meta=meta or {},
    )


def test_registry_loads_real_sources_and_builds_allowlist(seattle: SourceConfig) -> None:
    assert seattle.tenant == "seattle"
    assert "webapi.legistar.com" in seattle.hosts
    assert isinstance(load_adapter(seattle), LegistarAdapter)


def test_status_machine() -> None:
    check_transition("draft", "active")
    check_transition("active", "broken")
    with pytest.raises(StatusTransitionError):
        check_transition("retired", "active")


def test_registry_rejects_undefined_variables(tmp_path: Path) -> None:
    (tmp_path / "x.yaml").write_text("id: x\nentry: ${NOPE_NOT_SET}/a\n")
    with pytest.raises(ConfigError, match="undefined variable"):
        load_sources(tmp_path)


def test_discover_incremental_uses_last_modified(seattle: SourceConfig) -> None:
    adapter = LegistarAdapter(seattle)
    targets = list(adapter.discover(Window(since=datetime(2026, 10, 1, tzinfo=UTC))))
    assert [t.kind for t in targets] == ["bodies", "events"]
    q = parse_qs(urlsplit(targets[1].url).query)
    assert q["$filter"] == ["EventLastModifiedUtc ge datetime'2026-10-01T00:00:00'"]
    assert q["$top"] == ["1000"] and q["$skip"] == ["0"]


def test_discover_backfill_uses_event_date_range(seattle: SourceConfig) -> None:
    w = Window(
        since=datetime(2025, 1, 1, tzinfo=UTC),
        until=datetime(2025, 2, 1, tzinfo=UTC),
        backfill=True,
    )
    events = list(LegistarAdapter(seattle).discover(w))[1]
    assert (
        "EventDate ge datetime'2025-01-01T00:00:00' and EventDate lt datetime'2025-02-01T00:00:00'"
        in parse_qs(urlsplit(events.url).query)["$filter"][0]
    )


def test_parse_real_events_snapshot(seattle: SourceConfig, snapshots: Path) -> None:
    content = (snapshots / "legistar" / "seattle_events_page.json").read_bytes()
    result = LegistarAdapter(seattle).parse(raw(content, "events", {"filter": "f", "skip": 0}))
    meetings = [r for r in result.records if isinstance(r, Meeting)]
    docs = [r for r in result.records if isinstance(r, Document)]
    assert len(meetings) == 25 and not result.rejects
    assert all(
        m.canonical_url and m.canonical_url.startswith("https://seattle.legistar.com/")
        for m in meetings
    )
    assert {m.status for m in meetings} <= {"scheduled", "cancelled"}
    assert all("\r" not in (m.location or "") for m in meetings)
    assert docs and all(d.meeting_external_id for d in docs)
    assert result.watermark is not None
    assert result.next_targets == []  # 25 < page size


def test_parse_emits_next_page_when_full(seattle: SourceConfig) -> None:
    adapter = LegistarAdapter(seattle)
    adapter.page_size = 2
    events = json.loads(
        (Path(__file__).parents[1] / "snapshots/legistar/seattle_events_page.json").read_text()
    )[:2]
    result = adapter.parse(raw(json.dumps(events).encode(), "events", {"filter": "f", "skip": 0}))
    assert len(result.next_targets) == 1
    assert result.next_targets[0].meta == {"filter": "f", "skip": 2}


def test_parse_bodies_never_reads_contact_fields(seattle: SourceConfig) -> None:
    body = [
        {
            "BodyId": 1,
            "BodyName": " City  Council ",
            "BodyTypeName": "Council",
            "BodyActiveFlag": 1,
            "BodyContactEmail": "someone@example.gov",
            "BodyContactPhone": "555",
        }
    ]
    result = LegistarAdapter(seattle).parse(raw(json.dumps(body).encode(), "bodies"))
    (b,) = result.records
    assert isinstance(b, Body) and b.name == "City Council" and b.active is True
    assert "someone@example.gov" not in b.model_dump_json()


def test_parse_non_json_is_typed_error(seattle: SourceConfig) -> None:
    with pytest.raises(ParseError):
        LegistarAdapter(seattle).parse(raw(b"<html>maintenance</html>", "events"))


def test_quality_checks_zero_rows_fail_unless_incremental_window() -> None:
    cfg = QualityConfig()
    assert worst(run_checks([], [], cfg)) == "fail"
    assert worst(run_checks([], [], cfg, zero_is_ok=True)) == "warn"


def test_quality_checks_flag_nulls_and_duplicates() -> None:
    from datetime import date

    ms = [
        Meeting(external_id=str(i), title="Council", meeting_date=date(2026, 1, 1))
        for i in range(4)
    ]
    res = {c.name: c for c in run_checks(ms, [], QualityConfig(), duplicates_within_raw=1)}
    assert res["duplicate_rate"].status == "fail"
    assert res["null_rate.location"].status == "warn"  # 100% null > 0.5
    assert res["null_rate.title"].status == "pass"
