# scrapeline

Production-grade scraper platform: register any public source with an instruction, let agents generate and repair the scraper under human review, run it politely on a schedule, and land clean, traceable records in PostgreSQL.

Status: scaffold (phase 0). Build plan and rules: [CLAUDE.md](CLAUDE.md). Decisions: [docs/adr](docs/adr). Job coverage: [docs/job-requirements-map.md](docs/job-requirements-map.md).

## Quick start
```bash
cp .env.example .env        # add ANTHROPIC_API_KEY
make setup                  # python 3.12 + deps + chromium
make up                     # postgres, minio, fixture sites
make test
```

## What "production grade" means here
| Property | Proven by |
|---|---|
| Idempotent | run twice, same row counts (test) |
| Incremental | watermarks + explicit backfill |
| Resilient | retries, backoff, circuit breaker, dead-letter, resumable jobs |
| Polite | robots.txt, per-host rate limit, identified User-Agent, cache |
| Observable | JSON logs, ops tables, dashboard, alerts, runbook |
| Tested | fixture sites, golden data, unit + integration CI |
| Reviewed | agents open PRs, checklist, humans merge |
| Recoverable | immutable raw store, rebuild from raw |
| Evaluated | six agent metrics per prompt version |

Compliance: scrapes only public pages, honours robots.txt and rate limits, never evades blocks, collects no personal data.
