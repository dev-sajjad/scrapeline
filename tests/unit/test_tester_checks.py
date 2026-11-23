"""Tester checks: field pattern safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from scrapeline.adapters.declarative import SITES_DIR, SiteSpec
from scrapeline.agents.tester import evaluate_spec, has_nested_quantifier, pattern_problems
from scrapeline.settings import Settings
from tests.unit.test_parsing_declarative import PRODUCT_PAGE, PRODUCT_SPEC, UK, write_pages


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
