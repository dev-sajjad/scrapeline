"""URL policy (SSRF guard) applied to every outbound URL. CLAUDE.md §1.2.

Rules: http/https only; no userinfo; host on the source allowlist (exact or subdomain);
DNS-resolved addresses must all be globally routable (rejects loopback, private, link-local
incl. cloud metadata 169.254.169.254, CGNAT, multicast, reserved, unspecified).
Hosts in `private_hosts` (fixture hosts, dev/test only) skip the address check.

Known v1 gap (ADR-0005): the connection is not pinned to the validated IP, so a DNS rebind
between check and connect is possible. Allowlisted public hosts keep the residual risk low.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from scrapeline.errors import URLPolicyError

Resolver = Callable[[str], list[str]]


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise URLPolicyError(host, f"dns resolution failed ({exc.strerror})") from exc
    return sorted({str(info[4][0]) for info in infos})


def _host_matches(host: str, allowed: str) -> bool:
    return host == allowed or host.endswith("." + allowed)


@dataclass(frozen=True)
class URLPolicy:
    allowed_hosts: frozenset[str]
    private_hosts: frozenset[str] = frozenset()
    resolver: Resolver = field(default=system_resolver, compare=False)

    @classmethod
    def for_hosts(
        cls,
        hosts: Iterable[str],
        *,
        private_hosts: Iterable[str] = (),
        resolver: Resolver = system_resolver,
    ) -> URLPolicy:
        return cls(
            allowed_hosts=frozenset(h.lower().rstrip(".") for h in hosts),
            private_hosts=frozenset(h.lower() for h in private_hosts),
            resolver=resolver,
        )

    def check(self, url: str) -> str:
        """Return the normalised URL or raise URLPolicyError with a reason."""
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise URLPolicyError(url, f"scheme '{scheme or '<none>'}' not allowed")
        if parts.username is not None or parts.password is not None:
            raise URLPolicyError(url, "userinfo in url not allowed")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise URLPolicyError(url, "missing host")
        if not any(_host_matches(host, allowed) for allowed in self.allowed_hosts):
            raise URLPolicyError(url, f"host '{host}' not on allowlist")
        if host not in self.private_hosts:
            self._check_addresses(url, host)
        netloc = host if parts.port is None else f"{host}:{parts.port}"
        return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))

    def _check_addresses(self, url: str, host: str) -> None:
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        addresses = [str(literal)] if literal is not None else self.resolver(host)
        if not addresses:
            raise URLPolicyError(url, "host resolved to no addresses")
        for addr in addresses:
            ip = ipaddress.ip_address(addr)
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            if not ip.is_global or ip.is_multicast:
                raise URLPolicyError(url, f"resolves to non-public address {ip}")
