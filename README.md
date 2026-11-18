# scrapeline

Production-grade scraper platform for public civic data: register a source with a YAML file, run it politely, keep every raw response immutably, land clean, traceable records in PostgreSQL, check quality on every run, and let a bounded AI agent propose fixes for broken scrapers. A human reviews and merges every fix.

Build plan and rules: [CLAUDE.md](CLAUDE.md) · Decisions: [docs/adr](docs/adr) · Job coverage: [docs/job-requirements-map.md](docs/job-requirements-map.md) · AI review log: [docs/ai-review-log.md](docs/ai-review-log.md)

## Real data, today

One Legistar platform adapter, three real US cities, zero city-specific code (each city is one YAML file):

| Source | Meetings | Date range | Cancelled |
|---|---|---|---|
| `legistar-seattle` | 332 | 2025-09 → 2027-01 | 84 |
| `legistar-oakland` | 195 | 2025-07 → 2026-11 | 30 |
| `legistar-denver` | 3,031 | 1995-01 → 2026-11 | 59 |

Rerunning a source produces no duplicates (`inserted 0 / unchanged N`). `scrapeline reprocess` rebuilds every row from stored raw bytes. A 4th tenant (`sfgov`) failed with HTTP 400 because of a server-side misconfiguration. The pipeline opened an incident, and the source was retired and replaced (see the review log).

## AI agents, under deterministic control

| Piece | What it does |
|---|---|
| `scrapeline drift diagnose` | Deterministic: which selectors stopped matching, which classes appeared or disappeared |
| Repair agent (`agents/repairer.py`) | Claude (Opus 5.5, `tool_runner`) edits only a workspace copy of the site's `spec.yaml` through 8 typed, path-scoped tools. Max 3 attempts, then the source is marked `broken` and an incident opens |
| Generator agent (`agents/generator.py`) | Writes a new site's `spec.yaml` from captured sample pages (long-tail sites). It never fetches live pages |
| Tester (`agents/tester.py`) | **The only judge.** Spec validation, AST allowlist, then `docker run --network none --read-only …` against saved pages, then golden comparison (or self-consistency for new sites) |
| Release | The agent commits to an `agent/*` branch. A human reviews with the PR checklist and merges. The live run starts in `draft`, and quality checks promote it to `active` |
| Evals | 10 drift cases + 5 generation cases. Metrics: success rate, first-attempt pass, **false success** (agent says "fixed", tester disagrees), regression, attempts, tool errors, tokens, cost. See [docs/evals.md](docs/evals.md) |

Selectors first, code second: most redesigns are a renamed class, so most repairs are a one-line YAML diff that is cheap to generate and easy to review.

## Quick start

Works with any Docker Engine runtime (Kiri, Docker Desktop, OrbStack, Colima).

```bash
cp .env.example .env              # ANTHROPIC_API_KEY only needed for agents (step 4+)
make setup                        # python 3.12 + deps + chromium + pre-commit
make up                           # postgres + fixture sites
make migrate                      # alembic upgrade head
uv run scrapeline run legistar-seattle
uv run scrapeline status legistar-seattle
make test && make test-int        # unit (no network) + integration (postgres)
make sandbox-check                # proves the agent sandbox has no network on this runtime
make demo                         # end-to-end walkthrough (no API key needed)
make eval-baseline                # agent benchmark without LLM; `make eval` runs Claude (needs key)
make api                          # read API: /health /sources /meetings /incidents
```

Operations: `scrapeline schedule` (cron) enqueues due active sources; `scrapeline worker` claims jobs with `FOR UPDATE SKIP LOCKED`, leases, retries with backoff, and dead-letters failed jobs. Runbooks: [docs/runbook](docs/runbook).

ADRs: [0002 agent loop on Anthropic SDK](docs/adr/0002-agent-loop-anthropic-sdk.md) · [0003 Postgres queue](docs/adr/0003-queue-postgres-skip-locked.md) · [0004 offline sandbox](docs/adr/0004-sandbox-networking.md) · [0005 URL policy scope](docs/adr/0005-urlpolicy-v1-scope.md)

## How a run works

```
config/sources/<id>.yaml → registry → adapter.discover(window)
  → FetchClient (URL policy → robots.txt → circuit breaker → per-host rate limit → retries)
  → raw store (sha256, immutable) + raw.fetch_log row per attempt
  → adapter.parse(raw)  (pure: records + rejects + next pages + watermark)
  → validate (pydantic) → identity + content hash → upsert with lineage → pipeline.rejects
  → quality checks (pandas) → ops.check_result → ops.incident on failure
```

## What "production grade" means here

| Property | Proven by |
|---|---|
| Idempotent | integration test: run twice → equal counts; title change → update, not duplicate |
| Incremental | watermark on source-side `EventLastModifiedUtc` with 24h overlap; explicit `--backfill --from --to` |
| Recoverable | immutable content-addressed raw store; `scrapeline reprocess --from-raw` |
| Polite | identified User-Agent, robots.txt, per-host token bucket, Retry-After honoured |
| Safe | SSRF URL policy on every URL and redirect (ADR-0005); offline sandbox for generated code (ADR-0004) |
| Resilient | retries with backoff, per-host circuit breaker, one run per source (advisory lock) |
| Honest about blocks | 403/429/challenge pages detected and turned into incidents; never evaded |
| Observable | JSON logs with run/source ids, `ops.run`, `raw.fetch_log`, `ops.check_result`, `ops.incident` |
| Traceable | every row carries source, external id, content hash, schema version, first/last seen, run, scraper version, raw sha256 |
| Tested | 113 unit (no network) + 22 integration/browser tests (Postgres, nginx fixtures, Playwright, docker sandbox) + migration drift check in CI |
| AI-reviewed | every caught AI mistake logged with root cause in [docs/ai-review-log.md](docs/ai-review-log.md) |

Compliance: public pages and official APIs only, robots.txt honoured, never evades blocks, collects no personal data (Legistar body contact fields are never read).
