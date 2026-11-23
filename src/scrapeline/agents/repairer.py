"""Repair agent orchestration. Bounded, scope-limited, escalates.

Flow per drift case:
  baseline tester on the current spec  -> passes? no repair needed (no LLM call, zero cost)
  attempt 1..3: agent edits ONLY a workspace copy of the spec, tools are path-restricted
               -> independent tester run on the final candidate (agent claims are not evidence)
  passed  -> optionally open a branch + commit for human review (never main, never merge)
  3 fails -> source marked `broken` + `repair_exhausted` incident
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from scrapeline.adapters.declarative import SiteSpec, spec_path_for
from scrapeline.agents.llm import AgentDriver, AgentRunLog, load_prompt
from scrapeline.agents.tester import SandboxMode, TesterReport, evaluate_spec
from scrapeline.agents.tools import Workspace, as_beta_tools, build_tool_functions
from scrapeline.errors import AgentError, ConfigError
from scrapeline.ops.logging import get_logger
from scrapeline.quality.drift import diagnose
from scrapeline.settings import PROJECT_ROOT, Settings

log = get_logger(__name__)

MAX_ATTEMPTS = 3
MAX_ITERATIONS = 12
PROMPT_VERSION = 1


@dataclass
class RepairResult:
    source_id: str
    case_id: str
    repair_needed: bool
    passed: bool
    attempts: int
    first_attempt_pass: bool
    false_success: bool
    regression: bool
    accuracy_before: float
    accuracy_after: float
    tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    agent_run_ids: list[str] = field(default_factory=list)
    final_spec: str | None = None
    spec_diff_lines: int = 0
    branch: str | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "final_spec"}


def _claimed_success(text: str, ws: Workspace) -> bool:
    if ws.last_report is not None and ws.last_report.passed:
        return True
    return bool(
        re.search(r"\b(fixed|repaired|passes|passed|success)\b", text, re.I)
    ) and not re.search(r"\b(cannot|can't|unable|not able|fail)", text, re.I)


def _diff_lines(a: str, b: str) -> int:
    import difflib

    return sum(
        1
        for line in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="")
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def _first_message(source_id: str, report: TesterReport, diagnosis: dict[str, Any]) -> str:
    return (
        f"Source `{source_id}` is broken after a site change. Repair its spec.\n\n"
        f"Tester summary for the CURRENT spec on the new pages:\n```json\n{json.dumps(report.summary(), indent=1)}\n```\n\n"
        f"Deterministic drift diagnosis (selector match counts on the new page 1):\n"
        f"```json\n{json.dumps(diagnosis, indent=1)}\n```\n"
        "Fix the spec with the tools, run the tester, and stop when it passes."
    )


def repair_case(
    source_id: str,
    case_dir: Path,
    *,
    driver: AgentDriver,
    settings: Settings,
    engine: Engine | None = None,
    golden_path: Path | None = None,
    sandbox_mode: SandboxMode = "docker",
    eval_run_id: uuid.UUID | None = None,
    apply: bool = False,
    max_attempts: int = MAX_ATTEMPTS,
) -> RepairResult:
    spec_file = spec_path_for(source_id)
    if not spec_file.exists():
        raise ConfigError(
            f"{source_id} has no declarative spec; only spec repairs are supported in v1"
        )
    original = spec_file.read_text()
    golden_path = (
        golden_path
        or PROJECT_ROOT / "tests/fixtures/golden" / f"{source_id.removeprefix('fixture-')}.json"
    )
    case_id = case_dir.name

    # The source's own locale, as in the agent's tools and the live run after merge.
    before = evaluate_spec(
        original, case_dir, golden_path, settings, mode=sandbox_mode, source_id=source_id
    )
    acc_before = float(before.golden.get("field_accuracy", 0.0))
    if before.passed:
        log.info("repair.not_needed", source_id=source_id, case=case_id)
        return RepairResult(
            source_id,
            case_id,
            False,
            True,
            0,
            True,
            False,
            False,
            acc_before,
            acc_before,
            final_spec=original,
            note="baseline passes; no LLM call",
        )

    prompt_version, system = load_prompt("repairer", PROMPT_VERSION)
    run_log = AgentRunLog(engine, "repair", source_id, prompt_version, case_id, eval_run_id)
    first_page = sorted(case_dir.glob("*.html"), key=lambda p: (p.name != "index.html", p.name))[0]
    diagnosis = diagnose(SiteSpec.from_yaml(original), first_page.read_text())
    result = RepairResult(
        source_id, case_id, True, False, 0, False, False, False, acc_before, acc_before
    )
    candidate = original
    feedback = _first_message(source_id, before, diagnosis)

    for attempt in range(1, max_attempts + 1):
        started_at = datetime.now(UTC)
        root = PROJECT_ROOT / "data" / "agent" / f"{source_id}-{case_id}-{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=True)
        (root / "spec.yaml").write_text(candidate)
        ws = Workspace(
            source_id,
            root,
            case_dir,
            golden_path,
            instruction="Collect public meetings: title, body, date, time, location, status, agenda link.",
            settings=settings,
            sandbox_mode=sandbox_mode,
        )
        functions = build_tool_functions(ws)
        tools = as_beta_tools(functions) if driver.name == "anthropic" else list(functions.values())
        out = driver(system=system, user=feedback, tools=tools, max_iterations=MAX_ITERATIONS)
        candidate = ws.spec_path.read_text()
        # Re-test independently: the agent's own claim is not evidence.
        final = evaluate_spec(
            candidate, case_dir, golden_path, settings, mode=sandbox_mode, source_id=source_id
        )
        outcome = (
            "passed"
            if final.passed
            else (
                "budget_exceeded"
                if out.stopped == "budget_exceeded"
                else "error"
                if out.stopped == "error"
                else "failed"
            )
        )
        run_id = run_log.record(
            attempt=attempt, result=out, outcome=outcome, calls=ws.calls, started_at=started_at
        )
        result.agent_run_ids.append(str(run_id))
        result.attempts = attempt
        result.tokens += out.usage.total
        result.cost_usd = round(result.cost_usd + out.usage.cost(out.model), 6)
        result.tool_calls += len(ws.calls)
        result.tool_errors += sum(1 for c in ws.calls if not c.ok)
        result.accuracy_after = float(final.golden.get("field_accuracy", 0.0))
        if _claimed_success(out.final_text, ws) and not final.passed:
            result.false_success = True
        log.info(
            "repair.attempt",
            source_id=source_id,
            case=case_id,
            attempt=attempt,
            outcome=outcome,
            cost=result.cost_usd,
            tools=len(ws.calls),
        )
        if final.passed:
            result.passed = True
            result.first_attempt_pass = attempt == 1
            result.final_spec = candidate
            break
        if out.stopped in ("budget_exceeded", "refusal"):
            break
        feedback = (
            f"Attempt {attempt} did not pass the independent tester.\n```json\n"
            f"{json.dumps(final.summary(), indent=1)}\n```\nThe workspace spec now contains your last "
            "version. Read it, fix the remaining problems, run the tester, stop when it passes."
        )

    result.regression = result.accuracy_after < acc_before
    if result.final_spec is not None:
        result.spec_diff_lines = _diff_lines(original, result.final_spec)
    if result.passed and apply:
        result.branch = _open_branch(source_id, case_id, spec_file, result)
    if not result.passed and engine is not None and eval_run_id is None:
        _escalate(engine, source_id, case_id, result)
    return result


def _escalate(engine: Engine, source_id: str, case_id: str, result: RepairResult) -> None:
    from scrapeline.ops.runs import set_status
    from scrapeline.quality.incidents import open_incident

    with engine.begin() as conn:
        open_incident(
            conn,
            source_id=source_id,
            kind="repair_exhausted",
            summary=f"repair failed after {result.attempts} attempts ({case_id})",
            details=result.as_dict(),
        )
        try:
            set_status(conn, source_id, "broken", f"repair exhausted on {case_id}")
        except Exception as exc:  # source may not be synced yet
            log.warning("repair.status_not_set", error=repr(exc))


def _git(*args: str) -> str:
    cmd = ["git", *args]
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise AgentError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def commit_on_agent_branch(branch: str, files: dict[Path, str], message: str) -> str:
    """Write files on a new agent/* branch, commit, switch back. Never main; never push/merge."""
    if not branch.startswith("agent/"):
        raise AgentError("agent branches must start with agent/")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise AgentError("working tree has uncommitted changes; refusing to create an agent branch")
    current = _git("rev-parse", "--abbrev-ref", "HEAD")
    _git("switch", "-c", branch)
    try:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            _git("add", str(path.relative_to(PROJECT_ROOT)))
        _git("commit", "-m", message)
    finally:
        _git("switch", current)
    return branch


def _open_branch(source_id: str, case_id: str, spec_file: Path, result: RepairResult) -> str:
    assert result.final_spec is not None
    branch = f"agent/repair-{source_id}-{case_id}-{result.agent_run_ids[-1][:8]}"
    message = (
        f"fix(adapter): agent repair of {source_id} spec ({case_id})\n\n"
        f"Agent run ids: {', '.join(result.agent_run_ids)}\n"
        f"Attempts: {result.attempts}; cost USD: {result.cost_usd}; tester: passed.\n"
        "Needs human review per .github/pull_request_template.md before merge."
    )
    commit_on_agent_branch(branch, {spec_file: result.final_spec}, message)
    shutil.rmtree(PROJECT_ROOT / "data" / "agent", ignore_errors=True)
    return branch
