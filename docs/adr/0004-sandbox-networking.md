# ADR-0004: Sandbox for generated code uses a fixtures-only internal network

Status: accepted · Date: 2026-11-14

## Context
Agent-generated adapters must run somewhere before a human reviews them. The first scaffold said `network_mode: none` "except an allow-listed host", which is contradictory: a container with no network cannot reach any host. Options were an egress proxy with destination allowlists, or an internal network that holds only the fixture sites.

## Decision
The sandbox container joins `sandbox_net` (`internal: true`), which contains only the fixture nginx. It has no route to the internet, no `.env`, a read-only filesystem, `cap_drop: ALL`, a memory limit and a per-run wall-clock limit. Generated code is tested there against fixture sites and golden data only. Live sites are reached solely by the trusted worker through `FetchClient`, after a human merges the PR, and a source becomes `active` only after a live run passes quality checks. Repair agents read new page samples from the raw store captured by the worker; they never fetch live.

## Alternatives
- **Egress proxy** (Squid/tinyproxy with DNS/IP validation, allowlists, logging): lets agents test against live sites, but adds a component with its own security surface and maintenance for a solo developer. Deferred; revisit via a new ADR if agent runs against live pages become necessary.
- **No sandbox, run in worker**: generated code would get network and DB access before review. Rejected.

## Consequences
Two release gates instead of one (fixtures, then live in `draft`). Simpler and safer. Drift repair depends on the worker having captured the changed page into raw, which the drift detector already does.
