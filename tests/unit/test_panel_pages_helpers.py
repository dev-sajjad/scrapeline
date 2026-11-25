"""Operator page helpers without a database: flags, verdicts, filters and macros."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jinja2 import Undefined

from scrapeline.analytics.layout import STATE_PATH
from scrapeline.serve.panel import filters
from scrapeline.serve.panel.deps import templates
from scrapeline.serve.panel.pages.health import lake_freshness, verdict
from scrapeline.serve.panel.pages.runs import load_rows
from scrapeline.serve.panel.pages.sources import (
    attention,
    config_view,
    is_overdue,
    personal_data,
    records_loaded,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
QUIET = {
    "queued": 0,
    "running": 0,
    "stalled": 0,
    "failed": 0,
    "failed_total": 0,
    "oldest_queued": None,
}


def render(source: str, **context: object) -> str:
    return templates.env.from_string(source).render(**context)


@pytest.mark.parametrize(
    ("status", "cadence", "since_last", "expected"),
    [
        ("active", "daily", timedelta(hours=30), False),  # late, but within the grace
        ("active", "daily", timedelta(hours=37), True),
        ("active", "hourly", timedelta(minutes=95), True),
        ("active", "weekly", timedelta(days=10), False),
        ("active", "weekly", timedelta(days=11), True),
        ("active", "daily", None, True),  # scheduled but never run
        ("active", "manual", timedelta(days=400), False),
        ("active", "manual", None, False),
        ("draft", "hourly", None, False),
        ("broken", "daily", timedelta(days=5), False),  # broken is not scheduled
    ],
)
def test_overdue_follows_the_cadence(
    status: str, cadence: str, since_last: timedelta | None, expected: bool
) -> None:
    last = NOW - since_last if since_last is not None else None
    assert is_overdue(status, cadence, last, NOW) is expected


def test_attention_names_the_reasons() -> None:
    long_ago = NOW - timedelta(days=3)
    assert attention("active", "daily", "failed", long_ago, NOW) == ("last run failed", "overdue")
    assert attention("active", "manual", "failed", long_ago, NOW) == ("last run failed",)
    assert attention("broken", "daily", "failed", long_ago, NOW) == ("last run failed",)
    assert attention("active", "daily", "succeeded", NOW - timedelta(hours=2), NOW) == ()


def test_drafts_and_retired_sources_are_never_flagged() -> None:
    for status in ("draft", "retired"):
        assert attention(status, "hourly", "failed", None, NOW) == ()


def test_verdict_separates_known_problems_from_broken_scraping() -> None:
    assert verdict(0, QUIET, 0) == ("ok", [])
    assert verdict(0, QUIET, 1) == ("attention", ["1 open incident"])
    state, problems = verdict(2, {**QUIET, "stalled": 1, "failed": 3}, 2)
    assert state == "bad"
    assert problems == [
        "2 sources need attention",
        "1 stalled job",
        "3 failed jobs in the last 24 h",
        "2 open incidents",
    ]
    assert verdict(1, QUIET, 0)[1] == ["1 source needs attention"]


def test_jobs_that_died_long_ago_do_not_spoil_the_verdict() -> None:
    assert verdict(0, {**QUIET, "failed_total": 7}, 0) == ("ok", [])


def test_lake_never_exported(tmp_path: Path) -> None:
    lake = lake_freshness(tmp_path / "lake")
    assert lake.last_export is None and lake.exported == 0 and lake.error is None
    assert lake.tables > 0


def test_unreadable_lake_state_is_reported_not_raised(tmp_path: Path) -> None:
    state = tmp_path / STATE_PATH
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json")
    lake = lake_freshness(tmp_path)
    assert lake.error and "unreadable lake state" in lake.error


def test_config_view_flags_the_placeholder_review_date() -> None:
    assert config_view({"legal": {"terms_checked": "1970-01-01"}})["terms_unreviewed"]
    assert config_view({})["terms_unreviewed"]
    view = config_view(
        {"legal": {"terms_checked": "2026-11-24"}, "locale": {"language": "en", "timezone": None}}
    )
    assert not view["terms_unreviewed"]
    assert view["locale"] == {"language": "en"}


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"allow_pii": True, "purpose": "Contact the seller"}, "kept"),
        # The record store keeps personal data only with a purpose as well.
        ({"allow_pii": True}, "no-purpose"),
        ({"allow_pii": True, "purpose": "   "}, "no-purpose"),
        ({"allow_pii": False, "purpose": "Price tracking"}, "blanked"),
        ({}, "blanked"),
        (None, "blanked"),
        # Usage that does not validate grants nothing, as in the lake export.
        ({"allow_pii": "maybe", "purpose": "x"}, "unreadable"),
        ({"allow_pii": True, "purpose": "x", "unknown": 1}, "unreadable"),
        ("allow everything", "unreadable"),
    ],
)
def test_personal_data_follows_the_record_store_rule(usage: object, expected: str) -> None:
    assert personal_data(usage) == expected
    assert config_view({"usage": usage})["personal_data"] == expected


def test_records_loaded_counts_what_the_load_stored() -> None:
    loaded = {
        "product": {"inserted": 1, "updated": 8, "unchanged": 11, "restored": 2, "removed": 5},
        "meeting": {"inserted": 25},
        "junk": 3,
    }
    assert records_loaded(loaded) == 47  # removed records are not stored ones
    assert records_loaded({}) == 0  # a load that rolled back
    assert records_loaded(None) is None  # a run still going
    assert records_loaded({"product": {"inserted": "3", "updated": None}}) == 0


def test_load_rows_skip_malformed_stats() -> None:
    rows = load_rows({"product": {"inserted": 1, "removed": 2}, "junk": 3})
    assert rows == [
        {
            "dataset": "product",
            "inserted": 1,
            "updated": None,
            "unchanged": None,
            "restored": None,
            "removed": 2,
        }
    ]
    assert load_rows(None) == []


def test_new_filters() -> None:
    start = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    assert filters.iso(start) == "2026-11-24T12:00:00+00:00"
    assert filters.iso(None) == ""
    assert filters.duration(start, start + timedelta(seconds=1.5)) == "1.5 s"
    assert filters.duration(start, None) == filters.EMPTY
    assert filters.loaded(
        {"product": {"inserted": 0, "updated": 8, "removed": 1, "unchanged": 11}}
    ) == ("product: 8 updated, 1 removed, 11 unchanged")
    assert filters.loaded({"meeting": {"inserted": 0}}) == "meeting: nothing"
    assert filters.loaded({}) == filters.EMPTY
    assert filters.to_json({"b": 1, "a": "<x>"}) == '{"a": "<x>", "b": 1}'
    assert filters.http_url("https://example.org/a") == "https://example.org/a"
    assert filters.http_url("javascript:alert(1)") == ""
    assert filters.http_url("//example.org") == ""
    assert filters.http_url(None) == ""


def test_filters_treat_missing_template_keys_as_empty() -> None:
    missing = Undefined(name="pages")
    for show in (filters.number, filters.nbytes, filters.duration_ms, filters.dt, filters.ago):
        assert show(missing) == filters.EMPTY
    assert render("{{ stats.pages | number }}", stats={}) == filters.EMPTY


def test_reltime_macro_carries_machine_and_exact_time() -> None:
    html = render(
        '{% from "_macros.html" import reltime %}{{ reltime(value) }}',
        value=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
    )
    assert '<time datetime="2026-11-24T12:00:00+00:00" title="2026-11-24 12:00 UTC">' in html


def test_ext_link_only_links_http_urls() -> None:
    macro = '{% from "_macros.html" import ext_link %}{{ ext_link(url) }}'
    safe = render(macro, url="https://example.org/?a=1&b=2")
    assert 'href="https://example.org/?a=1&amp;b=2"' in safe
    assert 'rel="noopener noreferrer nofollow"' in safe
    hostile = render(macro, url="javascript:alert(1)")
    assert "href" not in hostile and "javascript:alert(1)" in hostile


def test_json_block_escapes_content() -> None:
    html = render(
        '{% from "_macros.html" import json_block %}{{ json_block(value) }}',
        value={"note": "<script>alert(1)</script>"},
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<details" in html
