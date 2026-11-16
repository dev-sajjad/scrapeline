from __future__ import annotations

from pathlib import Path

import yaml

from scrapeline.adapters.declarative import spec_path_for
from scrapeline.agents.generator import SKELETON, generate_site
from scrapeline.agents.tester import evaluate_spec
from scrapeline.cli import _draft_source_yaml
from scrapeline.registry.config import SourceConfig
from scrapeline.settings import Settings
from tests.unit.test_agents import ScriptedDriver

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "tests/sites/static-list"
GOLDEN = ROOT / "tests/fixtures/golden/static-list.json"
REFERENCE = spec_path_for("fixture-static-list").read_text()


def gen(spec_text: str) -> object:
    return generate_site(
        "gen-test",
        STATIC,
        instruction="meetings",
        driver=ScriptedDriver(lambda _: spec_text),
        settings=Settings(_env_file=None),
        golden_for_grading=GOLDEN,
        sandbox_mode="local",
    )


def test_generator_success_is_graded_against_golden() -> None:
    res = gen(REFERENCE)
    assert res.passed and res.first_attempt_pass and res.golden_passed
    assert res.field_accuracy == 1.0 and not res.false_success


def test_plausible_but_wrong_spec_is_a_false_success() -> None:
    # Title read from the body column: self-consistent, but wrong values vs golden.
    wrong = REFERENCE.replace("{css: td.title, remove: [em.status]}", "{css: td.body}")
    res = gen(wrong)
    assert res.passed  # self-consistency cannot see the mistake
    assert res.golden_passed is False and res.false_success
    assert res.field_accuracy is not None and res.field_accuracy < 1.0


def test_skeleton_alone_never_passes() -> None:
    res = gen(SKELETON)
    assert not res.passed and res.attempts == 3


def test_self_consistency_requires_reaching_all_pages() -> None:
    no_pager = REFERENCE.replace('next_page: {css: "a[rel=next]", attr: href}\n', "")
    report = evaluate_spec(no_pager, STATIC, None, Settings(_env_file=None), mode="local")
    assert not report.passed
    assert report.golden["checks"]["all_sample_pages_reached"] is False
    ok = evaluate_spec(REFERENCE, STATIC, None, Settings(_env_file=None), mode="local")
    assert ok.passed and ok.golden["mode"] == "self_consistency"


def test_draft_source_yaml_is_valid_and_injection_safe() -> None:
    text = _draft_source_yaml("city-x", "https://www.example.gov/m", 'say "hi"\nstatus: active')
    cfg = SourceConfig.model_validate(yaml.safe_load(text))
    assert cfg.status == "draft" and cfg.owner == "agent"
    assert cfg.instruction is not None and "status: active" in cfg.instruction.collect
