# ADR-0005: URL policy v1 scope (no connection pinning)

Status: accepted · Date: 2026-11-17

## Context
`fetch/urlpolicy.py` resolves DNS and rejects non-public addresses before every request and after every redirect. A DNS-rebinding attacker could answer the check with a public IP and the connection with a private one. Defeating that requires connecting to the validated IP (custom httpx transport that pins the address while keeping SNI and the Host header).

## Decision
v1 does not pin. Mitigations in place: per-source host allowlist (only official hosts we register), `not ip.is_global` rejection (loopback, RFC1918, link-local incl. 169.254.169.254, CGNAT, reserved, v4-mapped v6), redirect revalidation, redirect cap, no userinfo URLs, and prod disables the fixture private-host exception. Agent-generated code never fetches at all (offline sandbox, ADR-0004).

## Consequences
Residual risk: an allowlisted government host whose DNS is compromised could rebind between check and connect. Accepted for v1; revisit with a pinning transport if sources are ever registered from untrusted input.
