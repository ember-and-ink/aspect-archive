"""The two network routes: the CORS passthrough and the proxy status check.

The passthrough is the one route in this server that fetches an arbitrary URL a
stranger wrote, so most of what is tested here is what it *refuses*. The rest
pins two contracts the vendored frontend reads by hand -- the exact
unreachable-upstream body, and "not a 404 means the route exists" -- which are
easy to break by tidying an error message.
"""

from __future__ import annotations

import gzip
import json
from urllib.parse import quote

import httpx
import pytest

from aspect.api import cors_proxy
from aspect.config import settings
from aspect.runtime import net


def enc(url: str) -> str:
    """What `proxyEncode` in web/modules/providers/provider-utils.js produces."""
    return quote(url, safe="")


@pytest.fixture(autouse=True)
def cold_cache(monkeypatch):
    """No developer `.env` proxy, no inherited memo -- these tests assert on
    what the client was built with."""
    monkeypatch.setattr(settings, "http_proxy", None)
    net.reset_cache()
    yield
    net.reset_cache()


@pytest.fixture
def upstream(monkeypatch):
    """Swap the passthrough's client factory for a MockTransport, and record the
    requests it was handed so header forwarding can be asserted."""
    seen: list[httpx.Request] = []
    responses: dict = {"handler": lambda request: httpx.Response(200, text="ok")}

    def fake_async_client(**kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return responses["handler"](request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cors_proxy.net, "async_client", fake_async_client)
    # The guard's DNS leg would resolve a fake hostname for real; the refusals
    # it exists for are tested separately, below.
    monkeypatch.setattr(cors_proxy.media_guard, "preflight_dns", lambda url: None)
    return {"seen": seen, "responses": responses}


# --- /proxy/{url} : what it refuses ------------------------------------------


@pytest.mark.parametrize(
    "url,fragment",
    [
        ("http://127.0.0.1:8000/", "private IPv4"),
        ("http://192.168.1.10/admin", "private IPv4"),
        ("http://[::1]/", "private IPv6"),
        ("http://localhost/x", "blocked hostname"),
        ("http://169.254.169.254/latest/meta-data/", "private IPv4"),
        ("http://metadata.google.internal/", "blocked hostname"),
        ("http://thing.internal/", "blocked hostname suffix"),
        ("ftp://example.com/x", "blocked scheme"),
        ("file:///etc/passwd", "invalid URL"),  # no host at all, refused before the scheme check
    ],
)
def test_the_guard_refuses_internal_and_non_http_targets(client, url, fragment):
    """This route is the server fetching a URL a stranger put on a character
    card. Without the guard it is an SSRF hole into whatever LAN it runs in."""
    resp = client.get(f"/proxy/{enc(url)}")
    assert resp.status_code == 400
    assert fragment in resp.text


def test_a_refusal_is_not_a_404(client):
    """`gatherEnvInfo` probes this route with our own origin and reads anything
    that is not a 404 as "the passthrough exists". A refusal is a working route
    answering, so it must not use 404 to say no."""
    resp = client.get(f"/proxy/{enc('http://127.0.0.1:8000/')}")
    assert resp.status_code != 404


def test_a_host_that_resolves_only_to_private_addresses_is_refused(client, monkeypatch):
    """The literal-hostname check cannot see this one -- `rebind.example.com`
    looks fine until it resolves to 127.0.0.1."""
    monkeypatch.setattr(
        cors_proxy.media_guard,
        "preflight_dns",
        lambda url: "rebind.example.com resolves only to blocked addresses: ['127.0.0.1']",
    )
    resp = client.get(f"/proxy/{enc('https://rebind.example.com/x')}")
    assert resp.status_code == 400
    assert "blocked addresses" in resp.text


def test_a_redirect_to_an_internal_target_is_refused(client, upstream):
    """The guard has to see every hop. A public URL that answers with a 302
    into the LAN is the same SSRF as asking for the LAN address directly, and a
    guard that only vets the URL it was handed never notices."""
    upstream["responses"]["handler"] = lambda request: httpx.Response(
        302, headers={"location": "http://192.168.1.10/admin"}
    )
    resp = client.get(f"/proxy/{enc('https://example.com/innocent.png')}")
    assert resp.status_code == 400
    assert "private IPv4" in resp.text
    assert [str(r.url) for r in upstream["seen"]] == ["https://example.com/innocent.png"]


def test_a_redirect_to_a_public_target_is_followed(client, upstream):
    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.net/a.png"})
        return httpx.Response(200, text="image")

    upstream["responses"]["handler"] = handler
    resp = client.get(f"/proxy/{enc('https://example.com/a.png')}")
    assert resp.status_code == 200
    assert resp.text == "image"


# --- /proxy/{url} : what it passes through -----------------------------------


def test_the_body_status_and_content_type_come_back_verbatim(client, upstream):
    upstream["responses"]["handler"] = lambda request: httpx.Response(
        201, json={"hello": "world"}, headers={"content-type": "application/json"}
    )
    resp = client.get(f"/proxy/{enc('https://example.com/api?a=1&b=2')}")
    assert resp.status_code == 201
    assert resp.json() == {"hello": "world"}
    assert resp.headers["content-type"] == "application/json"


def test_the_whole_url_including_its_query_survives_encoding(client, upstream):
    """`proxyEncode` escapes `?` and `&` into the path, so the query only exists
    if Starlette's path decoding hands it back intact."""
    client.get(f"/proxy/{enc('https://example.com/search?q=a+b&page=2')}")
    assert str(upstream["seen"][0].url) == "https://example.com/search?q=a+b&page=2"


def test_parens_in_a_filename_survive(client, upstream):
    """postimg's `(1)` filenames are why proxyEncode escapes sub-delims at all."""
    client.get(f"/proxy/{enc('https://i.postimg.cc/x/photo(1).png')}")
    assert str(upstream["seen"][0].url) == "https://i.postimg.cc/x/photo(1).png"


def test_provider_auth_headers_are_forwarded(client, upstream):
    """Chub's calls carry a token; a passthrough that dropped it would turn
    every authenticated browse into an anonymous one."""
    client.get(
        f"/proxy/{enc('https://api.chub.ai/search')}",
        headers={"Authorization": "Bearer tok", "CH-API-KEY": "key"},
    )
    sent = upstream["seen"][0].headers
    assert sent["authorization"] == "Bearer tok"
    assert sent["ch-api-key"] == "key"


def test_our_own_hop_headers_are_not_replayed_upstream(client, upstream):
    """`host` would name this server, and `cookie` would hand the archive's own
    session material to a third party."""
    client.get(
        f"/proxy/{enc('https://example.com/x')}",
        headers={"Cookie": "session=secret", "Referer": "http://localhost:8000/"},
    )
    sent = upstream["seen"][0].headers
    assert "cookie" not in sent
    assert "referer" not in sent
    assert sent["host"] == "example.com"


def test_content_encoding_is_not_copied_back(client, upstream):
    """httpx already decoded the body; a copied `content-encoding: gzip` would
    describe bytes that are no longer gzipped and the browser would fail."""
    upstream["responses"]["handler"] = lambda request: httpx.Response(
        200, content=gzip.compress(b"plain"), headers={"content-encoding": "gzip"}
    )
    resp = client.get(f"/proxy/{enc('https://example.com/x')}")
    assert "content-encoding" not in {k.lower() for k in resp.headers}
    assert resp.text == "plain"


def test_post_is_supported_for_mega(client, upstream):
    """mega.js is the one caller that posts a command batch through the
    fallback; every other site is a GET."""
    body = [{"a": "f"}]
    client.post(f"/proxy/{enc('https://g.api.mega.co.nz/cs')}", json=body)
    request = upstream["seen"][0]
    assert request.method == "POST"
    assert json.loads(request.content) == body


def test_an_upstream_error_status_is_relayed_not_swallowed(client, upstream):
    upstream["responses"]["handler"] = lambda request: httpx.Response(403, text="Forbidden")
    resp = client.get(f"/proxy/{enc('https://example.com/x')}")
    assert resp.status_code == 403
    assert resp.text == "Forbidden"


def test_an_unreachable_upstream_answers_the_exact_legacy_body(client, upstream):
    """provider-utils.js:247 compares this body character for character to tell
    "your server could not reach the provider" apart from "the provider returned
    a 500". Rewording it silently degrades that error message."""

    def boom(request):
        raise httpx.ConnectError("no route to host")

    upstream["responses"]["handler"] = boom
    resp = client.get(f"/proxy/{enc('https://example.com/x')}")
    assert resp.status_code == 500
    assert resp.text.strip() == "Internal Server Error"


def test_an_oversized_response_is_refused(client, upstream):
    upstream["responses"]["handler"] = lambda request: httpx.Response(
        200, content=b"x" * 32, headers={"content-length": str(60 * 1024 * 1024)}
    )
    resp = client.get(f"/proxy/{enc('https://example.com/huge.png')}")
    assert resp.status_code == 502
    # A fixed message: the exception's own text is not echoed to the caller.
    assert resp.text == "refused: response too large"


def test_the_passthrough_uses_the_configured_proxy(client, monkeypatch, populated_archive):
    """The point of the whole feature: browsing traffic goes through it too."""
    built: dict = {}
    real = net.async_client

    def spy(**kwargs):
        built["proxy"] = kwargs.get("proxy", net.resolved_proxy())
        return real(**kwargs, transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    settings.settings_file.write_text(json.dumps({net.SETTINGS_KEY: "http://ui:8118"}))
    net.reset_cache()
    monkeypatch.setattr(cors_proxy.net, "async_client", spy)
    monkeypatch.setattr(cors_proxy.media_guard, "preflight_dns", lambda url: None)

    client.get(f"/proxy/{enc('https://example.com/x')}")
    assert built["proxy"] == "http://ui:8118"


# --- /api/v1/proxy/status ----------------------------------------------------


@pytest.fixture
def ip_legs(monkeypatch):
    """Control what each leg of the status check reports, by proxy value."""
    from aspect.api.v1 import network

    answers: dict = {}

    async def fake(proxy):
        return answers.get(proxy, (None, "not configured in this test"))

    monkeypatch.setattr(network, "_external_ip", fake)
    return answers


def test_status_is_unset_when_no_proxy_is_configured(client, ip_legs):
    ip_legs[None] = ("203.0.113.9", None)
    body = client.get("/api/v1/proxy/status").json()
    assert body["configured"] is False
    assert body["state"] == "unset"
    assert body["direct_ip"] == "203.0.113.9"


def test_status_is_ok_only_when_the_ips_differ(client, ip_legs):
    settings.settings_file.write_text(json.dumps({net.SETTINGS_KEY: "http://ui:8118"}))
    net.reset_cache()
    ip_legs["http://ui:8118"] = ("198.51.100.4", None)
    ip_legs[None] = ("203.0.113.9", None)
    body = client.get("/api/v1/proxy/status").json()
    assert body["state"] == "ok"
    assert body["proxy_ip"] == "198.51.100.4"
    assert body["direct_ip"] == "203.0.113.9"


def test_the_same_ip_on_both_legs_is_bypassed_not_ok(client, ip_legs):
    """Reachable but not carrying traffic. Showing green here would be the one
    failure mode the whole two-leg design exists to catch."""
    settings.settings_file.write_text(json.dumps({net.SETTINGS_KEY: "http://ui:8118"}))
    net.reset_cache()
    ip_legs["http://ui:8118"] = ("203.0.113.9", None)
    ip_legs[None] = ("203.0.113.9", None)
    assert client.get("/api/v1/proxy/status").json()["state"] == "bypassed"


def test_a_dead_proxy_is_an_error(client, ip_legs):
    settings.settings_file.write_text(json.dumps({net.SETTINGS_KEY: "http://ui:8118"}))
    net.reset_cache()
    ip_legs["http://ui:8118"] = (None, "ConnectError: connection refused")
    ip_legs[None] = ("203.0.113.9", None)
    body = client.get("/api/v1/proxy/status").json()
    assert body["state"] == "error"
    assert "connection refused" in body["error"]


def test_a_dead_direct_leg_does_not_fail_the_proxy_leg(client, ip_legs):
    """A firewall that only permits the proxy is a legitimate setup; the check
    must not report red just because the control leg could not run."""
    settings.settings_file.write_text(json.dumps({net.SETTINGS_KEY: "http://ui:8118"}))
    net.reset_cache()
    ip_legs["http://ui:8118"] = ("198.51.100.4", None)
    ip_legs[None] = (None, "ConnectError: blocked")
    body = client.get("/api/v1/proxy/status").json()
    assert body["state"] == "ok"
    assert body["direct_ip"] is None


def test_an_unsaved_url_can_be_tested_without_storing_it(client, ip_legs):
    """The settings panel checks what was typed before the debounced save has
    landed, so a stored value must not be what gets tested."""
    ip_legs["http://typed:9999"] = ("198.51.100.7", None)
    ip_legs[None] = ("203.0.113.9", None)
    body = client.get("/api/v1/proxy/status", params={"url": "http://typed:9999"}).json()
    assert body["state"] == "ok"
    assert body["proxy_ip"] == "198.51.100.7"
    assert not settings.settings_file.exists() or net.SETTINGS_KEY not in json.loads(
        settings.settings_file.read_text()
    )


def test_testing_an_unusable_url_reports_an_error_not_a_crash(client, ip_legs):
    body = client.get("/api/v1/proxy/status", params={"url": "not-a-proxy"}).json()
    assert body["state"] == "error"
    assert "usable proxy URL" in body["error"]


def test_the_password_never_leaves_the_server(client, ip_legs):
    settings.settings_file.write_text(
        json.dumps({net.SETTINGS_KEY: "http://me:hunter2@ui:8118"})
    )
    net.reset_cache()
    ip_legs["http://me:hunter2@ui:8118"] = ("198.51.100.4", None)
    ip_legs[None] = ("203.0.113.9", None)
    resp = client.get("/api/v1/proxy/status")
    assert "hunter2" not in resp.text
    assert resp.json()["url"] == "http://me:***@ui:8118"
