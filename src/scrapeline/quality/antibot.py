"""Anti-bot response *detection*. Never evasion (CLAUDE.md §0.3).

A detected block stops the run for that host, backs off and opens an incident. The fix is
an official API, a slower schedule, or retiring the source, never a disguise.
"""

from __future__ import annotations

import re
from enum import StrEnum


class BlockKind(StrEnum):
    FORBIDDEN = "forbidden"  # 401/403
    RATE_LIMITED = "rate_limited"  # 429 past retry policy
    CHALLENGE = "challenge"  # JS/CAPTCHA interstitial
    EMPTY_SHELL = "empty_shell"  # HTML with scripts but no content (rendered pages)


# Markers of common bot-challenge interstitials. Detection only.
_CHALLENGE_MARKERS = re.compile(
    r"(cf-chl-|challenge-platform|cf_chl_opt|just a moment\.\.\.|attention required! \| cloudflare"
    r"|_incapsula_resource|incapsula incident|px-captcha|perimeterx|g-recaptcha|h-captcha"
    r"|hcaptcha\.com|are you a robot|verify you are human|access denied.{0,80}reference #"
    r"|request unsuccessful\. incapsula|data-scrapeline-challenge)",
    re.IGNORECASE | re.DOTALL,
)
_TAGS = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<[^>]+>", re.IGNORECASE | re.DOTALL)


def visible_text_length(html: str) -> int:
    return len(" ".join(_TAGS.sub(" ", html).split()))


def classify(
    status: int,
    body: bytes,
    content_type: str | None,
    *,
    expect_content: bool = True,
) -> BlockKind | None:
    """Return the block kind for a final (post-retry) response, or None if it looks normal."""
    if status in (401, 403):
        return BlockKind.FORBIDDEN
    if status == 429:
        return BlockKind.RATE_LIMITED
    is_html = content_type is None or "html" in content_type.lower()
    if not is_html:
        return None
    head = body[:200_000].decode("utf-8", errors="replace")
    if _CHALLENGE_MARKERS.search(head):
        return BlockKind.CHALLENGE
    if expect_content and 200 <= status < 300 and "<script" in head.lower():
        if visible_text_length(head) < 40:
            return BlockKind.EMPTY_SHELL
    return None
