# ADR-0008: Control panel on FastAPI, server-rendered HTML, HTMX and Server-Sent Events

Status: accepted · Date: 2026-11-24

## Context
Operators need one place to see health, sources, sites, runs, data, lineage and incidents, to watch runs live, and to trigger allowed actions. It runs in Docker next to the worker and must stay easy to maintain by a Python team.

## Decision
- The panel is part of the FastAPI app: Jinja templates, HTMX for interactions, Server-Sent Events for live updates. No JavaScript build step.
- Live updates come from `ops.event`: the publisher inserts a row and calls `pg_notify`; the panel listens and streams events to the browser.
- **The panel never scrapes.** Every action enqueues a typed job on the existing Postgres queue; the worker executes it, so actions are retried, logged and audited like scheduled work.
- There is no shell or free-form command box. Actions are an allowlist with validated inputs.
- The panel binds to localhost by default, requires a login token from `.env`, and protects forms against CSRF.

## Alternatives
- **React or another SPA**: richer UI, but a second language, a build pipeline and duplicated types. Rejected for now.
- **Streamlit**: quick to start, but weak for forms, live streams and actions. Rejected.
- **Polling instead of SSE**: simpler, but slower and noisier. Rejected; LISTEN/NOTIFY needs no extra infrastructure.

## Consequences
One image and one language for the whole platform. Highly interactive views stay modest by design; a separate frontend can be added later behind the same JSON API.
