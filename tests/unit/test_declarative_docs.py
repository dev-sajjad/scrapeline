from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scrapeline.adapters.declarative import SiteSpec, parse_html, spec_path_for
from scrapeline.adapters.rawdoc import RawDoc
from scrapeline.errors import ConfigError, ParseError
from scrapeline.pipeline.documents import extract_pdf
from scrapeline.pipeline.models import Meeting
from scrapeline.quality.golden import compare, load_golden

ROOT = Path(__file__).resolve().parents[2]
SITES = ROOT / "tests" / "sites"
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
BASE = "http://localhost:8080"


def raw_page(site: str, name: str) -> RawDoc:
    content = (SITES / site / name).read_bytes()
    url = f"{BASE}/{site}/{'' if name == 'index.html' else name}"
    return RawDoc(
        source_id="t",
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        content=content,
        sha256=RawDoc.hash_bytes(content),
        fetched_at=datetime(2026, 11, 17, tzinfo=UTC),
    )


@pytest.fixture
def static_spec() -> SiteSpec:
    return SiteSpec.from_yaml(spec_path_for("fixture-static-list").read_text())


def test_static_list_spec_matches_golden_across_pages(static_spec: SiteSpec) -> None:
    records, pages = [], 0
    name = "index.html"
    while name:
        result = parse_html(static_spec, raw_page("static-list", name))
        records += [r for r in result.records if isinstance(r, Meeting)]
        pages += 1
        name = result.next_targets[0].url.rsplit("/", 1)[1] if result.next_targets else ""
    report = compare(records, load_golden(GOLDEN / "static-list.json", BASE))
    assert pages == 3
    assert report.passed, report.field_diffs[:3]
    assert report.field_accuracy == 1.0


def test_record_selector_matching_nothing_is_a_parse_error(static_spec: SiteSpec) -> None:
    broken = static_spec.model_copy(update={"record": "table.renamed tbody tr"})
    with pytest.raises(ParseError, match="matched 0"):
        parse_html(broken, raw_page("static-list", "index.html"))


def test_missing_required_field_becomes_reject(static_spec: SiteSpec) -> None:
    fields = dict(static_spec.fields)
    fields["meeting_date"] = fields["meeting_date"].model_copy(update={"css": "td.when"})
    result = parse_html(
        static_spec.model_copy(update={"fields": fields}), raw_page("static-list", "index.html")
    )
    assert not [r for r in result.records if isinstance(r, Meeting)]
    assert len(result.rejects) == 10
    assert "meeting_date" in result.rejects[0].reason


def test_spec_rejects_fields_not_on_schema() -> None:
    with pytest.raises(ConfigError, match="fields not in Meeting"):
        SiteSpec.from_yaml("schema: Meeting\nrecord: tr\nfields:\n  attendees: {css: td}\n")


def test_spec_rejects_unknown_keys_and_schemas() -> None:
    with pytest.raises(ConfigError):
        SiteSpec.from_yaml("schema: Meeting\nrecord: tr\nfields: {}\nexec: rm -rf /\n")
    with pytest.raises(ConfigError, match="unknown schema"):
        SiteSpec.from_yaml("schema: Nope\nrecord: tr\nfields: {}\n")


def test_pdf_text_extraction_and_ocr_flag() -> None:
    files = sorted((SITES / "pdf-archive" / "files").glob("*.pdf"))
    golden = {
        d["file"]: d
        for d in __import__("json").loads((GOLDEN / "pdf-archive.json").read_text())["documents"]
    }
    for path in files:
        out = extract_pdf(path.read_bytes(), allow_ocr=False)
        want = golden[path.name]
        assert out.page_count == want["page_count"]
        assert out.needs_ocr == want["needs_ocr"], path.name
        for phrase in want["contains"]:
            assert phrase in out.text


def test_non_pdf_is_parse_error() -> None:
    with pytest.raises(ParseError):
        extract_pdf(b"<html>not a pdf</html>")


def test_golden_compare_reports_field_diffs() -> None:
    from datetime import date

    golden = [
        {"external_id": "1", "title": "A", "meeting_date": "2026-01-01"},
        {"external_id": "2", "title": "B", "meeting_date": "2026-01-02"},
    ]
    recs = [
        Meeting(external_id="1", title="A", meeting_date=date(2026, 1, 1)),
        Meeting(external_id="3", title="C", meeting_date=date(2026, 1, 3)),
    ]
    report = compare(recs, golden)
    assert not report.passed
    assert report.missing == ["2"] and report.unexpected == ["3"]
    assert report.field_accuracy == 0.5
