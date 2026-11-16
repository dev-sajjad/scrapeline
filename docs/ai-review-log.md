# AI-generated code review log

One entry per agent PR. Record what the agent produced, what the reviewer found, and the decision. Rejections are as valuable as acceptances.

| Date | Agent run id | Source | Decision | Findings |
|---|---|---|---|---|
| 2026-11-17 | claude-code | step 1 | fixed before commit | `ruff format .` reformatted the Python code block inside CLAUDE.md (ruff now formats Markdown code fences), breaking the documented adapter contract layout. Restored the block and excluded `*.md` from ruff. |
| 2026-11-17 | claude-code | step 1 | fixed before commit | `env_file: {path, required}` mapping form was rejected by the local Compose version ("must be a string"); switched to the list form, which both old and new Compose accept. |
| 2026-11-17 | claude-code | step 2 | fixed before commit | First draft used `pg_try_advisory_xact_lock` for "one run per source"; a run spans several transactions, so the lock would release after the first commit. Switched to a session lock held on a dedicated connection for the whole run. |
| 2026-11-17 | claude-code | step 2 | fixed before commit | `reprocess` failed `duplicate_rate` on correct data: replaying raw from two runs (24h overlap window) legitimately contains the same meeting twice. Duplicate check now counts identities repeated inside one raw document (a real parser bug); other checks run on deduplicated records. Found by running reprocess against live Seattle data, not by unit tests. |
| 2026-11-17 | claude-code | step 2 | fixed before commit | Token-bucket limiter could spin forever: after a 429 penalty, sleeping exactly the computed delay left 0.99999… tokens and a sub-ULP delay. Surfaced by a fake-clock test that hung; fixed with epsilon + minimum sleep; regression test added. |
| 2026-11-17 | claude-code | step 2 | source replaced | `legistar-sfgov` returned HTTP 400 for every events query ("'Agenda Draft Status' … is not setup in settings") — server-side tenant misconfiguration. Pipeline opened a `fetch_error` incident as designed; source retired in DB and replaced by `legistar-denver`. |
