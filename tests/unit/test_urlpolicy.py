import pytest

from scrapeline.errors import URLPolicyError
from scrapeline.fetch.urlpolicy import URLPolicy


def policy(resolved: list[str], **kw) -> URLPolicy:
    return URLPolicy.for_hosts(["example.gov"], resolver=lambda h: resolved, **kw)


def test_allows_allowlisted_public_host() -> None:
    assert (
        policy(["93.184.215.14"]).check("https://example.gov/a?b=1") == "https://example.gov/a?b=1"
    )


def test_allows_subdomain_of_allowlisted_host() -> None:
    policy(["93.184.215.14"]).check("https://docs.example.gov/x.pdf")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.gov/x", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("https://user:pw@example.gov/", "userinfo"),
        ("https://evil.test/", "not on allowlist"),
        ("https://example.gov.evil.test/", "not on allowlist"),
        ("https://notexample.gov/", "not on allowlist"),
    ],
)
def test_rejects_bad_urls(url: str, reason: str) -> None:
    with pytest.raises(URLPolicyError, match=reason):
        policy(["93.184.215.14"]).check(url)


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.1",  # private
        "172.16.0.1",  # private
        "169.254.169.254",  # cloud metadata (link-local)
        "100.64.0.1",  # CGNAT
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "::1",  # v6 loopback
        "fd00::1",  # v6 unique local
        "::ffff:127.0.0.1",  # v4-mapped loopback
    ],
)
def test_rejects_non_public_resolution(addr: str) -> None:
    with pytest.raises(URLPolicyError, match="non-public"):
        policy([addr]).check("https://example.gov/")


def test_rejects_if_any_resolved_address_is_private() -> None:
    with pytest.raises(URLPolicyError):
        policy(["93.184.215.14", "10.1.1.1"]).check("https://example.gov/")


def test_ip_literal_must_be_allowlisted_and_public() -> None:
    p = URLPolicy.for_hosts(["127.0.0.1"], resolver=lambda h: [])
    with pytest.raises(URLPolicyError, match="non-public"):
        p.check("http://127.0.0.1:8080/")


def test_private_hosts_skip_address_check_for_fixtures() -> None:
    p = URLPolicy.for_hosts(
        ["localhost"], private_hosts=["localhost"], resolver=lambda h: ["127.0.0.1"]
    )
    assert p.check("http://localhost:8080/static-list/") == "http://localhost:8080/static-list/"


def test_strips_fragment() -> None:
    assert policy(["93.184.215.14"]).check("https://example.gov/a#frag") == "https://example.gov/a"
