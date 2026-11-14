# AI-assisted workflow

How Claude Code is used on this repo, and how its mistakes are caught.

1. Context: `CLAUDE.md` is loaded every session. Tasks are single checklist items from §7.
2. Breakdown: one branch per item; the request names the files, the tests that must pass, and what must not change.
3. Review: every AI-written diff is read in full before commit. Anything not understood is rewritten or rejected.
4. Evidence: mistakes caught are logged in `docs/ai-review-log.md` (same table as agent PRs, author `claude-code`).
5. Verification: `make lint type test` locally, CI on PR. No green CI, no merge.
