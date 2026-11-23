"""Tester checks: field pattern safety and the locale a spec is tested under."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scrapeline.adapters.declarative import SITES_DIR, SiteSpec, spec_path_for
from scrapeline.agents.tester import evaluate_spec, has_nested_quantifier, pattern_problems
from scrapeline.agents.tools import Workspace, build_tool_functions
from scrapeline.pipeline.locale import Locale
from scrapeline.settings import Settings
from tests.unit.test_parsing_declarative import PRODUCT_PAGE, PRODUCT_SPEC, UK, write_pages

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "tests/sites/static-list"
SOURCE = "fixture-static-list"
REFERENCE = spec_path_for(SOURCE).read_text()
NEW_SITE = "gen-new-site"  # no config file: a site the generator has never seen


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.mark.parametrize(
    "pattern",
    ["(a+)+", "(.*)*", r"(\w+\s?)+$", r"((ab)*c)+", r"(?:\d+,){2,}", "(x{2,})*"],
)
def test_nested_quantifiers_are_found(pattern: str) -> None:
    assert has_nested_quantifier(pattern)
    assert any("repeats a group" in p for p in pattern_problems(pattern))


@pytest.mark.parametrize(
    "pattern",
    [
        r"\((\d+) available\)",
        r"star-rating (\w+)",
        r"SKU: (\S+)",
        "ab (.+)",
        r"(\d+)?",
        "(a|b)+",
        r"[(+]+",
        r"\(.+\)+",
        "(a+){1}",
        r"[)]+(b)+",
    ],
)
def test_ordinary_patterns_pass(pattern: str) -> None:
    assert not has_nested_quantifier(pattern)
    assert pattern_problems(pattern) == []


def test_tester_refuses_unsafe_and_long_patterns(tmp_path: Path, settings: Settings) -> None:
    pages = write_pages(tmp_path / "pages", {"index.html": PRODUCT_PAGE})

    def check(sku_pattern: str) -> list[str]:
        spec = PRODUCT_SPEC.replace(r"'SKU: (\S+)'", f"'{sku_pattern}'")
        report = evaluate_spec(spec, pages, None, settings, mode="local", locale=UK)
        assert not report.passed and report.stage == "static"
        return report.security

    assert any(
        f.startswith("sku: pattern") and "repeats a group" in f for f in check(r"(\w+\s?)+$")
    )
    assert any("longer than 200" in f for f in check("SKU: " + "x" * 200))


def test_human_written_site_specs_pass_the_pattern_check() -> None:
    for path in SITES_DIR.glob("*/spec.yaml"):
        spec = SiteSpec.from_yaml(path.read_text())
        patterns = [f.pattern for f in spec.fields.values() if f.pattern is not None]
        assert not [p for pattern in patterns for p in pattern_problems(pattern)], path


def test_a_new_site_is_tested_strictly_not_under_the_fixture_locale(settings: Settings) -> None:
    # The pages hold '03/01/2026'. Under the fixture's mdy it reads as 1 March and passes;
    # a new site has no such locale, so the tester must not assume it.
    report = evaluate_spec(REFERENCE, STATIC, None, settings, mode="local", source_id=NEW_SITE)
    assert report.locale_origin == "strict default"
    assert report.locale == Locale().model_dump(mode="json")
    assert not report.passed and report.rejects >= 1
    summary = report.summary()
    assert summary["locale_origin"] == "strict default" and summary["locale"]["date_order"] is None


@pytest.mark.parametrize(
    ("options", "origin", "date_order"),
    [
        ({"source_id": SOURCE}, "source", "mdy"),
        ({"source_id": NEW_SITE, "benchmark": True}, "benchmark", "mdy"),
        ({"source_id": SOURCE, "locale": Locale(date_order="dmy")}, "explicit", "dmy"),
    ],
)
def test_the_report_names_the_locale_and_its_origin(
    settings: Settings, options: dict[str, Any], origin: str, date_order: str
) -> None:
    report = evaluate_spec(REFERENCE, STATIC, None, settings, mode="local", **options)
    assert report.locale_origin == origin and report.locale["date_order"] == date_order
    assert report.summary()["locale_origin"] == origin


def test_read_instruction_reports_identity_types_and_locale_origin(
    tmp_path: Path, settings: Settings
) -> None:
    (tmp_path / "spec.yaml").write_text(REFERENCE)
    ws = Workspace(NEW_SITE, tmp_path, STATIC, None, "collect", settings, sandbox_mode="local")
    info = json.loads(build_tool_functions(ws)["read_instruction"]())
    assert info["allowed_fields"]["canonical_url"] == "url"
    assert info["allowed_fields"]["external_id"] == "text"
    assert (info["locale_origin"], info["locale"]["date_order"]) == ("strict default", None)
    ws.benchmark = True
    info = json.loads(build_tool_functions(ws)["read_instruction"]())
    assert (info["locale_origin"], info["locale"]["date_order"]) == ("benchmark", "mdy")
