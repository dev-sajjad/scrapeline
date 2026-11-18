# scrapeline

A production-grade platform for collecting structured data from public websites, APIs and document archives, with AI agents that write and repair scrapers under deterministic control.

Register a source with one YAML file. scrapeline fetches it politely, stores every raw response immutably, loads clean and traceable records into PostgreSQL, checks data quality on every run, and detects when a site changes. When a scraper breaks, a sandboxed AI agent proposes a fix. Fixes are judged by deterministic tests and merged only by a human.

[![ci](https://github.com/dev-sajjad/scrapeline/actions/workflows/ci.yml/badge.svg)](https://github.com/dev-sajjad/scrapeline/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.12-blue)
![postgres](https://img.shields.io/badge/postgres-16-blue)

## Features

**Collection**
- Platform adapters (one adapter serves every site on a vendor platform) and declarative site adapters (CSS selectors in `spec.yaml`, no code)
- Static HTML, JSON APIs and JavaScript-rendered pages (Playwright, including "load more" pagination)
- PDF text extraction with an OCR fallback flag for scanned documents
- Incremental runs from a per-source watermark, explicit backfills, and full reprocessing from stored raw data

**Reliability**
- Idempotent loads: identity plus content hash means reruns never duplicate rows, and edits update rows in place
- Immutable, content-addressed raw store; every database row links back to the exact bytes it came from
- Retries with backoff, per-host circuit breaker, one concurrent run per source
- Postgres job queue (`FOR UPDATE SKIP LOCKED`) with leases, retries and dead-lettering

**Safety and politeness**
- One network path for all traffic: URL policy (SSRF guard), host allowlist, robots.txt and a per-host rate limit
- Blocks (403, 429, challenge pages, empty shells) are detected and raised as incidents, never evaded
- No personal data collected beyond what a source's instruction requires

**Data quality**
- Per-run checks: record counts, null rates, duplicates, reject rate; results stored in `ops.check_result`
- Drift diagnosis: which selectors stopped matching and how the page structure changed
- Incidents with an open, acknowledged, closed lifecycle and a runbook per incident type

**AI agents**
- **Repair agent** fixes a broken site adapter; **generator agent** writes a new one from sample pages
- Agents work in a scoped workspace through 8 typed tools; page content is treated as untrusted data
- Every candidate is tested in an offline Docker sandbox (`--network none`, read-only, no secrets) against expected rows
- At most 3 attempts, then the source is marked `broken` and an incident opens
- Fixes land on `agent/*` branches for human review; every LLM call is logged with tokens and cost

## Architecture

```
config/sources/<id>.yaml
        │
        ▼
   registry ──► scheduler ──► ops.job queue ──► worker
                                                  │
        ┌─────────────────────────────────────────┘
        ▼
   fetch: URL policy → robots.txt → circuit breaker → rate limit → httpx | Playwright
        │
        ▼
   raw store (sha256, immutable) + raw.fetch_log
        │
        ▼
   adapter.parse(raw)   pure, no I/O
        │
        ▼
   validate → normalise → identity + content hash → upsert with lineage → rejects
        │
        ▼
   quality checks → drift diagnosis → incidents
        │
        ▼
   agents (offline sandbox) → deterministic tester → agent/* branch → human review
```

The scraping pipeline never depends on an LLM. If the AI service is unavailable, collection, loading, checks and alerts keep running.

## Quick start

Requirements: Docker (Desktop, OrbStack, Colima or any Docker Engine runtime) and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
make setup        # Python 3.12, dependencies, Chromium
make up           # PostgreSQL and fixture sites
make migrate      # apply database migrations
make demo         # end-to-end walkthrough, no API key needed
```

Run a real source:

```bash
uv run scrapeline run legistar-seattle
uv run scrapeline status legistar-seattle
```

Agents need `ANTHROPIC_API_KEY` in `.env`. Everything else works without it.

## Adding a source

A source on a supported platform needs only its tenant:

```yaml
# config/sources/legistar-seattle.yaml (excerpt)
id: legistar-seattle
platform: legistar
tenant: seattle
entry: https://webapi.legistar.com/v1/seattle
allowed_hosts: [webapi.legistar.com]
schema: Meeting
politeness: { rps: 1, respect_robots: true }
quality: { min_records: 1, max_reject_rate: 0.05 }
```

A custom site gets a declarative spec, written by hand or by the generator agent:

```yaml
# src/scrapeline/adapters/sites/<site>/spec.yaml
schema: Meeting
record: "table.meetings tbody tr"
fields:
  title:        {css: td.title}
  meeting_date: {css: td.date, transform: date}
  agenda_url:   {css: a.agenda, attr: href, transform: absolute_url, optional: true}
  external_id:  {css: a.detail, attr: href, transform: absolute_url}
next_page: {css: "a[rel=next]", attr: href}
```

Most site changes are a renamed class or a moved element, so most repairs are a one-line change to this file.

## Example data

The Legistar platform adapter collects meetings for three US cities with no city-specific code:

| Source | Meetings | Date range |
|---|---|---|
| `legistar-seattle` | 332 | 2025-09 → 2027-01 |
| `legistar-oakland` | 195 | 2025-07 → 2026-11 |
| `legistar-denver` | 3,031 | 1995-01 → 2026-11 |

Running a source again inserts nothing new; `scrapeline reprocess` rebuilds every row from stored raw bytes.

## Command line

| Command | Purpose |
|---|---|
| `scrapeline run <source>` | Fetch, store, parse, load and check one source (`--backfill --from --to` for history) |
| `scrapeline reprocess <source>` | Rebuild records from stored raw data |
| `scrapeline verify <source>` | Compare parsed output with expected rows |
| `scrapeline documents <source>` | Download linked PDFs and extract text |
| `scrapeline drift diagnose <source>` | Explain why a site adapter stopped matching |
| `scrapeline agent repair` / `generate` | Run the repair or generator agent |
| `scrapeline eval run` | Run agent benchmarks |
| `scrapeline schedule` / `worker` | Enqueue due sources and process the job queue |
| `scrapeline status <source>` | Latest runs, checks and open incidents |

A read-only HTTP API (`make api`) serves `/health`, `/sources`, `/meetings` and `/incidents`.

## Project layout

```
src/scrapeline/
  fetch/       network access: URL policy, rate limiter, robots.txt, httpx client, Playwright pool
  store/       raw store, database engine, table definitions
  adapters/    adapter contract, declarative spec engine, platform and site adapters
  pipeline/    models, normalisation, identity, loading, run orchestration, documents
  quality/     checks, drift diagnosis, anti-bot detection, incidents
  ops/         queue, worker, run bookkeeping, logging
  agents/      LLM wrapper, tools, tester, sandbox harness, repairer, generator, prompts
  evals/       benchmark runner and metrics
  serve/       read-only HTTP API
config/sources/  one YAML file per source
alembic/         database migrations
tests/           unit, integration, fixture sites, expected rows, real-page snapshots
docker/          app image, offline sandbox image, fixture web server
docs/            ADRs, runbooks, eval results
```

## Development

```bash
make check        # lint, format check, strict type check, unit tests
make test-int     # integration tests against PostgreSQL and fixture sites
make test-browser # Playwright tests
make sandbox-check  # verify sandbox isolation on this Docker runtime
make eval-baseline  # agent benchmarks without an LLM (free)
make eval           # agent benchmarks with Claude (requires API key)
```

Tooling: ruff, mypy (strict), pytest, pre-commit, GitHub Actions. CI runs lint and types, 113 unit tests, 24 integration and browser tests, a migration drift check, both image builds and the sandbox isolation check.

## Documentation

- [Architecture decisions](docs/adr): agent loop, job queue, sandbox networking, URL policy scope
- [Runbooks](docs/runbook): one page per incident type
- [Agent evals](docs/evals.md): benchmark design and results
- [AI review log](docs/ai-review-log.md): defects found in AI-written code, with root causes

## Responsible collection

scrapeline collects only public pages and official APIs, identifies itself with a descriptive user agent, honours robots.txt and rate limits, and never bypasses blocks, logins or paywalls. Each source records its terms and robots.txt review in a `legal` block before it is activated.

## Roadmap

- Per-source locale, timezone and date order for sources outside the US
- Generic record schema for data beyond meetings
- Sources from more countries and languages, including generator-built adapters
- S3 raw store backend
