"""Deterministic site classifier: domain rules, page evidence, tricky and non-English sites."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from scrapeline.adapters.rawdoc import RawDoc
from scrapeline.catalog.classify import (
    MAX_EVIDENCE,
    DeterministicClassifier,
    domain_rule,
    host_country,
    normalise_language,
)
from scrapeline.catalog.sites import UNCLASSIFIED, SiteClassifier

CLASSIFIER = DeterministicClassifier()


def page(
    body: str,
    *,
    head: str = "",
    lang: str | None = "en",
    body_class: str = "",
    content_type: str | None = "text/html; charset=utf-8",
    encoding: str = "utf-8",
    status: int = 200,
) -> RawDoc:
    lang_attr = f' lang="{lang}"' if lang else ""
    content = (
        f"<!doctype html><html{lang_attr}><head>{head}</head>"
        f'<body class="{body_class}">{body}</body></html>'
    )
    data = content.encode(encoding)
    return RawDoc(
        source_id="test",
        url="https://example.test/",
        final_url="https://example.test/",
        status=status,
        content_type=content_type,
        content=data,
        sha256=RawDoc.hash_bytes(data),
        fetched_at=datetime(2026, 11, 24, tzinfo=UTC),
    )


def jsonld(data: Any) -> str:
    return f'<script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script>'


def test_it_implements_the_classifier_contract() -> None:
    classifier: SiteClassifier = CLASSIFIER
    assert classifier.classify("example.com", []).category == UNCLASSIFIED


@pytest.mark.parametrize(
    ("host", "category", "country"),
    [
        ("www.whitehouse.gov", "government", "US"),
        ("www.gov.uk", "government", "GB"),
        ("camden.gov.uk", "government", "GB"),
        ("www.service-public.gouv.fr", "government", "FR"),
        ("www.boe.gob.es", "government", "ES"),
        ("www.gob.mx", "government", "MX"),
        ("www.mhlw.go.jp", "government", "JP"),
        ("www.mois.go.kr", "government", "KR"),
        ("www.planalto.gov.br", "government", "BR"),
        ("www.govt.nz", "government", "NZ"),
        ("www.canada.ca", "government", "CA"),
        ("www.tbs-sct.gc.ca", "government", "CA"),
        ("www.quebec.gouv.qc.ca", "government", "CA"),
        ("www.oesterreich.gv.at", "government", "AT"),
        ("www.bfs.admin.ch", "government", "CH"),
        ("www.bundesregierung.bund.de", "government", "DE"),
        ("www.army.mil", "government", "US"),
        ("www.who.int", "government", None),
        ("commission.europa.eu", "government", None),
        ("www.gov.scot", "government", "GB"),
        ("ci.boston.ma.us", "government", "US"),
        ("www.portal.state.pa.us", "government", "US"),
        ("www.gov.co", "government", "CO"),
        ("www.harvard.edu", "education", "US"),
        ("www.ox.ac.uk", "education", "GB"),
        ("www.u-tokyo.ac.jp", "education", "JP"),
        ("www.unimelb.edu.au", "education", "AU"),
        ("www.usp.edu.br", "education", "BR"),
        ("lausd.k12.ca.us", "education", "US"),
        ("www.nhs.uk", "health", "GB"),
        ("www.example.bank", "finance", None),
        ("www.example.ngo", "nonprofit", None),
    ],
)
def test_public_sector_suffixes_decide_the_category(
    host: str, category: str, country: str | None
) -> None:
    result = CLASSIFIER.classify(host, [])
    assert (result.category, result.method, result.country) == (category, "rule", country)
    assert result.confidence >= 0.9
    assert result.evidence[0].signal == f"domain.{category}"


@pytest.mark.parametrize(
    "host",
    ["gov.example.com", "example.com", "governance.io", "edu-online.com", "x.ac.io", "go.co"],
)
def test_lookalike_hosts_match_no_domain_rule(host: str) -> None:
    assert domain_rule(host) is None


@pytest.mark.parametrize(
    ("host", "country"),
    [
        ("shop.example.de", "DE"),
        ("example.co.uk", "GB"),
        ("example.com.br", "BR"),
        ("example.io", None),
        ("example.co", None),
        ("example.eu", None),
        ("example.com", None),
    ],
)
def test_country_comes_from_meaningful_cctlds_only(host: str, country: str | None) -> None:
    assert host_country(host, domain_rule(host)) == country


def test_nhs_is_health_tagged_government() -> None:
    result = CLASSIFIER.classify("www.nhs.uk", [])
    assert (result.category, result.tags) == ("health", ("government",))


def test_a_government_site_with_a_shop_stays_government_and_gains_a_tag() -> None:
    home = page("<h1>Camden Council</h1><p>Pay your council tax. Public notices.</p>")
    shop = page(
        "<h1>Gift shop</h1>" + "<p>£12.50</p>" * 10 + "<button>Add to basket</button>",
        head=jsonld({"@type": "Product", "name": "Mug", "offers": {"@type": "Offer"}}),
    )
    result = CLASSIFIER.classify("shop.camden.gov.uk", [home, shop])
    assert (result.category, result.method, result.country) == ("government", "rule", "GB")
    assert "ecommerce" in result.tags
    assert result.evidence[0].value == ".gov.uk"


def test_a_shopify_store_is_ecommerce() -> None:
    shop = page(
        "<button>Add to cart</button>",
        head='<script src="https://cdn.shopify.com/s/files/1/theme.js"></script>'
        '<meta property="og:type" content="product">',
    )
    result = CLASSIFIER.classify("store.example.com", [shop])
    assert (result.category, result.method, result.platform) == ("ecommerce", "evidence", "shopify")
    assert result.confidence > 0.8
    signals = {e.signal for e in result.evidence}
    assert {"platform.ecommerce", "og_type.ecommerce", "text.ecommerce"} <= signals


def test_a_french_woocommerce_shop() -> None:
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Chaise en chêne",
        "offers": {"@type": "Offer", "price": "89.00", "priceCurrency": "EUR"},
    }
    shop = page(
        "<p>Livraison gratuite dès 50 €</p><button>Ajouter au panier</button>",
        head='<meta name="generator" content="WordPress 6.6">'
        '<meta name="generator" content="WooCommerce 9.1">' + jsonld(product),
        lang="fr-FR",
    )
    result = CLASSIFIER.classify("www.meubles-exemple.fr", [shop])
    assert (result.category, result.platform, result.language, result.country) == (
        "ecommerce",
        "woocommerce",
        "fr-FR",
        "FR",
    )


def test_a_chinese_shop_with_microdata() -> None:
    shop = page(
        '<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">茶杯</span>'
        '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">¥39</div></div>'
        "<button>加入购物车</button><span>有货</span>",
        lang="zh-cn",
    )
    result = CLASSIFIER.classify("shop.example.cn", [shop])
    assert (result.category, result.language, result.country) == ("ecommerce", "zh-CN", "CN")
    assert ("microdata.ecommerce", "Product") in {(e.signal, e.value) for e in result.evidence}


def test_a_german_news_site_and_its_authors_are_not_personal() -> None:
    article = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "Stadtrat beschließt Haushalt",
        "author": {"@type": "Person", "name": "Erika Mustermann"},
        "publisher": {"@type": "NewsMediaOrganization", "name": "Beispiel Zeitung"},
    }
    news = page("<p>Eilmeldung: Der Stadtrat tagt.</p>", head=jsonld(article), lang="de-DE")
    result = CLASSIFIER.classify("www.zeitung-beispiel.de", [news])
    assert (result.category, result.language, result.country) == ("news_media", "de-DE", "DE")
    assert "personal" not in result.tags
    assert not any(e.signal.endswith(".personal") for e in result.evidence)


def test_a_job_posting_page_is_jobs() -> None:
    posting = {
        "@type": "JobPosting",
        "title": "Data engineer",
        "hiringOrganization": {"@type": "Organization", "name": "Example Ltd"},
    }
    jobs = page("<h2>Job description</h2><a href='/apply'>Apply now</a>", head=jsonld(posting))
    result = CLASSIFIER.classify("careers.example.com", [jobs])
    assert result.category == "jobs"
    assert "business" not in result.tags


def test_hosted_job_boards_and_shops_are_known_from_the_host() -> None:
    board = CLASSIFIER.classify("boards.greenhouse.io", [])
    shop = CLASSIFIER.classify("teacups.myshopify.com", [])
    assert (board.category, board.method, board.platform) == ("jobs", "rule", "greenhouse")
    assert (shop.category, shop.method, shop.platform) == ("ecommerce", "rule", "shopify")


def test_a_spanish_town_hall_without_a_government_suffix() -> None:
    org = {"@type": "GovernmentOrganization", "name": "Ayuntamiento de Ejemplo"}
    town = page(
        "<h1>Ayuntamiento de Ejemplo</h1><p>Sede electrónica</p>", head=jsonld(org), lang="es"
    )
    result = CLASSIFIER.classify("www.ayto-ejemplo.es", [town])
    assert (result.category, result.method, result.language, result.country) == (
        "government",
        "evidence",
        "es",
        "ES",
    )


def test_a_us_city_on_dot_org_linking_to_its_legistar_calendar() -> None:
    city = page(
        "<h1>City of Example</h1><a href='https://example.legistar.com/Calendar.aspx'>"
        "Agendas and Minutes</a><p>City Council meets on Tuesdays.</p>"
    )
    result = CLASSIFIER.classify("www.cityofexample.org", [city])
    assert result.category == "government"
    assert result.platform is None  # it links to Legistar; it does not run on it
    assert ("link.government", "legistar") in {(e.signal, e.value) for e in result.evidence}


def test_a_japanese_university_in_shift_jis() -> None:
    uni = page(
        "<h1>例大学</h1><p>入学案内</p>",
        head='<meta charset="shift_jis">',
        lang="ja",
        content_type="text/html",
        encoding="shift_jis",
    )
    result = CLASSIFIER.classify("www.example.ac.jp", [uni])
    assert (result.category, result.method, result.language, result.country) == (
        "education",
        "rule",
        "ja",
        "JP",
    )
    assert ("text.education", "大学") in {(e.signal, e.value) for e in result.evidence}


def test_a_university_known_from_its_pages() -> None:
    uni = page(
        "<p>Undergraduate admissions</p>",
        head=jsonld({"@type": "CollegeOrUniversity", "name": "Example University"}),
    )
    assert CLASSIFIER.classify("www.example-university.org", [uni]).category == "education"


def test_a_property_listing_is_real_estate() -> None:
    listing = page(
        "<h1>Properties for sale</h1><p>3 bedrooms</p>",
        head=jsonld({"@type": "RealEstateListing", "name": "3 bed house"}),
    )
    assert CLASSIFIER.classify("www.homes-example.com", [listing]).category == "real_estate"


def test_a_wordpress_blog_is_personal() -> None:
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "name": "Jane's notes"},
            {"@type": "Person", "name": "Jane Doe"},
            {"@type": "BlogPosting", "headline": "Sourdough, week 3"},
        ],
    }
    blog = page(
        "<h2>About me</h2><p>Hi, I\N{RIGHT SINGLE QUOTATION MARK}m Jane.</p>",
        head='<meta name="generator" content="WordPress 6.6">'
        '<link rel="stylesheet" href="/wp-content/themes/twentytwentyfour/style.css">'
        + jsonld(graph),
    )
    result = CLASSIFIER.classify("janedoe.example.net", [blog])
    assert (result.category, result.method, result.platform) == (
        "personal",
        "evidence",
        "wordpress",
    )
    assert ("text.personal", "hi, i'm") in {(e.signal, e.value) for e in result.evidence}


def test_a_wordpress_site_with_nothing_else_names_only_its_platform() -> None:
    site = page(
        "<h1>Welcome</h1><p>Lorem ipsum.</p>",
        head='<meta name="generator" content="WordPress 6.6">',
    )
    result = CLASSIFIER.classify("lorem.example.com", [site])
    assert (result.category, result.method, result.confidence) == (UNCLASSIFIED, "none", 0.0)
    assert (result.platform, result.language) == ("wordpress", "en")


@pytest.mark.parametrize(
    ("category", "body", "head"),
    [
        (
            "health",
            "<a href='/portal'>Patient portal</a>",
            jsonld({"@type": "Hospital", "name": "St Example"}),
        ),
        (
            "finance",
            "<a href='/login'>Online banking</a>",
            jsonld({"@type": "BankOrCreditUnion", "name": "Example Bank"}),
        ),
        ("forum_social", "<p>Latest topics</p>", '<meta name="generator" content="Discourse 3.2">'),
        (
            "reference",
            "<p>From the free encyclopedia</p>",
            '<meta name="generator" content="MediaWiki 1.42">',
        ),
        (
            "travel",
            "<button>Book your stay</button>",
            jsonld({"@type": "Hotel", "name": "Hotel Example"}),
        ),
        (
            "nonprofit",
            "<p>Registered charity no. 123456</p><a href='/give'>Donate now</a>",
            jsonld({"@type": "Organization", "nonprofitStatus": "NonprofitType"}),
        ),
        (
            "business",
            "<h2>Our services</h2><h2>Case studies</h2>",
            jsonld([{"@type": "Corporation"}, {"@type": "ProfessionalService"}]),
        ),
    ],
)
def test_page_evidence_per_category(category: str, body: str, head: str) -> None:
    result = CLASSIFIER.classify("www.example.com", [page(body, head=head)])
    assert (result.category, result.method) == (category, "evidence")
    assert 0.5 <= result.confidence <= 1.0


def test_pages_that_look_like_two_things_stay_unclassified() -> None:
    mixed = page(
        "",
        head=jsonld([{"@type": "NewsArticle"}, {"@type": "Product"}]),
    )
    result = CLASSIFIER.classify("www.example.com", [mixed])
    assert (result.category, result.method) == (UNCLASSIFIED, "none")
    assert result.extra["score.news_media"] == "0.700"


def test_weak_phrases_alone_do_not_classify() -> None:
    result = CLASSIFIER.classify("www.example.com", [page("<a href='/x'>Apply now</a>")])
    assert result.category == UNCLASSIFIED
    assert result.evidence  # the signal is still reported


def test_phrases_alone_never_decide_however_many_there_are() -> None:
    local_paper = page(
        "<p>City council and the board of supervisors met; council tax rises.</p>"
        "<p>Agendas and minutes, public notices, town council, county commission.</p>"
    )
    result = CLASSIFIER.classify("www.example-gazette.com", [local_paper])
    assert result.category == UNCLASSIFIED
    assert result.extra["score.government"] == "0.450"


def test_an_article_about_woocommerce_does_not_make_a_shop() -> None:
    article = page(
        "<h1>WooCommerce vs Shopify</h1><p>Visit cdn.shopify.com for assets.</p>",
        head='<meta name="generator" content="WordPress 6.6">'
        '<link rel="canonical" href="https://blog.example.com/woocommerce-vs-shopify/">'
        '<script>dataLayer.push({"page": "/woocommerce-vs-shopify/"})</script>',
    )
    result = CLASSIFIER.classify("blog.example.com", [article])
    assert (result.category, result.platform, result.evidence) == (UNCLASSIFIED, "wordpress", ())


def test_woocommerce_is_recognised_by_its_body_class() -> None:
    shop = page("<p>Shop</p>", body_class="home woocommerce-no-js")
    result = CLASSIFIER.classify("www.example.com", [shop])
    assert (result.category, result.platform) == ("ecommerce", "woocommerce")


def test_only_links_to_the_sites_own_cart_count() -> None:
    elsewhere = page("<a href='https://other-shop.example/cart'>Buy it there</a>")
    own = page("<a href='/basket'>Basket</a><a href='https://www.example.test/checkout'>Pay</a>")
    signals = {
        (e.signal, e.value) for e in CLASSIFIER.classify("example.test", [elsewhere, own]).evidence
    }
    assert signals == {("link.ecommerce", "/basket"), ("link.ecommerce", "/checkout")}


def test_phrases_match_whole_words_only() -> None:
    result = CLASSIFIER.classify("www.example.com", [page("<p>Reapply nowhere.</p>")])
    assert not result.evidence


def test_only_successful_html_pages_count() -> None:
    api = page("Add to cart", content_type="application/json")
    blocked = page("<button>Add to cart</button>" * 3, status=403)
    result = CLASSIFIER.classify("www.example.com", [api, blocked])
    assert (result.category, result.evidence, result.language) == (UNCLASSIFIED, (), None)


def test_broken_json_ld_is_ignored() -> None:
    broken = page("", head='<script type="application/ld+json">{"@type": "Product",</script>')
    assert CLASSIFIER.classify("www.example.com", [broken]).evidence == ()


def test_the_result_does_not_depend_on_page_order() -> None:
    pages = [
        page("<button>Add to cart</button>", lang="en-GB"),
        page("", head=jsonld({"@type": "Product"}), lang="en-GB"),
        page("<p>Free shipping</p>", lang="en-US"),
    ]
    forward = CLASSIFIER.classify("shop.example.com", pages)
    assert forward == CLASSIFIER.classify("shop.example.com", list(reversed(pages)))
    assert forward.language == "en-GB"


def test_evidence_is_sorted_qualified_and_bounded() -> None:
    phrases = "add to cart|in stock|free shipping|apply now|about me|our services|campus"
    body = "".join(f"<p>{p}</p>" for p in phrases.split("|")) + "<p>$1</p>" * 10
    head = jsonld([{"@type": t} for t in ("Product", "JobPosting", "Hotel", "Dataset")])
    heavy = page(body * 5, head=head + '<meta property="og:type" content="product">')
    result = CLASSIFIER.classify("www.example.com", [heavy])
    weights = [e.weight for e in result.evidence]
    assert weights == sorted(weights, reverse=True)
    assert len(result.evidence) <= MAX_EVIDENCE
    assert all("." in e.signal for e in result.evidence)


def test_secondary_categories_become_tags() -> None:
    uni_shop = page(
        "<button>Add to basket</button>",
        head=jsonld([{"@type": "CollegeOrUniversity"}, {"@type": "Product"}]),
    )
    result = CLASSIFIER.classify("shop.example.ac.uk", [uni_shop])
    assert (result.category, result.tags) == ("education", ("ecommerce",))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("en", "en"),
        ("EN_gb", "en-GB"),
        ("zh-hant-tw", "zh-Hant-TW"),
        ("pt-BR", "pt-BR"),
        ("", None),
        (None, None),
        ("not a language", None),
    ],
)
def test_language_tags_are_normalised(value: str | None, expected: str | None) -> None:
    assert normalise_language(value) == expected


def test_language_falls_back_to_og_locale() -> None:
    p = page("", head='<meta property="og:locale" content="nl_NL">', lang=None)
    assert CLASSIFIER.classify("www.example.nl", [p]).language == "nl-NL"


@pytest.mark.parametrize("threshold", [0.0, 1.5, -1.0])
def test_thresholds_are_validated(threshold: float) -> None:
    with pytest.raises(ValueError, match="thresholds"):
        DeterministicClassifier(threshold=threshold)
