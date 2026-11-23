from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from scrapeline.adapters.rawdoc import ParseResult
from scrapeline.pipeline.identity import content_hash, meeting_natural_key
from scrapeline.pipeline.locale import Locale
from scrapeline.pipeline.models import Meeting
from scrapeline.pipeline.normalise import absolute_url, clean_text, parse_date, parse_time
from scrapeline.quality.antibot import BlockKind, classify
from scrapeline.store.raw import FilesystemRawStore, ext_for


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-25", date(2026, 11, 25)),
        ("11/25/2026", date(2026, 11, 25)),
        ("Tue, Nov 25th 2026", date(2026, 11, 25)),
        ("Tuesday, November 25, 2026", date(2026, 11, 25)),
        ("25 Nov 2026", date(2026, 11, 25)),
        ("2026-11-25T00:00:00", date(2026, 11, 25)),
        ("not a date", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(raw: str | None, expected: date | None) -> None:
    assert parse_date(raw, locale=Locale(date_order="mdy")) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2:00 PM", time(14, 0)),
        ("9:30am", time(9, 30)),
        ("9:30 a.m.", time(9, 30)),
        ("14:05", time(14, 5)),
        ("TBD", None),
        (None, None),
    ],
)
def test_parse_time(raw: str | None, expected: time | None) -> None:
    assert parse_time(raw) == expected


def test_clean_text_and_urls() -> None:
    assert clean_text("  Council\r\n Chamber\u00a0 ") == "Council Chamber"
    assert clean_text("   ") is None
    assert absolute_url("/a/b.pdf", "https://x.gov/m/") == "https://x.gov/a/b.pdf"
    assert absolute_url("javascript:void(0)", "https://x.gov/") is None
    assert absolute_url("#top", "https://x.gov/") is None


def meeting(**kw) -> Meeting:
    base = dict(external_id="1", title="City Council", meeting_date=date(2026, 1, 5))
    return Meeting(**(base | kw))


def test_content_hash_ignores_identity_and_source_timestamps() -> None:
    a = meeting(
        canonical_url="https://x.gov/1", source_modified_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    b = meeting(canonical_url="https://x.gov/other", source_modified_at=None)
    assert content_hash(a) == content_hash(b)


def test_content_hash_changes_with_content() -> None:
    assert content_hash(meeting()) != content_hash(meeting(title="City Council (corrected)"))


def test_natural_key_is_stable_and_normalised() -> None:
    k1 = meeting_natural_key(
        body="City Council", meeting_date=date(2026, 1, 5), title=" Regular  Meeting "
    )
    k2 = meeting_natural_key(
        body="city council", meeting_date=date(2026, 1, 5), title="Regular Meeting!"
    )
    assert k1 == k2 and k1.startswith("nk:")


def test_parse_result_routes_invalid_records_to_rejects() -> None:
    result = ParseResult()
    result.add(Meeting, {"external_id": "1", "title": "  ", "meeting_date": date(2026, 1, 1)})
    result.add(Meeting, {"external_id": "2", "title": "Ok", "meeting_date": None})
    result.add(Meeting, {"external_id": "3", "title": "Ok", "meeting_date": date(2026, 1, 1)})
    assert len(result.records) == 1
    assert [r.record_type for r in result.rejects] == ["meeting", "meeting"]
    assert "meeting_date" in result.rejects[1].reason


def test_raw_store_is_content_addressed_and_immutable(tmp_path: Path) -> None:
    store = FilesystemRawStore(tmp_path)
    at = datetime(2026, 11, 17, tzinfo=UTC)
    first = store.put("legistar-seattle", b"[1]", "json", at)
    again = store.put("legistar-seattle", b"[1]", "json", at)
    assert first.created and not again.created and first.key == again.key
    assert first.key.startswith("legistar-seattle/2026/11/17/")
    assert store.get(first.key) == b"[1]"
    with pytest.raises(ValueError):
        store.put("../escape", b"x", "json", at)


def test_ext_for() -> None:
    assert ext_for("application/json; charset=utf-8") == "json"
    assert ext_for(None, "https://x.gov/a.pdf?x=1") == "pdf"
    assert ext_for("application/octet-stream", "https://x.gov/a") == "bin"


@pytest.mark.parametrize(
    ("status", "body", "ctype", "kind"),
    [
        (403, b"no", "text/html", BlockKind.FORBIDDEN),
        (429, b"", "text/html", BlockKind.RATE_LIMITED),
        (200, b"<html>Verify you are human</html>", "text/html", BlockKind.CHALLENGE),
        (
            200,
            b"<html><body><div id=app></div><script src=a.js></script></body></html>",
            "text/html",
            BlockKind.EMPTY_SHELL,
        ),
        (
            200,
            b"<html><body><h1>Meetings</h1><table><tr><td>City Council regular meeting"
            b"</td></tr></table></body></html>",
            "text/html",
            None,
        ),
        (200, b'{"captcha": "word inside JSON data"}', "application/json", None),
    ],
)
def test_antibot_classify(status: int, body: bytes, ctype: str, kind: BlockKind | None) -> None:
    assert classify(status, body, ctype) == kind
