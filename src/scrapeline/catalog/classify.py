"""Deterministic site classifier (taxonomy v1) and the catalog writes that go with it.

Two tiers. Domain rules come first: suffixes such as .gov, .gouv.fr or .ac.jp are only
registered by public bodies or schools, so they settle the category on their own and page
evidence can only add secondary tags. Otherwise evidence from pages the worker already
captured is scored: schema.org types, og:type, platform fingerprints and a few strong phrases.
Weights combine per category as independent signals (noisy-OR), a close runner-up lowers
the confidence, and below the threshold the site stays unclassified. The classifier never
fetches, and the same host and pages always give the same result.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser
from sqlalchemy import Connection, func, or_, select, update

from scrapeline.adapters.rawdoc import RawDoc
from scrapeline.catalog.sites import (
    CATEGORY_CODES,
    UNCLASSIFIED,
    Classification,
    ClassificationMethod,
    Evidence,
    site_host,
)
from scrapeline.errors import ConfigError
from scrapeline.ops.events import Event, publish_in
from scrapeline.ops.logging import get_logger
from scrapeline.store import tables as t
from scrapeline.store.raw import RawStore

log = get_logger(__name__)

CLASSIFIER_ACTOR = "classifier:deterministic-v1"
DEFAULT_THRESHOLD = 0.5
DEFAULT_TAG_THRESHOLD = 0.35
MAX_TAGS = 3
MAX_EVIDENCE = 25

# A domain rule at or above this weight decides the category without looking at pages.
AUTHORITATIVE_WEIGHT = 0.9
# Phrases are cheap to fake and easy to over-count (a local paper writes "city council" all
# day), so together they count at most this: below the threshold, never decisive alone.
TEXT_CAP = 0.45
# Signals derived from the host name; when they carry the winning category, method is "rule".
HOST_KINDS = frozenset({"domain", "host"})


@dataclass(frozen=True)
class DomainRule:
    """A host suffix and what it says about the site.

    `pattern` is matched label by label from the right: `{cc}` is any two-letter country
    code and `*` is any label, so `gov.{cc}` matches `camden.gov.uk` and `*.myshopify.com`
    matches shop subdomains but not the vendor's own site.
    """

    pattern: str
    category: str
    weight: float
    country: str | None = None
    tags: tuple[str, ...] = ()


def _rules(category: str, weight: float, *patterns: str, **kw: Any) -> list[DomainRule]:
    return [DomainRule(p, category, weight, **kw) for p in patterns]


_ACADEMIC_CC = (
    "ae at bd be cn cr cy fj id il in ir jp ke kr lk ma mw nz rs rw th tz ug uk za zm zw"
).split()
_GO_CC = "cr id jp ke kr th tz ug".split()
_US_LOCALITY = ("ci", "co", "city", "town", "vil", "twp")

DOMAIN_RULES: tuple[DomainRule, ...] = (
    *_rules("government", 0.97, "gov", "mil", country="US"),
    *_rules("government", 0.95, "gov.{cc}", "gob.{cc}", "gouv.{cc}", "mil.{cc}"),
    *_rules("government", 0.95, *(f"go.{cc}" for cc in _GO_CC)),
    *_rules("government", 0.95, "govt.nz", "gv.at", "admin.ch", "bund.de", "gc.ca", "canada.ca"),
    *_rules("government", 0.95, "gov.*.ca", "gouv.*.ca", "fgov.be", "europa.eu", "int"),
    *_rules("government", 0.95, "parliament.uk", "police.uk", "mod.uk", "nic.in", "lg.jp"),
    *_rules("government", 0.95, "leg.br", "jus.br", "muni.il", "bel.tr", "state.*.us"),
    *_rules("government", 0.95, *(f"{label}.*.*.us" for label in _US_LOCALITY)),
    *_rules("government", 0.95, "gov.scot", "gov.wales", country="GB"),
    *_rules("health", 0.95, "nhs.uk", tags=("government",)),
    *_rules("education", 0.97, "edu", country="US"),
    *_rules("education", 0.95, "edu.{cc}", *(f"ac.{cc}" for cc in _ACADEMIC_CC)),
    *_rules("education", 0.95, "sch.uk", "ed.jp", "school.nz", "sch.id", "res.in"),
    *_rules("education", 0.95, "k12.*.us", "cc.*.us", "k12.il", "k12.tr"),
    *_rules("finance", 0.95, "bank", "insurance", "creditunion"),
    *_rules("health", 0.9, "pharmacy"),
    *_rules("nonprofit", 0.9, "ngo", "ong"),
    *_rules("nonprofit", 0.8, "charity", "asso.fr"),
    *_rules("nonprofit", 0.6, "or.jp", "or.kr"),
    *_rules("jobs", 0.6, "jobs"),
)


@dataclass(frozen=True)
class HostPlatform:
    """A hosted platform recognisable from the host name alone."""

    pattern: str
    platform: str
    category: str | None
    weight: float


HOST_PLATFORMS: tuple[HostPlatform, ...] = (
    HostPlatform("*.myshopify.com", "shopify", "ecommerce", 0.8),
    HostPlatform("*.bigcartel.com", "bigcartel", "ecommerce", 0.7),
    HostPlatform("*.legistar.com", "legistar", "government", 0.8),
    HostPlatform("*.granicus.com", "granicus", "government", 0.6),
    HostPlatform("*.primegov.com", "primegov", "government", 0.7),
    HostPlatform("*.escribemeetings.com", "escribe", "government", 0.7),
    HostPlatform("*.myworkdayjobs.com", "workday", "jobs", 0.8),
    HostPlatform("boards.greenhouse.io", "greenhouse", "jobs", 0.8),
    HostPlatform("job-boards.greenhouse.io", "greenhouse", "jobs", 0.8),
    HostPlatform("jobs.lever.co", "lever", "jobs", 0.8),
    HostPlatform("jobs.ashbyhq.com", "ashby", "jobs", 0.8),
    HostPlatform("*.blogspot.com", "blogger", "personal", 0.5),
    HostPlatform("*.wordpress.com", "wordpress", "personal", 0.3),
    HostPlatform("*.github.io", "github_pages", "personal", 0.3),
    HostPlatform("*.ghost.io", "ghost", None, 0.0),
    HostPlatform("*.wixsite.com", "wix", None, 0.0),
)

# Two-letter TLDs mostly bought for the letters, not the country (.io, .co, .ai, .me, ...).
VANITY_CCTLDS = frozenset(
    "ac ai am bz cc co fm gg gl io la ly me ms nu pw sh so st tk to tv vc ws".split()
)
# Two-letter TLDs that are not ISO 3166 country codes, or map to a different one.
_NOT_COUNTRIES = frozenset({"eu", "su"})
_CC_TO_ISO = {"uk": "GB"}


@dataclass(frozen=True)
class Fingerprint:
    """How a platform shows in a page.

    A match in `generator` (meta generator prefix), `assets` (script, stylesheet and form
    URLs), `inline` (inline script code), `classes` (html and body class or id tokens) or
    `text` (visible credits) means the site runs on the platform. `links` (anchor URLs) only
    mean it links to one, such as a council site linking to its Legistar calendar: evidence
    for the category, but not the site's own platform. Page URLs (canonical, og:url) are never
    searched, so an article about WooCommerce does not make its blog a shop.
    """

    platform: str
    category: str | None
    weight: float
    generator: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    inline: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    text: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    link_weight: float = 0.0


FINGERPRINTS: tuple[Fingerprint, ...] = (
    Fingerprint(
        "shopify",
        "ecommerce",
        0.7,
        assets=("cdn.shopify.com", "/cdn/shop/"),
        inline=("shopify.theme", "shopify.shop"),
    ),
    Fingerprint(
        "woocommerce",
        "ecommerce",
        0.7,
        generator=("woocommerce",),
        assets=("/plugins/woocommerce/",),
        classes=("woocommerce", "woocommerce-page", "woocommerce-no-js", "woocommerce-js"),
    ),
    Fingerprint(
        "magento",
        "ecommerce",
        0.7,
        generator=("magento",),
        assets=("/magento_",),
        inline=("text/x-magento-init", "mage/cookies"),
    ),
    Fingerprint("bigcommerce", "ecommerce", 0.7, assets=("bigcommerce.com/",)),
    Fingerprint(
        "prestashop", "ecommerce", 0.7, generator=("prestashop",), inline=("var prestashop",)
    ),
    Fingerprint(
        "shopware", "ecommerce", 0.6, generator=("shopware",), assets=("/bundles/storefront/",)
    ),
    Fingerprint("discourse", "forum_social", 0.7, generator=("discourse",)),
    Fingerprint(
        "phpbb", "forum_social", 0.7, assets=("/styles/prosilver/",), text=("powered by phpbb",)
    ),
    Fingerprint("xenforo", "forum_social", 0.7, assets=("/js/xf/",), inline=("xf.config",)),
    Fingerprint("vbulletin", "forum_social", 0.7, generator=("vbulletin",), assets=("vbulletin",)),
    Fingerprint("mediawiki", "reference", 0.6, generator=("mediawiki",)),
    Fingerprint("ckan", "reference", 0.6, generator=("ckan",)),
    Fingerprint("socrata", "reference", 0.5, assets=("socrata",)),
    Fingerprint("legistar", "government", 0.6, links=("legistar.com",), link_weight=0.45),
    Fingerprint(
        "granicus",
        "government",
        0.5,
        assets=("granicus.com",),
        links=("granicus.com", "govdelivery.com"),
        link_weight=0.4,
    ),
    Fingerprint(
        "civicplus",
        "government",
        0.6,
        assets=("civicplus",),
        text=("websites by civicplus",),
        links=("/agendacenter",),
        link_weight=0.4,
    ),
    Fingerprint("primegov", "government", 0.5, links=("primegov.com",), link_weight=0.4),
    Fingerprint("escribe", "government", 0.5, links=("escribemeetings.com",), link_weight=0.4),
    # Content platforms: they name the platform, not what the site is about.
    Fingerprint(
        "wordpress", None, 0.0, generator=("wordpress",), assets=("/wp-content/", "/wp-includes/")
    ),
    Fingerprint("ghost", None, 0.0, generator=("ghost",)),
    Fingerprint("drupal", None, 0.0, generator=("drupal",), assets=("/misc/drupal.js",)),
    Fingerprint("joomla", None, 0.0, generator=("joomla",)),
    Fingerprint("wix", None, 0.0, generator=("wix.com",), assets=("static.parastorage.com",)),
    Fingerprint("squarespace", None, 0.0, assets=("static1.squarespace.com",)),
    Fingerprint("hugo", None, 0.0, generator=("hugo",)),
    Fingerprint("jekyll", None, 0.0, generator=("jekyll",)),
)
# <link rel> values that load something; canonical, alternate and other page URLs do not.
ASSET_RELS = frozenset(
    "stylesheet preload modulepreload prefetch icon manifest dns-prefetch preconnect".split()
)


def _types(category: str, weight: float, names: str) -> dict[str, tuple[str, float]]:
    return dict.fromkeys(names.split(), (category, weight))


SCHEMA_TYPES: dict[str, tuple[str, float]] = {
    **_types("ecommerce", 0.6, "Product ProductGroup IndividualProduct"),
    **_types("ecommerce", 0.5, "ProductModel AggregateOffer OfferCatalog"),
    **_types("ecommerce", 0.45, "Offer"),
    **_types("ecommerce", 0.7, "OnlineStore CheckoutPage"),
    **_types("ecommerce", 0.3, "Store"),
    **_types("news_media", 0.8, "NewsMediaOrganization"),
    **_types("news_media", 0.7, "NewsArticle ReportageNewsArticle AnalysisNewsArticle"),
    **_types("news_media", 0.6, "OpinionNewsArticle BackgroundNewsArticle ReviewNewsArticle"),
    **_types("news_media", 0.6, "AskPublicNewsArticle Newspaper"),
    **_types("news_media", 0.5, "LiveBlogPosting"),
    **_types("jobs", 0.7, "JobPosting"),
    **_types("jobs", 0.5, "EmploymentAgency"),
    **_types("government", 0.7, "GovernmentOrganization"),
    **_types("government", 0.6, "GovernmentOffice CityHall Embassy"),
    **_types("government", 0.5, "GovernmentService GovernmentBuilding GovernmentPermit"),
    **_types("government", 0.5, "LegislativeBuilding Courthouse DefenceEstablishment"),
    **_types("government", 0.4, "Legislation PoliceStation FireStation"),
    **_types("education", 0.7, "CollegeOrUniversity"),
    **_types("education", 0.6, "EducationalOrganization School HighSchool MiddleSchool"),
    **_types("education", 0.6, "ElementarySchool Preschool"),
    **_types("education", 0.4, "Course CourseInstance EducationalOccupationalProgram"),
    **_types("real_estate", 0.7, "RealEstateListing"),
    **_types("real_estate", 0.6, "RealEstateAgent"),
    **_types("real_estate", 0.3, "SingleFamilyResidence House Apartment"),
    **_types("personal", 0.5, "ProfilePage"),
    **_types("personal", 0.4, "Person Blog"),
    **_types("personal", 0.35, "BlogPosting"),
    **_types("health", 0.7, "Hospital MedicalClinic"),
    **_types("health", 0.6, "MedicalOrganization Physician Dentist Pharmacy DiagnosticLab"),
    **_types("health", 0.5, "MedicalBusiness MedicalWebPage"),
    **_types("health", 0.4, "MedicalCondition Drug"),
    **_types("finance", 0.7, "BankOrCreditUnion"),
    **_types("finance", 0.6, "FinancialService InsuranceAgency"),
    **_types("finance", 0.5, "FinancialProduct LoanOrCredit BankAccount MortgageLoan"),
    **_types("finance", 0.5, "CurrencyConversionService InvestmentFund"),
    **_types("forum_social", 0.6, "DiscussionForumPosting"),
    **_types("forum_social", 0.5, "SocialMediaPosting QAPage"),
    **_types("reference", 0.6, "DataCatalog"),
    **_types("reference", 0.5, "Dataset"),
    **_types("reference", 0.4, "DataDownload APIReference DefinedTermSet"),
    **_types("reference", 0.3, "TechArticle"),
    **_types("travel", 0.6, "Hotel LodgingBusiness Hostel Motel BedAndBreakfast Resort"),
    **_types("travel", 0.6, "TravelAgency Airline"),
    **_types("travel", 0.5, "Campground TouristTrip Flight TrainTrip BusTrip BoatTrip Airport"),
    **_types("travel", 0.5, "LodgingReservation FlightReservation"),
    **_types("travel", 0.4, "TouristAttraction TouristDestination"),
    **_types("nonprofit", 0.7, "NGO"),
    **_types("business", 0.4, "ProfessionalService AccountingService LegalService"),
    **_types("business", 0.35, "Corporation LocalBusiness HomeAndConstructionBusiness"),
    **_types("business", 0.15, "Organization"),
}

# Nested schema.org objects count only when they are listed items. Authors, publishers,
# employers and sellers (Person, Organization, ...) nested in them say nothing about the site.
NESTED_ITEM_TYPES = frozenset(
    "Product ProductGroup IndividualProduct JobPosting NewsArticle ReportageNewsArticle "
    "AnalysisNewsArticle OpinionNewsArticle RealEstateListing SingleFamilyResidence House "
    "Apartment Hotel LodgingBusiness Hostel DiscussionForumPosting SocialMediaPosting Dataset "
    "Course BlogPosting TouristTrip Flight".split()
)
# Properties that name the people or bodies behind a piece of content, and the ones that
# name who stands behind the site itself. A `@graph` member referenced only through the
# first kind counts as nested, like an inline author.
ATTRIBUTION_KEYS = frozenset(
    "author creator contributor editor employee founder reviewedBy translator".split()
)
IDENTITY_KEYS = frozenset({"publisher"})

OG_TYPES: dict[str, tuple[str, float]] = {
    "product": ("ecommerce", 0.5),
    "product.item": ("ecommerce", 0.5),
    "product.group": ("ecommerce", 0.5),
    "og:product": ("ecommerce", 0.5),
    "profile": ("personal", 0.4),
    "business.business": ("business", 0.3),
    "restaurant.restaurant": ("travel", 0.3),
    "video.movie": ("news_media", 0.25),
    "video.episode": ("news_media", 0.25),
    "video.tv_show": ("news_media", 0.25),
}
COMMERCE_META = frozenset(
    {"product:price:amount", "og:price:amount", "product:availability", "product:retailer_item_id"}
)


def _phrases(category: str, weight: float, phrases: str) -> list[tuple[str, str, float]]:
    return [(p.strip(), category, weight) for p in phrases.split("|")]


PHRASES: tuple[tuple[str, str, float], ...] = (
    *_phrases(
        "ecommerce",
        0.35,
        "add to cart|add to basket|add to bag|add to trolley|ajouter au panier|"
        "in den warenkorb|añadir al carrito|agregar al carrito|aggiungi al carrello|"
        "adicionar ao carrinho|in winkelwagen|toevoegen aan winkelwagen|dodaj do koszyka|"
        "lägg i varukorgen|læg i kurv|legg i handlekurv|sepete ekle|в корзину|"
        "カートに入れる|加入购物车|加入購物車|장바구니 담기",
    ),
    *_phrases(
        "ecommerce",
        0.25,
        "proceed to checkout|passer la commande|zur kasse|finalizar compra|vai alla cassa",
    ),
    *_phrases(
        "ecommerce",
        0.15,
        "in stock|out of stock|free shipping|free delivery|en stock|auf lager|"
        "livraison gratuite|kostenloser versand|envío gratis|在庫あり|有货",
    ),
    *_phrases(
        "jobs",
        0.3,
        "job description|job openings|jetzt bewerben|stellenangebote|offres d'emploi|"
        "ofertas de empleo|vagas de emprego|求人情報|招聘职位|채용 공고",
    ),
    *_phrases("jobs", 0.2, "apply now|years of experience|postuler|job type"),
    *_phrases(
        "news_media",
        0.3,
        "breaking news|eilmeldung|última hora|dernière minute|速報|突发新闻",
    ),
    *_phrases("news_media", 0.2, "latest news|editor's picks|op-ed|à la une|ultime notizie"),
    *_phrases("government", 0.6, "an official website of the united states government"),
    *_phrases(
        "government",
        0.4,
        "ayuntamiento de|municipalidad de|prefeitura municipal|市役所|区役所|"
        "agendas and minutes|agendas & minutes",
    ),
    *_phrases(
        "government",
        0.3,
        "city council|town council|county commission|board of supervisors|council tax|"
        "mairie de|conseil municipal|gemeinderat|stadtverwaltung|comune di|gemeente|人民政府",
    ),
    *_phrases("government", 0.2, "public notices"),
    *_phrases(
        "education",
        0.3,
        "undergraduate|postgraduate|school district|université|universität|universidad|"
        "università|universiteit|hochschule|大学|대학교",
    ),
    *_phrases("education", 0.2, "admissions|tuition|faculty of|university|campus"),
    *_phrases(
        "real_estate",
        0.35,
        "properties for sale|property for sale|homes for sale|estate agents|immobilier|"
        "immobilien|inmobiliaria|imóveis|不動産|房产",
    ),
    *_phrases("real_estate", 0.15, "for rent|to let|bedrooms|sq ft|real estate"),
    *_phrases(
        "health",
        0.4,
        "patient portal|patients and visitors|find a doctor|emergency department|"
        "krankenhaus|hôpital|病院|医院",
    ),
    *_phrases("health", 0.15, "book an appointment|symptoms|hospital|clinic"),
    *_phrases(
        "finance",
        0.4,
        "online banking|internet banking|banque en ligne|online-banking|banca online|"
        "ネットバンキング|网上银行",
    ),
    *_phrases("finance", 0.25, "open an account|savings account|insurance quote|mortgage rates"),
    *_phrases("forum_social", 0.35, "forum index|members online|last post|new topic"),
    *_phrases("reference", 0.3, "open data portal|open data|api reference|encyclopedia"),
    *_phrases("reference", 0.15, "datasets|documentation"),
    *_phrases("travel", 0.3, "boarding pass|check availability|book your stay"),
    *_phrases("travel", 0.15, "check-in|flights|hotels|itinerary|book now"),
    *_phrases(
        "nonprofit",
        0.45,
        "registered charity|501(c)(3)|association loi 1901|organización sin fines de lucro",
    ),
    *_phrases(
        "nonprofit",
        0.35,
        "charity number|non-profit organization|nonprofit organization|not-for-profit|"
        "gemeinnützig|donate now",
    ),
    *_phrases(
        "personal",
        0.3,
        "about me|my portfolio|hi, i'm|hello, i'm|à propos de moi|über mich|sobre mí",
    ),
    *_phrases("personal", 0.25, "my blog|welcome to my"),
    *_phrases(
        "business",
        0.2,
        "our services|our clients|case studies|request a quote|contact sales|nos services|"
        "unsere leistungen|nuestros servicios",
    ),
    *_phrases("business", 0.1, "about us|our team"),
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return 0x3040 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF or 0xFF00 <= code <= 0xFFEF


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    # CJK text has no spaces between words, so word boundaries apply to other scripts only.
    if any(_is_cjk(ch) for ch in phrase):
        return re.compile(re.escape(phrase))
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")


_PHRASE_PATTERNS = tuple((_phrase_pattern(p), p, c, w) for p, c, w in PHRASES)
_CART_PATH = re.compile(
    r"/(cart|basket|checkout|warenkorb|panier|carrito|carrello|winkelwagen|koszyk|sepet)"
    r"(?:[/?#.]|$)"
)
# Linear time on untrusted text: a trailing-currency number starts only where a run of
# digits, dots and commas starts, and the run is possessive, so a long run of digits
# with no currency after it fails once instead of backtracking from every position in it.
_PRICE = re.compile(
    r"(?:[£$€¥₹₩₽₺]\s?\d[\d.,]*+"
    r"|(?<![\d.,])\d[\d.,]*+\s?(?:€|zł|kr|kč|₽|₺|usd|eur|gbp|chf)(?!\w))"
)
MIN_PRICES = 8
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([a-zA-Z0-9_.:-]+)""", re.I)
_LANG = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_SCHEMA_NAME = re.compile(r"^[A-Za-z]+$")
_MAX_JSON_NODES = 5000
_MAX_TEXT = 500_000
_APOSTROPHES = {0x2019: "'"}  # typographic apostrophes match the plain ones in PHRASES


@dataclass(frozen=True)
class Signal:
    """One observation pointing at a category. Becomes an Evidence row `<kind>.<category>`."""

    kind: str
    category: str
    value: str
    weight: float

    def evidence(self) -> Evidence:
        return Evidence(f"{self.kind}.{self.category}", self.value, self.weight)


@dataclass(frozen=True)
class PageFacts:
    signals: tuple[Signal, ...]
    platforms: tuple[tuple[str, float], ...]
    language: str | None


def _host_matches(pattern: str, labels: Sequence[str]) -> bool:
    parts = pattern.split(".")
    if len(labels) < len(parts):
        return False
    for part, label in zip(reversed(parts), reversed(labels), strict=False):
        if part == "{cc}":
            if len(label) != 2 or not label.isalpha():
                return False
        elif part != "*" and part != label:
            return False
    return True


def _country_code(tld: str) -> str | None:
    if len(tld) != 2 or not tld.isalpha() or tld in _NOT_COUNTRIES:
        return None
    return _CC_TO_ISO.get(tld, tld.upper())


def domain_rule(host: str) -> DomainRule | None:
    """The most specific domain rule for a host (longest pattern, then highest weight)."""
    labels = host.split(".")
    matches = [r for r in DOMAIN_RULES if _host_matches(r.pattern, labels)]
    if not matches:
        return None
    return max(matches, key=lambda r: (r.pattern.count(".") + 1, r.weight, r.pattern))


def host_country(host: str, rule: DomainRule | None = None) -> str | None:
    """ISO country from the ccTLD; vanity ccTLDs count only under a public-sector suffix."""
    if rule is not None and rule.country is not None:
        return rule.country
    tld = host.rsplit(".", 1)[-1]
    if rule is None and tld in VANITY_CCTLDS:
        return None
    return _country_code(tld)


def host_platform(host: str) -> HostPlatform | None:
    labels = host.split(".")
    return next((p for p in HOST_PLATFORMS if _host_matches(p.pattern, labels)), None)


def _decode(page: RawDoc) -> str:
    charset = None
    if page.content_type and "charset=" in page.content_type.lower():
        charset = page.content_type.lower().split("charset=", 1)[1].split(";")[0].strip(" \"'")
    if not charset and (match := _META_CHARSET.search(page.content[:4096])):
        charset = match.group(1).decode("ascii", "ignore")
    try:
        return page.content.decode(charset or "utf-8", errors="replace")
    except (LookupError, ValueError):
        # The page names its own charset. Unknown names, binary codecs ('base64') and codecs
        # that fail on decode ('undefined', 'idna', 'punycode') all fall back to UTF-8.
        return page.content.decode("utf-8", errors="replace")


def _page_host(page: RawDoc) -> str | None:
    try:
        return site_host(page.final_url)
    except ValueError:
        return None


def _is_html(page: RawDoc) -> bool:
    if not 200 <= page.status < 300:
        return False
    if page.content_type:
        return "html" in page.content_type.lower()
    return page.content.lstrip()[:1] == b"<"


def normalise_language(value: str | None) -> str | None:
    """`en_gb`, `EN-gb` -> `en-GB`; anything that is not a language tag -> None."""
    if not value:
        return None
    parts = value.strip().replace("_", "-").split("-")
    tag = "-".join(
        [parts[0].lower()]
        + [p.upper() if len(p) == 2 else p.title() if len(p) == 4 else p for p in parts[1:]]
    )
    return tag if _LANG.match(tag) and len(tag) <= 35 else None


def _schema_name(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    name = re.split(r"[/:#]", raw.strip())[-1]
    return name if _SCHEMA_NAME.match(name) else None


def _jsonld_dicts(data: object) -> Iterator[tuple[dict[str, Any], int]]:
    """Every object in a JSON-LD document with its depth; `@graph` members keep their parent's."""
    stack: list[tuple[object, int]] = [(data, 0)]
    visited = 0
    while stack and visited < _MAX_JSON_NODES:
        node, depth = stack.pop()
        visited += 1
        if isinstance(node, list):
            stack.extend((child, depth) for child in node)
        elif isinstance(node, dict):
            yield node, depth
            for key, value in node.items():
                if isinstance(value, dict | list):
                    stack.append((value, depth if key == "@graph" else depth + 1))


def _referenced_ids(data: object, keys: frozenset[str]) -> set[str]:
    """`@id`s that objects point to through one of `keys`, as in `"author": {"@id": ...}`."""
    ids: set[str] = set()
    for node, _ in _jsonld_dicts(data):
        for key in keys.intersection(node):
            value = node[key]
            for ref in value if isinstance(value, list) else [value]:
                if isinstance(ref, dict) and isinstance(ref.get("@id"), str):
                    ids.add(ref["@id"])
    return ids


def _jsonld_types(data: object) -> Iterator[tuple[str, bool]]:
    """(schema.org type, is top level) for every typed object; `@graph` members are top level.

    A `@graph` member that other objects only name as an author, editor, employee, ... is
    nested where it is referenced, the way Yoast and similar plugins write article authors,
    so it is not top level. It stays top level when something also names it as publisher:
    then it is the site's own identity, as on a personal blog.
    `nonprofitStatus` is reported as the pseudo type NGO, which is what it declares.
    """
    attributed = _referenced_ids(data, ATTRIBUTION_KEYS) - _referenced_ids(data, IDENTITY_KEYS)
    for node, depth in _jsonld_dicts(data):
        node_id = node.get("@id")
        top_level = depth == 0 and not (isinstance(node_id, str) and node_id in attributed)
        declared = node.get("@type")
        for raw in declared if isinstance(declared, list) else [declared]:
            if (name := _schema_name(raw)) is not None:
                yield name, top_level
        if "nonprofitStatus" in node:
            yield "NGO", top_level


def _schema_signals(types: Iterable[tuple[str, bool]], kind: str) -> Iterator[Signal]:
    for name, top_level in types:
        if name in SCHEMA_TYPES and (top_level or name in NESTED_ITEM_TYPES):
            category, weight = SCHEMA_TYPES[name]
            yield Signal(kind, category, name, weight)


@dataclass
class _Markup:
    """The parts of a page fingerprints look at, lowercased."""

    generators: list[str]
    assets: list[str]
    inline: list[str]
    classes: set[str]
    hrefs: list[str]


def _meta_signals(tree: LexborHTMLParser, markup: _Markup) -> tuple[list[Signal], str | None]:
    """og:type and product meta signals, generators (into `markup`) and the language."""
    root = tree.css_first("html")
    root_attrs = root.attributes if root is not None else {}
    language = normalise_language(root_attrs.get("lang") or root_attrs.get("xml:lang"))
    signals: list[Signal] = []
    for meta in tree.css("meta"):
        attrs = meta.attributes
        name = (attrs.get("name") or attrs.get("property") or "").strip().lower()
        content = (attrs.get("content") or "").strip()
        if name == "generator":
            markup.generators.append(content.lower())
        elif name == "og:type" and content.lower() in OG_TYPES:
            category, weight = OG_TYPES[content.lower()]
            signals.append(Signal("og_type", category, content.lower(), weight))
        elif name in COMMERCE_META and content:
            signals.append(Signal("meta", "ecommerce", name, 0.5))
        elif name == "og:locale" and language is None:
            language = normalise_language(content)
        if (attrs.get("http-equiv") or "").lower() == "content-language" and language is None:
            language = normalise_language(content.split(",")[0])
    return signals, language


def _collect_markup(tree: LexborHTMLParser) -> tuple[_Markup, list[Signal]]:
    """Asset URLs, inline scripts, root classes and anchors; schema.org signals on the way."""
    markup = _Markup([], [], [], set(), [])
    signals: list[Signal] = []
    for node in (tree.css_first("html"), tree.body):
        if node is not None:
            tokens = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
            markup.classes.update(tokens.lower().split())
    for script in tree.css("script"):
        script_type = (script.attributes.get("type") or "").lower()
        if src := script.attributes.get("src"):
            markup.assets.append(src.lower())
        elif script_type == "application/ld+json":
            try:
                data = json.loads(script.text())
            except (ValueError, RecursionError):
                continue
            signals.extend(_schema_signals(_jsonld_types(data), "schema_org"))
        else:
            markup.inline.append(f"{script_type}\n{script.text()[:20_000]}".lower())
    for link in tree.css("link[href]"):
        if ASSET_RELS & set((link.attributes.get("rel") or "").lower().split()):
            markup.assets.append((link.attributes.get("href") or "").lower())
    for form in tree.css("form[action]"):
        markup.assets.append((form.attributes.get("action") or "").lower())
    markup.hrefs = [(a.attributes.get("href") or "").strip().lower() for a in tree.css("a[href]")]
    for node in tree.css("[itemtype]"):
        top_level = "itemprop" not in node.attributes
        for url in (node.attributes.get("itemtype") or "").split():
            if "schema.org" in url and (type_name := _schema_name(url.rstrip("/"))) is not None:
                signals.extend(_schema_signals([(type_name, top_level)], "microdata"))
    return markup, signals


def _visible_text(tree: LexborHTMLParser) -> str:
    """Casefolded visible text plus button labels. Strips the tree's scripts and styles."""
    tree.strip_tags(["script", "style", "noscript", "template", "svg"])
    root = tree.body if tree.body is not None else tree.root
    parts = [root.text(separator=" ") if root is not None else ""]
    parts += [
        n.attributes.get("value") or "" for n in tree.css("input[type=submit], input[type=button]")
    ]
    text = re.sub(r"\s+", " ", " ".join(parts).casefold().translate(_APOSTROPHES))
    return text[:_MAX_TEXT]


def _cart_links(hrefs: Iterable[str], page_host: str | None) -> Iterator[Signal]:
    """Links to this site's own cart or checkout; a link to another shop says nothing."""
    for href in hrefs:
        try:
            parts = urlsplit(href)
            target = site_host(parts.hostname) if parts.hostname else page_host
        except ValueError:  # malformed href, e.g. an unclosed IPv6 bracket
            continue
        if target == page_host and (match := _CART_PATH.search("/" + parts.path.lstrip("/"))):
            yield Signal("link", "ecommerce", f"/{match.group(1)}", 0.3)


def _platform_signals(markup: _Markup, text: str) -> tuple[list[Signal], list[tuple[str, float]]]:
    signals: list[Signal] = []
    platforms: list[tuple[str, float]] = []
    for fp in FINGERPRINTS:
        runs_on = (
            any(g.startswith(p) for g in markup.generators for p in fp.generator)
            or any(m in a for a in markup.assets for m in fp.assets)
            or any(m in code for code in markup.inline for m in fp.inline)
            or not markup.classes.isdisjoint(fp.classes)
            or any(p in text for p in fp.text)
        )
        if runs_on:
            platforms.append((fp.platform, fp.weight))
            if fp.category is not None:
                signals.append(Signal("platform", fp.category, fp.platform, fp.weight))
        elif fp.category is not None and any(m in h for h in markup.hrefs for m in fp.links):
            signals.append(Signal("link", fp.category, fp.platform, fp.link_weight))
    return signals, platforms


def page_facts(html: str, page_host: str | None = None) -> PageFacts:
    """Category signals, platforms and language from one HTML page. Pure.

    `page_host` is the site the page belongs to; relative links always belong to it.
    """
    tree = LexborHTMLParser(html)
    markup, signals = _collect_markup(tree)
    meta_signals, language = _meta_signals(tree, markup)
    signals += meta_signals
    signals += _cart_links(markup.hrefs, page_host)
    text = _visible_text(tree)
    platform_signals, platforms = _platform_signals(markup, text)
    signals += platform_signals
    signals += [
        Signal("text", category, phrase, weight)
        for pattern, phrase, category, weight in _PHRASE_PATTERNS
        if pattern.search(text)
    ]
    if len(_PRICE.findall(text)) >= MIN_PRICES:
        signals.append(Signal("prices", "ecommerce", f"{MIN_PRICES}+ prices", 0.25))
    return PageFacts(tuple(signals), tuple(platforms), language)


def _noisy_or(weights: Iterable[float]) -> float:
    return 1.0 - math.prod(1.0 - w for w in weights)


def category_scores(signals: Iterable[Signal]) -> dict[str, float]:
    """Combine signals per category; phrases together count at most TEXT_CAP."""
    text: dict[str, list[float]] = {}
    other: dict[str, list[float]] = {}
    for s in signals:
        (text if s.kind == "text" else other).setdefault(s.category, []).append(s.weight)
    scores: dict[str, float] = {}
    for category in sorted(text.keys() | other.keys()):
        weights = list(other.get(category, []))
        if category in text:
            weights.append(min(TEXT_CAP, _noisy_or(text[category])))
        scores[category] = _noisy_or(weights)
    return scores


def _dedupe(signals: Iterable[Signal]) -> list[Signal]:
    """One signal per (kind, category, value), at its highest weight, in a stable order."""
    best: dict[tuple[str, str, str], Signal] = {}
    for s in signals:
        key = (s.kind, s.category, s.value)
        if key not in best or s.weight > best[key].weight:
            best[key] = s
    return sorted(best.values(), key=lambda s: (-s.weight, s.kind, s.category, s.value))


class DeterministicClassifier:
    """Rule- and evidence-based `SiteClassifier`. Pure: no network, no database, no clock."""

    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        tag_threshold: float = DEFAULT_TAG_THRESHOLD,
    ) -> None:
        if not 0.0 < threshold <= 1.0 or not 0.0 < tag_threshold <= 1.0:
            raise ValueError("thresholds must be in (0, 1]")
        self.threshold = threshold
        self.tag_threshold = tag_threshold

    def classify(self, host: str, pages: Sequence[RawDoc]) -> Classification:
        host = site_host(host)
        rule = domain_rule(host)
        hosted = host_platform(host)
        signals: list[Signal] = []
        platforms: list[tuple[str, float]] = []
        languages: Counter[str] = Counter()
        if rule is not None:
            suffix = ".".join(host.split(".")[-(rule.pattern.count(".") + 1) :])
            signals.append(Signal("domain", rule.category, f".{suffix}", rule.weight))
        if hosted is not None:
            platforms.append((hosted.platform, hosted.weight))
            if hosted.category is not None:
                signals.append(Signal("host", hosted.category, hosted.platform, hosted.weight))
        for page in pages:
            if _is_html(page):
                facts = page_facts(_decode(page), _page_host(page) or host)
                signals.extend(facts.signals)
                platforms.extend(facts.platforms)
                if facts.language:
                    languages[facts.language] += 1

        signals = _dedupe(signals)
        scores = category_scores(signals)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        common: dict[str, Any] = {
            "evidence": tuple(s.evidence() for s in signals[:MAX_EVIDENCE]),
            # The most specific platform wins: WooCommerce over the WordPress it runs on.
            "platform": min(platforms, key=lambda p: (-p[1], p[0]))[0] if platforms else None,
            "country": host_country(host, rule),
            "language": min(languages, key=lambda k: (-languages[k], k)) if languages else None,
            "extra": {f"score.{c}": f"{s:.3f}" for c, s in ranked},
        }
        decided = self._decide(rule, ranked, signals)
        if decided is None:
            return Classification(UNCLASSIFIED, 0.0, "none", **common)
        category, confidence, method = decided
        tags = [c for c, s in ranked if c != category and s >= self.tag_threshold]
        tags += [c for c in (rule.tags if rule else ()) if c not in tags and c != category]
        return Classification(
            category, round(confidence, 3), method, tags=tuple(tags[:MAX_TAGS]), **common
        )

    def _decide(
        self,
        rule: DomainRule | None,
        ranked: list[tuple[str, float]],
        signals: list[Signal],
    ) -> tuple[str, float, ClassificationMethod] | None:
        """(category, confidence, method), or None when nothing clears the threshold."""
        if rule is not None and rule.weight >= AUTHORITATIVE_WEIGHT:
            return rule.category, dict(ranked)[rule.category], "rule"
        if not ranked:
            return None
        category, best = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        # A site that looks almost as much like something else is an uncertain call.
        confidence = best * (1.0 - runner_up / 2)
        if confidence < self.threshold:
            return None
        strongest = next(s for s in signals if s.category == category)
        return category, confidence, "rule" if strongest.kind in HOST_KINDS else "evidence"


def stored_pages(
    conn: Connection, site_id: int, raw_store: RawStore, *, per_source: int = 5
) -> list[RawDoc]:
    """The latest captured HTML pages of every source on a site, read from the raw store.

    Never fetches. A raw object that is missing or fails its hash check is skipped.
    """
    source_ids = conn.execute(
        select(t.sources.c.id).where(t.sources.c.site_id == site_id).order_by(t.sources.c.id)
    ).scalars()
    pages: list[RawDoc] = []
    for source_id in source_ids:
        rows = conn.execute(
            select(t.fetch_log)
            .where(
                t.fetch_log.c.source_id == source_id,
                t.fetch_log.c.raw_key.is_not(None),
                t.fetch_log.c.status.between(200, 299),
                or_(
                    t.fetch_log.c.content_type.ilike("%html%"),
                    t.fetch_log.c.raw_key.like("%.html"),
                ),
            )
            .order_by(t.fetch_log.c.fetched_at.desc(), t.fetch_log.c.id.desc())
            .limit(per_source * 10)
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            if len(seen) >= per_source:
                break
            if row.raw_sha256 in seen:
                continue
            try:
                content = raw_store.get(row.raw_key)
            except (OSError, ValueError) as exc:
                log.warning("classify.raw_unreadable", raw_key=row.raw_key, error=repr(exc))
                continue
            seen.add(row.raw_sha256)
            pages.append(
                RawDoc(
                    source_id=source_id,
                    url=row.url,
                    final_url=row.final_url or row.url,
                    status=row.status,
                    content_type=row.content_type,
                    content=content,
                    sha256=row.raw_sha256,
                    fetched_at=row.fetched_at,
                    kind=row.kind or "page",
                    meta=row.meta or {},
                )
            )
    return pages


ApplyOutcome = Literal["changed", "refreshed", "locked"]


def _evidence_json(evidence: Iterable[Evidence]) -> list[dict[str, Any]]:
    return [asdict(e) for e in evidence]


def _write(conn: Connection, site: Any, c: Classification, actor: str, *, always_log: bool) -> bool:
    """Store a classification on a locked site row; log it when something material changed."""
    changed = (
        always_log
        or site.category != c.category
        or site.category_method != c.method
        or set(site.tags or []) != set(c.tags)
    )
    evidence = _evidence_json(c.evidence)
    conn.execute(
        update(t.site)
        .where(t.site.c.id == site.id)
        .values(
            category=c.category,
            category_confidence=c.confidence,
            category_method=c.method,
            category_evidence=evidence,
            tags=list(c.tags),
            # Facts missing from this round's pages do not erase what an earlier round saw.
            country=c.country or site.country,
            language=c.language or site.language,
            platform=c.platform or site.platform,
            classified_at=func.now(),
            updated_at=func.now(),
        )
    )
    if not changed:
        return False
    conn.execute(
        t.classification_log.insert().values(
            site_id=site.id,
            old_category=site.category,
            new_category=c.category,
            confidence=c.confidence,
            method=c.method,
            # The log has no tags column; the tags travel as one extra evidence entry.
            evidence=[*evidence, {"signal": "tags", "value": ",".join(c.tags), "weight": 0.0}],
            taxonomy_version=c.taxonomy_version,
            actor=actor,
        )
    )
    publish_in(
        conn,
        Event(
            kind="site.classified",
            message=f"{site.host}: {site.category} -> {c.category} ({c.method})",
            data={
                "site_id": site.id,
                "host": site.host,
                "old_category": site.category,
                "category": c.category,
                "method": c.method,
                "confidence": c.confidence,
                "tags": list(c.tags),
                "actor": actor,
            },
        ),
    )
    return True


def _locked_site(conn: Connection, condition: Any) -> Any:
    return conn.execute(select(t.site).where(condition).with_for_update()).one_or_none()


def apply_classification(
    conn: Connection, site_id: int, classification: Classification, actor: str
) -> ApplyOutcome:
    """Store an automatic classification on a site.

    The row is refreshed every time (confidence, evidence, platform, ...); a log row is
    appended only when the category, tags or method change. A site a human classified keeps
    its category: automatic results are dropped and "locked" is returned.
    """
    site = _locked_site(conn, t.site.c.id == site_id)
    if site is None:
        raise ConfigError(f"unknown site id {site_id}")
    if site.category_method == "human" and classification.method != "human":
        return "locked"
    return "changed" if _write(conn, site, classification, actor, always_log=False) else "refreshed"


def override_category(conn: Connection, host: str, category: str, actor: str, note: str) -> int:
    """A human decision: set the category with method human and confidence 1.0, and log it.

    The note is kept as the evidence. Returns the site id.
    """
    if category not in CATEGORY_CODES or category == UNCLASSIFIED:
        valid = sorted(CATEGORY_CODES - {UNCLASSIFIED})
        raise ConfigError(f"unknown category {category!r}; use one of {', '.join(valid)}")
    if not note.strip():
        raise ConfigError("an override needs a note saying why")
    try:
        canonical = site_host(host)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    site = _locked_site(conn, t.site.c.host == canonical)
    if site is None:
        raise ConfigError(f"unknown site {host!r}; register or sync a source on it first")
    classification = Classification(
        category,
        1.0,
        "human",
        evidence=(Evidence("human", note.strip(), 1.0),),
        tags=tuple(tag for tag in site.tags or [] if tag != category),
        platform=site.platform,
        country=site.country,
        language=site.language,
    )
    _write(conn, site, classification, actor, always_log=True)
    site_id: int = site.id
    return site_id
