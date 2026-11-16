# Runbook: `drift` incident

**Trigger:** a site adapter's record selector matched nothing (`ParseError`), or quality checks fail after a structure change.

1. `scrapeline status <source>`: confirm the incident and the failing run.
2. `scrapeline drift diagnose <source> --case <dir-with-new-pages>`: which selectors stopped matching, classes added/removed.
3. Repair:
   - `scrapeline agent repair <source> --case <dir> --apply` (max 3 attempts; commits to an `agent/repair-*` branch, never main), or
   - edit `adapters/sites/<source>/spec.yaml` by hand.
4. Review the branch with the PR template's AI-review checklist. Log the decision in `docs/ai-review-log.md`.
5. After merge: `scrapeline run <source>`, then `scrapeline verify <source>` (fixtures). The incident closes when checks pass.
6. Repair exhausted → source is `broken` (see `repair_exhausted.md`).
