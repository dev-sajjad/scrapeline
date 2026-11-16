# Runbook: `blocked` incident (403 / 429 past policy / challenge page / empty shell / robots disallow)

Never evade (CLAUDE.md §0.3): no proxies, no stealth, no CAPTCHA solving.

1. Check `raw.fetch_log` for the run: status codes, Retry-After, URLs.
2. 429: lower `politeness.rps`, widen the cadence; the circuit breaker already backs off.
3. 403 / challenge: look for an official API or data feed (many cities use Legistar, Socrata, ArcGIS). Prefer it.
4. robots.txt disallow: respect it. Keep the data link-only or drop the field.
5. No legitimate path: `scrapeline source set-status <source> retired --note "<why>"`.
