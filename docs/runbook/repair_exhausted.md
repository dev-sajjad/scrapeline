# Runbook: `repair_exhausted` incident

The repair agent failed 3 attempts. The source is now `broken`.

1. Read the agent runs: `select attempt, outcome, cost_usd, tools_called from ops.agent_run where source_id = ... order by started_at desc`.
2. Inspect the last workspace report (tester JSON) and the drift diagnosis.
3. Usually the page needs logic a spec cannot express → hand-write `parse.py` (with tests) or extend the declarative engine.
4. After the fix merges: `scrapeline source set-status <source> draft`, run, verify, then `active`.
5. Record what the agent got wrong in `docs/ai-review-log.md`; consider a new prompt version (`repairer.v2.md`) and re-run evals.
