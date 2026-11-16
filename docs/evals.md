# Agent evals

Benchmark: 10 drift cases (`tests/sites/drift/`, single mutations of a real-world-style meetings site: class renames, layout to cards, date moved to an attribute, pagination change, ID attribute rename, + 1 cosmetic control that must NOT trigger a repair). Graded only by the deterministic tester (offline Docker sandbox + human-owned golden rows). Metrics per CLAUDE.md §5.4.

Run: `make eval` (Anthropic driver, needs `ANTHROPIC_API_KEY`, costs money; per-run budget `AGENT_MAX_USD_PER_RUN`) or `uv run scrapeline eval run --driver noop` (baseline, free). Raw results: `evals/results/*.json`; rows in `evals.run` / `evals.case_result`.

## Results

| Date | Driver | Model | Prompt | Repair success | First-attempt pass | False success | Regression | Avg attempts | Controls untouched | Tool-call failure | Cost total | Cost / repair |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-17 | noop (baseline, no LLM) | – | repairer.v1 | 0.0 | 0.0 | 0.0 | 0.0 | 3.0 | 1/1 | 0.0 | $0.0 | – |
| _pending_ | anthropic | claude-opus-5-5 (effort high) | repairer.v1 | run `make eval` | | | | | | | | |

Baseline reading: 9/10 cases break the reference spec and none fix themselves; the control case is left alone with zero LLM calls. The Anthropic row is filled in after the first paid run (no API key was configured when this suite was built).

Reference fixes: `tests/unit/test_agents.py::test_known_fixes_pass_the_tester` proves each case is repairable by a spec-only change, so the benchmark is fair.

## Rules
- New prompt text = new file `agents/prompts/repairer.v<N>.md` = new row here.
- Switch a role's model only after a row shows no regression vs the current one.
- An agent's own claim of success is never counted; `false_success_rate` measures how often it claims success the tester rejects.
