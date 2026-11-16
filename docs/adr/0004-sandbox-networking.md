# ADR-0004: Agent-generated parsers run in an offline sandbox (revised)

Status: accepted · Date: 2026-11-14 · Revised: 2026-11-17

## Context
Agent-written adapter code must run before a human reviews it. The first version put the sandbox on an internal Docker network that contained only the fixture nginx. Since then the adapter contract made `parse()` pure (bytes in, records out) and agents only write `parse` logic (a `spec.yaml` first, `parse.py` only when needed). Parsing never needs a network.

## Decision
The tester runs generated parsers with:
`docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --memory 1g --pids-limit 128 -v <case>:/work:ro scrapeline-sandbox`
The image (`docker/sandbox/Dockerfile`) contains only the pure modules (models, normalise, identity, rawdoc, declarative engine) and a harness that reads `/work/input.json` and prints one JSON report. No fetch/, store/, settings, DB driver, secrets or `.env`. Sample pages come from the raw store, captured by the trusted worker. Pagination is followed offline by mapping next-page URLs to captured files.

`scripts/sandbox_check.sh` (`make sandbox-check`, CI job `image`) proves on the active runtime (Kiri, Docker Desktop, ...) that egress and DNS fail, the root FS is read-only, the user is non-root and no secrets are present. A runtime that accepts flags without enforcing them fails this check.

## Alternatives
- **Internal network with fixture nginx** (v1 of this ADR): more moving parts, and network reach that pure parsing does not need. Superseded.
- **Egress proxy for live-site testing**: deferred; live sites are reached only by the trusted worker after human merge.
- **In-process run** (`--local-sandbox`): used for unit tests and fast local loops only; never the release gate.

## Consequences
Release gates: (1) offline sandbox + golden pass, (2) security checks (schema-validated spec; AST allowlist for any `parse.py`), (3) human PR review and merge, (4) live `draft` run by the trusted worker, (5) checks pass → `active`.
