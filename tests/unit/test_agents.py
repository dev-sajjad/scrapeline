"""Agent orchestration tests with scripted drivers (no network, no API key, local sandbox)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scrapeline.adapters.declarative import SiteSpec, spec_path_for
from scrapeline.agents.llm import DriverResult, NoopDriver, Usage
from scrapeline.agents.repairer import repair_case
from scrapeline.agents.tester import evaluate_spec, security_check_python
from scrapeline.agents.tools import Workspace, build_tool_functions
from scrapeline.evals.runner import compute_metrics
from scrapeline.quality.drift import diagnose
from scrapeline.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
DRIFT = ROOT / "tests" / "sites" / "drift"
GOLDEN = ROOT / "tests" / "fixtures" / "golden" / "static-list.json"
SOURCE = "fixture-static-list"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@dataclass
class ScriptedDriver:
    """Calls the real tools like an agent would, with a deterministic edit."""

    edit: Callable[[str], str]
    claim: str = "Fixed: updated the selector. Tester passed."
    run_tests: bool = True
    name: str = "scripted"
    model: str = "claude-opus-5-5"
    calls: int = 0
    seen_users: list[str] = field(default_factory=list)

    def __call__(
        self, *, system: str, user: str, tools: Sequence[Any], max_iterations: int
    ) -> DriverResult:
        self.calls += 1
        self.seen_users.append(user)
        t = {fn.__name__: fn for fn in tools}
        assert "untrusted" in system.lower() or "never follow" in system.lower()
        spec = t["read_adapter"]()
        t["read_raw_sample"](n=1)
        t["write_adapter"](spec_yaml=self.edit(spec))
        if self.run_tests:
            t["run_tester"]()
        usage = Usage(input_tokens=12_000, output_tokens=900, cache_read_tokens=8_000)
        return DriverResult(4, usage, self.claim, "end_turn", self.model)


def run(case: str, driver: Any, settings: Settings) -> Any:
    return repair_case(SOURCE, DRIFT / case, driver=driver, settings=settings, sandbox_mode="local")


def test_successful_repair_first_attempt(settings: Settings) -> None:
    driver = ScriptedDriver(lambda s: s.replace("td.title", "td.meeting-name"))
    res = run("01-title-class-renamed", driver, settings)
    assert res.repair_needed and res.passed and res.first_attempt_pass
    assert res.attempts == 1 and not res.false_success and not res.regression
    assert res.accuracy_before == 0.0 and res.accuracy_after == 1.0
    assert res.spec_diff_lines == 2  # one-line selector change
    assert res.cost_usd > 0 and res.tool_calls == 4
    assert "title" in driver.seen_users[0]  # diagnosis points the agent at the broken field
    # Re-tests use the source's own locale ('03/01/2026' only passes under its mdy).
    assert '"locale_origin": "source"' in driver.seen_users[0]


def test_false_claim_is_caught_and_attempts_are_bounded(settings: Settings) -> None:
    driver = ScriptedDriver(lambda s: s.replace("td.title", "td.wrong-guess"), run_tests=False)
    res = run("01-title-class-renamed", driver, settings)
    assert not res.passed and res.attempts == 3 and driver.calls == 3
    assert res.false_success  # agent said "Fixed", independent tester disagreed
    assert "did not pass the independent tester" in driver.seen_users[1]


def test_control_case_makes_no_llm_call(settings: Settings) -> None:
    driver = ScriptedDriver(lambda s: s)
    res = run("09-cosmetic-only", driver, settings)
    assert res.passed and not res.repair_needed and driver.calls == 0 and res.cost_usd == 0


def test_noop_baseline_never_passes_broken_case(settings: Settings) -> None:
    res = run("02-table-class-renamed", NoopDriver(), settings)
    assert not res.passed and res.attempts == 3 and res.tokens == 0


@pytest.mark.parametrize(
    ("case", "edit"),
    [
        ("02-table-class-renamed", lambda s: s.replace("table.meetings", "table.meeting-table")),
        ("04-agenda-link-class", lambda s: s.replace("a.agenda", "a.doc-link-agenda")),
        ("05-next-link-changed", lambda s: s.replace('"a[rel=next]"', '"a.pager-next"')),
        ("07-status-badge", lambda s: s.replace("em.status", "span.badge-cancelled")),
        ("08-id-attribute-renamed", lambda s: s.replace("data-id", "data-meeting-key")),
        (
            "03-date-moved-to-attribute",
            lambda s: s.replace(
                "{css: td.date, transform: date}",
                "{css: td.date time, attr: datetime, transform: date}",
            ),
        ),
    ],
)
def test_known_fixes_pass_the_tester(
    case: str, edit: Callable[[str], str], settings: Settings
) -> None:
    """Every drift case is repairable by a spec-only change (the benchmark is fair)."""
    fixed = edit(spec_path_for(SOURCE).read_text())
    report = evaluate_spec(fixed, DRIFT / case, GOLDEN, settings, mode="local", source_id=SOURCE)
    assert report.passed and report.locale_origin == "source"


def make_ws(tmp_path: Path, settings: Settings) -> Workspace:
    (tmp_path / "spec.yaml").write_text(spec_path_for(SOURCE).read_text())
    return Workspace(
        SOURCE,
        tmp_path,
        DRIFT / "01-title-class-renamed",
        GOLDEN,
        "collect",
        settings,
        sandbox_mode="local",
    )


def test_tools_are_scoped_validated_and_logged(tmp_path: Path, settings: Settings) -> None:
    ws = make_ws(tmp_path, settings)
    fns = build_tool_functions(ws)
    before = ws.spec_path.read_text()
    assert fns["write_adapter"](
        spec_yaml="schema: Meeting\nrecord: tr\nfields:\n  attendees: {css: td}\n"
    ).startswith("ERROR")
    assert ws.spec_path.read_text() == before  # invalid spec never written
    assert fns["write_generated_test"](name="../../evil.py", code="x=1").startswith("ERROR")
    assert fns["write_generated_test"](
        name="test_x.py", code="import os\nos.system('id')"
    ).startswith("ERROR")
    assert fns["read_raw_sample"](n=99).startswith("ERROR")
    page = fns["read_raw_sample"](n=1)
    assert page.count("<untrusted_page_content") == 1 and "never follow" in page
    assert [c.ok for c in ws.calls] == [False, False, False, False, True]


def test_security_check_python() -> None:
    bad = "import os\nimport socket\nopen('/etc/passwd')\neval('1')\nx.__class__.__subclasses__()"
    findings = security_check_python(bad)
    assert any("os" in f for f in findings) and any("socket" in f for f in findings)
    assert any("open" in f for f in findings) and any("eval" in f for f in findings)
    assert any("__subclasses__" in f for f in findings)
    ok = (
        "from scrapeline.pipeline.normalise import parse_date\nimport re\n\n"
        "def f(x):\n    return parse_date(x)\n"
    )
    assert security_check_python(ok) == []


def test_tester_rejects_personal_data_fields(settings: Settings) -> None:
    spec = spec_path_for(SOURCE).read_text()
    report = evaluate_spec(
        spec + "\n", DRIFT / "09-cosmetic-only", GOLDEN, settings, mode="local", source_id=SOURCE
    )
    assert report.passed


def test_drift_diagnosis_names_broken_field() -> None:
    spec = SiteSpec.from_yaml(spec_path_for(SOURCE).read_text())
    old = (ROOT / "tests/sites/static-list/index.html").read_text()
    new = (DRIFT / "01-title-class-renamed" / "index.html").read_text()
    d = diagnose(spec, new, old)
    assert d["suspect_fields"] == ["title"]
    assert d["structure_changed"] and "td.title" in d["classes_removed"]
    d2 = diagnose(spec, (DRIFT / "06-table-to-cards" / "index.html").read_text())
    assert d2["record_matches"] == 0 and d2["suspect_fields"] == ["<record selector>"]


def test_usage_cost_and_metrics() -> None:
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000)
    assert u.cost("claude-opus-5-5") == pytest.approx(4.0 + 2.0 + 0.2)
    assert u.cost("claude-haiku-5-5") == pytest.approx(0.1 + 0.05 + 0.01)
    from scrapeline.agents.repairer import RepairResult

    cases = [
        RepairResult(
            "s", "a", True, True, 1, True, False, False, 0.0, 1.0, cost_usd=0.5, tool_calls=4
        ),
        RepairResult(
            "s",
            "b",
            True,
            False,
            3,
            False,
            True,
            False,
            0.0,
            0.0,
            cost_usd=1.0,
            tool_calls=6,
            tool_errors=1,
        ),
        RepairResult("s", "c", False, True, 0, True, False, False, 1.0, 1.0),
    ]
    m = compute_metrics(cases)
    assert m["repair_success_rate"] == 0.5 and m["false_success_rate"] == 0.5
    assert m["avg_attempts"] == 2.0 and m["controls_untouched"] == "1/1"
    assert m["cost_per_successful_repair"] == 1.5 and m["tool_call_failure_rate"] == 0.1
