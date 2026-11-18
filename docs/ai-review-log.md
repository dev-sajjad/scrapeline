# AI-generated code review log

One entry per agent PR. Record what the agent produced, what the reviewer found, and the decision. Rejections are as valuable as acceptances.

| Date | Agent run id | Source | Decision | Findings |
|---|---|---|---|---|
| 2026-11-17 | claude-code | step 1 | fixed before commit | `ruff format .` reformatted the Python code block inside CLAUDE.md (ruff now formats Markdown code fences), breaking the documented adapter contract layout. Restored the block and excluded `*.md` from ruff. |
| 2026-11-17 | claude-code | step 1 | fixed before commit | `env_file: {path, required}` mapping form was rejected by the local Compose version ("must be a string"); switched to the list form, which both old and new Compose accept. |
