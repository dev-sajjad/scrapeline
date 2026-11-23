"""Site categories and the classifier contract. Pure, no I/O.

Every source belongs to a site (one host). A site carries one primary category from a small,
versioned taxonomy plus optional tags, with the method, confidence and evidence that decided
it. A human override (`method="human"`) is never replaced by automatic re-classification.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urlsplit

from scrapeline.adapters.rawdoc import RawDoc

TAXONOMY_VERSION = 1
UNCLASSIFIED = "unclassified"

ClassificationMethod = Literal["rule", "evidence", "llm", "human", "none"]


@dataclass(frozen=True)
class SiteCategory:
    code: str
    label: str
    description: str


SITE_CATEGORIES: tuple[SiteCategory, ...] = (
    SiteCategory("government", "Government", "Public authorities at any level, courts, agencies"),
    SiteCategory("education", "Education", "Schools, universities, academic and research bodies"),
    SiteCategory("ecommerce", "E-commerce", "Online shops and marketplaces"),
    SiteCategory("news_media", "News and media", "News outlets, magazines, broadcasters"),
    SiteCategory("personal", "Personal", "Personal sites, blogs and portfolios"),
    SiteCategory("business", "Business", "Company and organisation sites that do not sell online"),
    SiteCategory("nonprofit", "Non-profit", "Charities, NGOs, associations, foundations"),
    SiteCategory("jobs", "Jobs", "Job boards and career sites"),
    SiteCategory("real_estate", "Real estate", "Property listings and agents"),
    SiteCategory("health", "Health", "Healthcare providers and health information"),
    SiteCategory("finance", "Finance", "Banks, insurers, financial data and services"),
    SiteCategory("forum_social", "Forums and social", "Forums, communities, social platforms"),
    SiteCategory("reference", "Reference", "Encyclopaedias, documentation, open data portals"),
    SiteCategory("travel", "Travel", "Travel, transport, hospitality and events"),
    SiteCategory("other", "Other", "Classified, but fits no other category"),
    SiteCategory(UNCLASSIFIED, "Unclassified", "Not yet classified or below confidence threshold"),
)
CATEGORY_CODES = frozenset(c.code for c in SITE_CATEGORIES)


@dataclass(frozen=True)
class Evidence:
    """One signal behind a classification, e.g. ("tld", ".gov.uk", 1.0)."""

    signal: str
    value: str
    weight: float


@dataclass(frozen=True)
class Classification:
    category: str
    confidence: float
    method: ClassificationMethod
    evidence: tuple[Evidence, ...] = ()
    tags: tuple[str, ...] = ()
    platform: str | None = None
    country: str | None = None
    language: str | None = None
    taxonomy_version: int = TAXONOMY_VERSION
    extra: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category not in CATEGORY_CODES:
            raise ValueError(f"unknown site category {self.category!r}")
        unknown_tags = set(self.tags) - CATEGORY_CODES
        if unknown_tags:
            raise ValueError(f"unknown tag(s) {sorted(unknown_tags)}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


class SiteClassifier(Protocol):
    """Classifies a site from pages the worker already captured. Never fetches."""

    def classify(self, host: str, pages: Sequence[RawDoc]) -> Classification: ...


def site_host(url_or_host: str) -> str:
    """Canonical site key: lowercase host without a leading `www.` or trailing dot."""
    host = urlsplit(url_or_host).hostname if "//" in url_or_host else url_or_host
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError(f"no host in {url_or_host!r}")
    return host.removeprefix("www.")
