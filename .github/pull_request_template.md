## What

## Why

## Author
- [ ] Human
- [ ] Agent run id: `ops.agent_run.id = ______` (agent PRs must link the run)

## AI-generated code review checklist (required when any file under `src/scrapeline/adapters/` changed)
- [ ] I read every line of the diff and can explain it.
- [ ] `parse()` is pure: no network, no I/O, no globals.
- [ ] No changes to `fetch/`, politeness settings, or `config/sources/*.yaml` rate limits.
- [ ] Selectors are specific and anchored; no `*`, no positional-only chains.
- [ ] Trusted tests and `tests/fixtures/golden/` untouched (agent PRs); new tests live in `tests/generated/` until a human promotes them.
- [ ] Golden-row comparison passes (`scrapeline verify <source>`).
- [ ] No hard-coded dates, URLs or secrets.
- [ ] Error paths return typed failures, never silent empty lists.
- [ ] `ops.agent_run` shows tokens + cost for this change.

## Evidence
Paste `scrapeline verify` output or CI link.
