# Job requirements → where scrapeline proves them

Source posting: Turl Street Group, "Junior Data Engineer (Scraping & AI Agents)". Phase numbers refer to CLAUDE.md §7.

## Responsibilities
| Posting line | Proof | Phase | Status |
|---|---|---|---|
| Write and maintain scrapers for government websites, document portals, meeting archives (Playwright, Python) | hand-written adapters for 4 fixture sites + real public sources registered via YAML; `fetch/browser.py` Playwright pool | 2 | ☑ Legistar platform adapter, 3 real cities (step 2); HTML/Playwright in step 3 |
| Contribute to agents that generate, test and repair scrapers | `agents/generator.py`, `agents/repairer.py`, deterministic `agents/tester.py` | 6 | ☐ |
| Review AI-generated code before it ships | PR template AI-review checklist; `docs/ai-review-log.md` with accepted and rejected agent PRs | 6 | ☐ |
| Build data pipelines into storage and processing systems | raw store → `pipeline/` → PostgreSQL → dbt marts; watermarks, backfill, reprocess-from-raw | 3, 4 | ☑ raw store → pydantic → Postgres upsert, watermarks, backfill, reprocess-from-raw |
| Run data quality checks, find cause of broken/drifting scrapers, fix them | `quality/checks.py`, `quality/drift.py`, `ops.incident`, runbook, bounded repair | 4, 6 | ☐ |
| Improve prompts, tools and evaluation checks | versioned `agents/prompts/`, `agents/tools/`, `evals/` with 11 metrics per prompt version, `docs/evals.md` | 6 | ☐ |
| Collaborate with senior engineers on pipeline design and reliability | ADRs, PR-only workflow, runbook, architecture doc | all | ☐ |

## Required skills
| Posting line | Proof | Phase | Status |
|---|---|---|---|
| Strong Python, working SQL | typed `src/` package, mypy strict; Alembic migrations, SKIP LOCKED queue, dbt | 2–4 | ☑ typed package, mypy strict; Alembic migrations, upserts, advisory locks |
| Web scraping incl. dynamic pages, pagination, anti-bot issues | js-table fixture + Playwright; five pagination strategies; `quality/antibot.py` detects 403/429/challenges and escalates, never evades | 1, 2 | ☐ |
| Daily practical use of AI coding agents; give context, break tasks down, catch mistakes | this CLAUDE.md, `docs/ai-workflow.md`, checklist-driven tasks, review log of caught mistakes | 0 | ☐ |
| Read unfamiliar code fast, judge critically, never merge what you don't understand | AI-review checklist, rejected agent PRs with reasons | 6 | ☐ |
| ETL concepts and basic data modelling | identity/content-hash model, schema_version, dims and facts | 3, 4 | ☑ identity + content-hash change detection, lineage columns, schema_version |
| Git and normal code review workflow | protected main, Conventional Commits, PR template, CI | 0 | ☑ branch per step, Conventional Commits, CI, PR template |
| Problem-solving and debugging | incident → runbook → fix traceable in git history | 4 | ☐ |

## Nice to have
| Posting line | Proof | Phase | Status |
|---|---|---|---|
| LLM agents / tool-calling workflows | Anthropic SDK tool runner (ADR-0002), eight narrow tools | 6 | ☐ |
| Evals or test harnesses for LLM output | `evals/` benchmarks and metrics; golden-based grading | 6 | ☐ |
| PostgreSQL, Docker | Postgres 16 schema; Compose dev/prod/agents profiles; sandbox (ADR-0004) | 0–6 | ☑ Postgres 16 + Alembic; Playwright-based app image; offline sandbox with isolation check |
| Kafka, Kubernetes | Out of scope by ADR-0003; reason documented | n/a | n/a |
| PDF and document extraction, OCR | `pipeline/documents.py`, pymupdf, OCR flag, 40-doc golden set | 5 | ☐ |
