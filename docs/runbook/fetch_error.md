# Runbook: `fetch_error` incident

Transport failures or non-retryable HTTP errors after retries (e.g. 400/404/5xx exhausted).

1. Check `raw.fetch_log` errors for the run.
2. Server-side misconfiguration (example: Legistar `sfgov` returned 400 for every events query) → retire or replace the source.
3. Transient outages: the next scheduled run retries; the circuit breaker prevents hammering.
