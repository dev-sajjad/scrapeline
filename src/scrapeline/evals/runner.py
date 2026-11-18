"""Eval engine (no LLM of its own): runs a driver over benchmark cases, grades with the
deterministic tester, computes eval metrics, stores evals.run / evals.case_result.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, update

from scrapeline.agents.llm import AgentDriver
from scrapeline.agents.repairer import PROMPT_VERSION, RepairResult, repair_case
from scrapeline.agents.tester import SandboxMode
from scrapeline.ops.logging import get_logger
from scrapeline.settings import PROJECT_ROOT, Settings
from scrapeline.store import tables as t

log = get_logger(__name__)
DRIFT_DIR = PROJECT_ROOT / "tests" / "sites" / "drift"
RESULTS_DIR = PROJECT_ROOT / "evals" / "results"


@dataclass
class SuiteResult:
    eval_run_id: uuid.UUID
    driver: str
    model: str
    prompt_version: str
    cases: list[RepairResult]
    metrics: dict[str, Any]

    def markdown(self) -> str:
        m = self.metrics
        head = (
            f"### repair suite · driver `{self.driver}` · model `{self.model}` · prompt `{self.prompt_version}`\n\n"
            "| Metric | Value |\n|---|---|\n" + "".join(f"| {k} | {v} |\n" for k, v in m.items())
        )
        rows = "".join(
            f"| {c.case_id} | {'yes' if c.repair_needed else 'no'} | {'✅' if c.passed else '❌'} | {c.attempts} "
            f"| {c.accuracy_before:.3f} → {c.accuracy_after:.3f} | {c.spec_diff_lines} | ${c.cost_usd:.4f} |\n"
            for c in self.cases
        )
        return (
            head
            + "\n| Case | Repair needed | Passed | Attempts | Field accuracy | Spec diff lines | Cost |\n|---|---|---|---|---|---|---|\n"
            + rows
        )


def _rate(n: int, d: int) -> float:
    return round(n / d, 3) if d else 0.0


def compute_metrics(cases: list[RepairResult]) -> dict[str, Any]:
    needed = [c for c in cases if c.repair_needed]
    passed = [c for c in needed if c.passed]
    controls = [c for c in cases if not c.repair_needed]
    calls = sum(c.tool_calls for c in cases)
    cost = round(sum(c.cost_usd for c in cases), 4)
    return {
        "cases": len(cases),
        "repair_needed": len(needed),
        "repair_success_rate": _rate(len(passed), len(needed)),
        "first_attempt_pass_rate": _rate(sum(c.first_attempt_pass for c in needed), len(needed)),
        "false_success_rate": _rate(sum(c.false_success for c in needed), len(needed)),
        "regression_rate": _rate(sum(c.regression for c in needed), len(needed)),
        "avg_attempts": round(sum(c.attempts for c in needed) / len(needed), 2) if needed else 0.0,
        "controls_untouched": f"{sum(c.passed and c.attempts == 0 for c in controls)}/{len(controls)}",
        "tool_call_failure_rate": _rate(sum(c.tool_errors for c in cases), calls),
        "tokens_total": sum(c.tokens for c in cases),
        "cost_usd_total": cost,
        "cost_per_successful_repair": round(cost / len(passed), 4) if passed else None,
    }


def run_repair_suite(
    *,
    driver: AgentDriver,
    settings: Settings,
    engine: Engine | None = None,
    cases_dir: Path = DRIFT_DIR,
    limit: int | None = None,
    sandbox_mode: SandboxMode = "docker",
) -> SuiteResult:
    eval_run_id = uuid.uuid4()
    model = getattr(driver, "model", "none")
    prompt_version = f"repairer.v{PROMPT_VERSION}"
    if engine is not None:
        with engine.begin() as conn:
            conn.execute(
                t.eval_run.insert().values(
                    id=eval_run_id,
                    suite="repair",
                    driver=driver.name,
                    model=model,
                    prompt_version=prompt_version,
                )
            )
    case_dirs = sorted(p for p in cases_dir.iterdir() if (p / "case.json").exists())[:limit]
    results: list[RepairResult] = []
    for case_dir in case_dirs:
        meta = json.loads((case_dir / "case.json").read_text())
        res = repair_case(
            meta["source_id"],
            case_dir,
            driver=driver,
            settings=settings,
            engine=engine,
            sandbox_mode=sandbox_mode,
            eval_run_id=eval_run_id,
            apply=False,
        )
        if not meta.get("breaks_reference_spec", True) and res.repair_needed:
            res.note += " | control case unexpectedly needed repair"
        results.append(res)
        if engine is not None:
            with engine.begin() as conn:
                conn.execute(
                    t.eval_case_result.insert().values(
                        eval_run_id=eval_run_id,
                        case_id=res.case_id,
                        passed=int(res.passed),
                        repair_needed=int(res.repair_needed),
                        attempts=res.attempts,
                        first_attempt_pass=int(res.first_attempt_pass),
                        false_success=int(res.false_success),
                        regression=int(res.regression),
                        field_accuracy_before=res.accuracy_before,
                        field_accuracy_after=res.accuracy_after,
                        tool_calls=res.tool_calls,
                        tool_errors=res.tool_errors,
                        tokens=res.tokens,
                        cost_usd=res.cost_usd,
                        details=res.as_dict(),
                    )
                )
        log.info("eval.case", case=res.case_id, passed=res.passed, attempts=res.attempts)
    metrics = compute_metrics(results)
    suite = SuiteResult(eval_run_id, driver.name, model, prompt_version, results, metrics)
    if engine is not None:
        with engine.begin() as conn:
            conn.execute(
                update(t.eval_run)
                .where(t.eval_run.c.id == eval_run_id)
                .values(summary=metrics, finished_at=datetime.now(UTC))
            )
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"repair-{driver.name}-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_text(
        json.dumps(
            {
                "eval_run_id": str(eval_run_id),
                "driver": driver.name,
                "model": model,
                "prompt_version": prompt_version,
                "metrics": metrics,
                "cases": [c.as_dict() for c in results],
            },
            indent=1,
            default=str,
        )
    )
    return suite


GEN_DIR = PROJECT_ROOT / "evals" / "benchmarks" / "generation"


def run_generation_suite(
    *,
    driver: AgentDriver,
    settings: Settings,
    engine: Engine | None = None,
    cases_dir: Path = GEN_DIR,
    limit: int | None = None,
    sandbox_mode: SandboxMode = "docker",
) -> dict[str, Any]:
    """Generate specs from scratch (agent sees no golden rows); grade the result with golden."""
    from scrapeline.agents.generator import PROMPT_VERSION as GEN_PROMPT
    from scrapeline.agents.generator import generate_site

    eval_run_id = uuid.uuid4()
    model = getattr(driver, "model", "none")
    if engine is not None:
        with engine.begin() as conn:
            conn.execute(
                t.eval_run.insert().values(
                    id=eval_run_id,
                    suite="generation",
                    driver=driver.name,
                    model=model,
                    prompt_version=f"generator.v{GEN_PROMPT}",
                )
            )
    results = []
    for case_dir in sorted(p for p in cases_dir.iterdir() if (p / "case.json").exists())[:limit]:
        meta = json.loads((case_dir / "case.json").read_text())
        res = generate_site(
            f"gen-{meta['case']}",
            PROJECT_ROOT / meta["pages"],
            instruction=meta["instruction"],
            driver=driver,
            settings=settings,
            engine=engine,
            golden_for_grading=PROJECT_ROOT / meta["golden"],
            sandbox_mode=sandbox_mode,
            eval_run_id=eval_run_id,
        )
        res.case_id = meta["case"]
        results.append(res)
        if engine is not None:
            with engine.begin() as conn:
                conn.execute(
                    t.eval_case_result.insert().values(
                        eval_run_id=eval_run_id,
                        case_id=res.case_id,
                        passed=int(bool(res.golden_passed)),
                        repair_needed=0,
                        attempts=res.attempts,
                        first_attempt_pass=int(res.first_attempt_pass),
                        false_success=int(res.false_success),
                        regression=0,
                        field_accuracy_before=None,
                        field_accuracy_after=res.field_accuracy,
                        tool_calls=res.tool_calls,
                        tool_errors=res.tool_errors,
                        tokens=res.tokens,
                        cost_usd=res.cost_usd,
                        details=res.as_dict(),
                    )
                )
    n = len(results)
    ok = [r for r in results if r.golden_passed]
    cost = round(sum(r.cost_usd for r in results), 4)
    accs = [r.field_accuracy for r in results if r.field_accuracy is not None]
    metrics = {
        "cases": n,
        "generation_success_rate": _rate(len(ok), n),
        "first_attempt_pass_rate": _rate(
            sum(r.first_attempt_pass and bool(r.golden_passed) for r in results), n
        ),
        "mean_field_accuracy": round(sum(accs) / len(accs), 3) if accs else 0.0,
        "false_success_rate": _rate(sum(r.false_success for r in results), n),
        "avg_attempts": round(sum(r.attempts for r in results) / n, 2) if n else 0.0,
        "tool_call_failure_rate": _rate(
            sum(r.tool_errors for r in results), sum(r.tool_calls for r in results)
        ),
        "tokens_total": sum(r.tokens for r in results),
        "cost_usd_total": cost,
        "cost_per_successful_generation": round(cost / len(ok), 4) if ok else None,
    }
    if engine is not None:
        with engine.begin() as conn:
            conn.execute(
                update(t.eval_run)
                .where(t.eval_run.c.id == eval_run_id)
                .values(summary=metrics, finished_at=datetime.now(UTC))
            )
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"generation-{driver.name}-{datetime.now(UTC):%Y%m%dT%H%M%S}.json").write_text(
        json.dumps(
            {
                "eval_run_id": str(eval_run_id),
                "metrics": metrics,
                "cases": [r.as_dict() for r in results],
            },
            indent=1,
            default=str,
        )
    )
    return {"metrics": metrics, "cases": [r.as_dict() for r in results]}
