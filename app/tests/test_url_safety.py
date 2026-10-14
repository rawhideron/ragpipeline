import socket

import httpx
import pytest
from fastapi import HTTPException

from src import main

PUBLIC_IPV4 = "93.184.215.14"


def resolve_to(monkeypatch, *ips: str) -> None:
    def fake_getaddrinfo(host, port):
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in ips
        ]

    monkeypatch.setattr(main.socket, "getaddrinfo", fake_getaddrinfo)


def serve(monkeypatch, handler) -> None:
    monkeypatch.setattr(main, "http_client", httpx.Client(transport=httpx.MockTransport(handler)))


def expect_http_error(status_code: int, call, *args):
    with pytest.raises(HTTPException) as exc_info:
        call(*args)
    assert exc_info.value.status_code == status_code
    return exc_info.value.detail


# validate_http_url: scheme and shape


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.com/page",
        "/relative/path",
        "http://",
        "",
    ],
)
def test_rejects_non_http_or_relative_urls(monkeypatch, url):
    resolve_to(monkeypatch, PUBLIC_IPV4)

    detail = expect_http_error(400, main.validate_http_url, url)

    assert detail == "URL must be an absolute http or https URL"


@pytest.mark.parametrize("url", ["http://example.com/page", "https://example.com/page"])
def test_accepts_public_http_and_https_urls(monkeypatch, url):
    resolve_to(monkeypatch, PUBLIC_IPV4)

    assert main.validate_http_url(url) == url


def test_strips_surrounding_whitespace(monkeypatch):
    resolve_to(monkeypatch, PUBLIC_IPV4)

    assert main.validate_http_url("  https://example.com/page \n") == "https://example.com/page"


def test_accepts_public_ipv6_address(monkeypatch):
    resolve_to(monkeypatch, "2606:4700::1111")

    assert main.validate_http_url("https://example.com/") == "https://example.com/"


# validate_http_url: private address blocking (SSRF)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC 1918
        "172.16.0.1",  # RFC 1918
        "192.168.1.230",  # RFC 1918, the homelab LAN
        "169.254.169.254",  # link-local, cloud metadata endpoint
        "0.0.0.0",  # unspecified, reaches localhost on Linux
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "::1",  # IPv6 loopback
        "::",  # IPv6 unspecified
        "fd00::1",  # IPv6 unique local
        "fe80::1",  # IPv6 link-local
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:192.168.1.230",  # IPv4-mapped LAN
    ],
)
def test_blocks_non_public_addresses(monkeypatch, ip):
    resolve_to(monkeypatch, ip)

    detail = expect_http_error(400, main.validate_http_url, "http://internal.example/")

    assert detail == "Private URL ingestion is disabled"


def test_blocks_when_any_resolved_address_is_private(monkeypatch):
    resolve_to(monkeypatch, PUBLIC_IPV4, "10.0.0.5")

    expect_http_error(400, main.validate_http_url, "http://mixed.example/")


def test_rejects_unresolvable_hostname(monkeypatch):
    def fail(host, port):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(main.socket, "getaddrinfo", fail)

    detail = expect_http_error(400, main.validate_http_url, "http://does-not-exist.example/")

    assert detail == "Could not resolve URL hostname"


def test_private_addresses_allowed_when_enabled(monkeypatch):
    monkeypatch.setattr(main, "ALLOW_PRIVATE_URL_INGEST", True)

    def must_not_resolve(host, port):
        raise AssertionError("hostname should not be resolved when private ingest is allowed")

    monkeypatch.setattr(main.socket, "getaddrinfo", must_not_resolve)

    assert main.validate_http_url("http://192.168.1.230/") == "http://192.168.1.230/"


@pytest.mark.xfail(strict=True, reason="Known gap: 100.64.0.0/10 (CGNAT, Tailscale) is not blocked")
def test_blocks_shared_address_space(monkeypatch):
    resolve_to(monkeypatch, "100.64.0.1")

    expect_http_error(400, main.validate_http_url, "http://cgnat.example/")


@pytest.mark.xfail(strict=True, reason="Known gap: redirects are followed without re-checking the target address")
def test_fetch_does_not_follow_redirect_to_private_address(monkeypatch):
    def fake_getaddrinfo(host, port):
        ip = PUBLIC_IPV4 if host == "public.example" else "192.168.1.230"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(main.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://192.168.1.230/secret"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="internal secret")

    serve(monkeypatch, handler)

    url = main.validate_http_url("http://public.example/page")
    expect_http_error(400, main.fetch_url_text, url)


# assert_robots_allowed

ROBOTS_ALL_AGENTS = "User-agent: *\nDisallow: /private/\n"
ROBOTS_RAGPIPELINE_GROUP = "User-agent: *\nDisallow: /private/\n\nUser-agent: ragpipeline\nDisallow: /no-rag/\n"


def serve_robots(monkeypatch, response: httpx.Response) -> list[httpx.Request]:
    requests = []

    def handler(request):
        requests.append(request)
        return response

    serve(monkeypatch, handler)
    return requests


def test_robots_allows_permitted_path(monkeypatch):
    requests = serve_robots(monkeypatch, httpx.Response(200, text=ROBOTS_ALL_AGENTS))

    main.assert_robots_allowed("https://example.com/docs/page.html")

    assert str(requests[0].url) == "https://example.com/robots.txt"
    assert requests[0].headers["user-agent"] == main.INGEST_USER_AGENT


@pytest.mark.parametrize(
    ("robots", "path"),
    [
        (ROBOTS_ALL_AGENTS, "/private/page"),
        (ROBOTS_RAGPIPELINE_GROUP, "/no-rag/page"),
    ],
)
def test_robots_disallowed_path_is_forbidden(monkeypatch, robots, path):
    serve_robots(monkeypatch, httpx.Response(200, text=robots))

    detail = expect_http_error(403, main.assert_robots_allowed, f"https://example.com{path}")

    assert detail == "robots.txt does not allow ingestion of this URL"


def test_robots_uses_only_the_most_specific_agent_group(monkeypatch):
    # A crawler with its own group ignores the "*" group, so /private/ is allowed here.
    serve_robots(monkeypatch, httpx.Response(200, text=ROBOTS_RAGPIPELINE_GROUP))

    main.assert_robots_allowed("https://example.com/private/page")


def test_robots_is_fetched_from_site_root(monkeypatch):
    requests = serve_robots(monkeypatch, httpx.Response(200, text=""))

    main.assert_robots_allowed("https://example.com:8443/a/b/c?q=1")

    assert str(requests[0].url) == "https://example.com:8443/robots.txt"


def test_robots_server_error_is_rejected(monkeypatch):
    serve_robots(monkeypatch, httpx.Response(503))

    detail = expect_http_error(400, main.assert_robots_allowed, "https://example.com/page")

    assert detail == "Could not verify robots.txt for example.com"


def test_robots_network_error_is_rejected(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    serve(monkeypatch, handler)

    expect_http_error(400, main.assert_robots_allowed, "https://example.com/page")


@pytest.mark.xfail(strict=True, reason="Known gap: a missing robots.txt (404) should allow ingestion per RFC 9309")
def test_missing_robots_txt_allows_ingestion(monkeypatch):
    serve_robots(monkeypatch, httpx.Response(404))

    main.assert_robots_allowed("https://example.com/page")


# /ingest/url wires the checks together


def test_ingest_url_rejects_private_address_before_fetching(monkeypatch):
    resolve_to(monkeypatch, "127.0.0.1")

    def must_not_fetch(request):
        raise AssertionError(f"fetched {request.url}")

    serve(monkeypatch, must_not_fetch)

    from fastapi.testclient import TestClient

    response = TestClient(main.app).post("/ingest/url", json={"url": "http://localhost:6333/collections"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Private URL ingestion is disabled"
