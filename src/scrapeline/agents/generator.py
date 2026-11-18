"""Generator agent for long-tail sites. Selectors first.

instruction + captured sample pages -> spec.yaml in a scoped workspace -> deterministic tester
(self-consistency; golden rows when they exist) -> agent/* branch with spec + draft source YAML.
The agent never sees the live site and never writes outside its workspace.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from scrapeline.adapters.declarative import spec_path_for
from scrapeline.agents.llm import AgentDriver, AgentRunLog, load_prompt
from scrapeline.agents.repairer import _claimed_success, commit_on_agent_branch
from scrapeline.agents.tester import SandboxMode, evaluate_spec
from scrapeline.agents.tools import Workspace, as_beta_tools, build_tool_functions
from scrapeline.ops.logging import get_logger
from scrapeline.settings import PROJECT_ROOT, Settings

log = get_logger(__name__)
PROMPT_VERSION = 1
MAX_ATTEMPTS = 3
MAX_ITERATIONS = 16

SKELETON = """spec_version: 1
schema: Meeting
record: "REPLACE with a selector matching one meeting element"
fields:
  title: {css: "REPLACE"}
  meeting_date: {css: "REPLACE", transform: date}
"""


@dataclass
class GenerationResult:
    source_id: str
    case_id: str
    passed: bool  # self-consistency (what the agent could see)
    attempts: int
    first_attempt_pass: bool
    graded: bool = False  # golden rows were available for grading
    golden_passed: bool | None = None
    field_accuracy: float | None = None
    false_success: bool = False  # claimed/self-consistent but golden disagrees
    tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    agent_run_ids: list[str] = field(default_factory=list)
    final_spec: str | None = None
    branch: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "final_spec"}


def generate_site(
    source_id: str,
    pages_dir: Path,
    *,
    instruction: str,
    driver: AgentDriver,
    settings: Settings,
    engine: Engine | None = None,
    golden_for_grading: Path | None = None,
    sandbox_mode: SandboxMode = "docker",
    eval_run_id: uuid.UUID | None = None,
    apply: bool = False,
    source_yaml: str | None = None,
) -> GenerationResult:
    prompt_version, system = load_prompt("generator", PROMPT_VERSION)
    run_log = AgentRunLog(
        engine, "generate", source_id, prompt_version, pages_dir.name, eval_run_id
    )
    result = GenerationResult(source_id, pages_dir.name, False, 0, False)
    candidate = SKELETON
    n_pages = len(list(pages_dir.glob("*.html")))
    feedback = (
        f"Write a spec for new source `{source_id}`.\nInstruction: {instruction}\n"
        f"{n_pages} sample page(s) were captured. Start with read_instruction and read_raw_sample(1)."
    )
    root: Path | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started_at = datetime.now(UTC)
        root = PROJECT_ROOT / "data" / "agent" / f"gen-{source_id}-{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=True)
        (root / "spec.yaml").write_text(candidate)
        ws = Workspace(source_id, root, pages_dir, None, instruction, settings, sandbox_mode)
        functions = build_tool_functions(ws)
        tools = as_beta_tools(functions) if driver.name == "anthropic" else list(functions.values())
        out = driver(system=system, user=feedback, tools=tools, max_iterations=MAX_ITERATIONS)
        candidate = ws.spec_path.read_text()
        final = evaluate_spec(candidate, pages_dir, None, settings, mode=sandbox_mode)
        outcome = (
            "passed"
            if final.passed
            else ("budget_exceeded" if out.stopped == "budget_exceeded" else "failed")
        )
        result.agent_run_ids.append(
            str(
                run_log.record(
                    attempt=attempt,
                    result=out,
                    outcome=outcome,
                    calls=ws.calls,
                    started_at=started_at,
                )
            )
        )
        result.attempts = attempt
        result.tokens += out.usage.total
        result.cost_usd = round(result.cost_usd + out.usage.cost(out.model), 6)
        result.tool_calls += len(ws.calls)
        result.tool_errors += sum(1 for c in ws.calls if not c.ok)
        claimed = _claimed_success(out.final_text, ws)
        if final.passed:
            result.passed = True
            result.first_attempt_pass = attempt == 1
            result.final_spec = candidate
            break
        if claimed:
            result.false_success = True
        if out.stopped in ("budget_exceeded", "refusal"):
            break
        feedback = (
            f"Attempt {attempt} did not pass the independent tester:\n```json\n"
            f"{json.dumps(final.summary(), indent=1)}\n```\nFix the workspace spec and re-run the tester."
        )

    if golden_for_grading is not None and result.final_spec is not None:
        graded = evaluate_spec(
            result.final_spec, pages_dir, golden_for_grading, settings, mode=sandbox_mode
        )
        result.graded = True
        result.golden_passed = graded.passed
        result.field_accuracy = graded.golden.get("field_accuracy")
        if not graded.passed:
            result.false_success = True  # looked fine to self-consistency, wrong vs golden
    if result.passed and apply and result.final_spec is not None:
        files = {spec_path_for(source_id): result.final_spec}
        if source_yaml:
            files[PROJECT_ROOT / "config" / "sources" / f"{source_id}.yaml"] = source_yaml
        if root is not None and (root / "tests").exists():
            for test_file in (root / "tests").glob("test_*.py"):
                files[
                    PROJECT_ROOT
                    / "tests"
                    / "generated"
                    / source_id.replace("-", "_")
                    / test_file.name
                ] = test_file.read_text()
        result.branch = commit_on_agent_branch(
            f"agent/generate-{source_id}-{result.agent_run_ids[-1][:8]}",
            files,
            f"feat(adapter): agent-generated spec for {source_id}\n\nAgent run ids: "
            f"{', '.join(result.agent_run_ids)}\nTester: self-consistency passed. Source stays `draft` "
            "until a human reviews sample rows, merges, and a live run passes quality checks.",
        )
    shutil.rmtree(PROJECT_ROOT / "data" / "agent", ignore_errors=True)
    log.info("generate.done", source_id=source_id, passed=result.passed, cost=result.cost_usd)
    return result
