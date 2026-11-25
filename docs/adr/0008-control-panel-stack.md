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

## Implementation notes (2026-11-25)
- **Live events.** Each panel process holds one `LISTEN` connection and fans notifications out to its open streams through bounded queues. A slow browser is dropped and reconnects rather than slowing the others. A new stream replays the latest 50 matching events. A reconnecting browser sends `Last-Event-ID` and gets only what it missed, up to 500 events; beyond that the stream starts over. If the listening connection drops, the hub reconnects with backoff and catches up from `ops.event`, so no committed event is lost.
- **Scraped content stays inert.** Templates autoescape everything. Raw responses are previewed as escaped text and downloaded only as attachments, never rendered on the panel's origin. The content security policy allows scripts and styles from `/static` only.
- **Bound to this machine.** The compose service publishes on `127.0.0.1` and the app refuses other `Host` headers, which blocks DNS rebinding. Login, CSRF protection and the audit trail arrive with the first write actions.
- **Analytics are read in place.** Marts are read from the dbt DuckDB file in read-only mode, one short connection per request; a missing, locked or outdated warehouse gives a page that says what to run.
