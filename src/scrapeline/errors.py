"""Typed exceptions. Never return an empty list to mean failure (CLAUDE.md §6)."""

from __future__ import annotations


class ScrapelineError(Exception):
    """Base class for every error raised by scrapeline."""


class ConfigError(ScrapelineError):
    """Invalid source config, settings or registry state."""


class StatusTransitionError(ConfigError):
    """Illegal source status transition (e.g. retired -> active)."""


# --- fetch -----------------------------------------------------------------


class FetchError(ScrapelineError):
    """Any failure to obtain a response."""


class URLPolicyError(FetchError):
    """URL rejected by fetch/urlpolicy.py (scheme, allowlist, private IP, ...)."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"url rejected: {reason}: {url}")
        self.url = url
        self.reason = reason


class RobotsDisallowedError(FetchError):
    """robots.txt disallows this URL for our user agent."""


class CircuitOpenError(FetchError):
    """Too many consecutive failures for a host; requests short-circuited."""


class BlockedError(FetchError):
    """Anti-bot response detected (403, 429 past policy, challenge page, empty shell).

    Handled by backing off and opening an incident. Never evaded.
    """

    def __init__(self, url: str, kind: str, status: int | None = None) -> None:
        super().__init__(f"blocked ({kind}, status={status}): {url}")
        self.url = url
        self.kind = kind
        self.status = status


class HTTPStatusError(FetchError):
    """Non-retryable HTTP status (e.g. 404) or retries exhausted."""

    def __init__(self, url: str, status: int) -> None:
        super().__init__(f"http {status}: {url}")
        self.url = url
        self.status = status


# --- parse / pipeline ------------------------------------------------------


class ParseError(ScrapelineError):
    """Adapter could not interpret a raw document."""


class EmptyResultError(ParseError):
    """A 200 response produced zero records where records were expected."""


class QualityCheckError(ScrapelineError):
    """A blocking quality check failed."""


# --- agents ----------------------------------------------------------------


class AgentError(ScrapelineError):
    """Agent run failed or exceeded its budget."""


class ToolPermissionError(AgentError):
    """Agent tool asked for a path or source it is not assigned to."""
