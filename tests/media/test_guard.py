"""aspect/media/guard.py: the SSRF guard + size cap that must exist before any
outbound-fetch code does (docs/PHASE_3C_PLAN.md §6/§9 step 3).

Most cases are checked against real `isUrlSafeForDownload` output captured
from node (tests/fixtures/media_3c/guard_urls_js.json — see the commit that
added it for the capture script). Three URLs are deliberately overridden
because the Python port intentionally diverges from the JS, in the safer
direction in every case — see the inline notes.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from aspect.media.guard import (
    MAX_MEDIA_BYTES,
    MediaTooLargeError,
    UnsafeAddressError,
    check_content_length,
    is_safe_address,
    is_url_safe_for_download,
    read_body_with_cap,
    resolve_safe_addresses,
)


class _FakeStreamResponse:
    """Minimal stand-in for httpx.Response in streaming mode."""

    def __init__(self, chunks: list[bytes], content_length: str | None = None):
        self._chunks = chunks
        self.headers = {"content-length": content_length} if content_length is not None else {}

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

FIXTURE = Path(__file__).parent.parent / "fixtures" / "media_3c" / "guard_urls_js.json"

# Divergences from the captured JS output, and why each is intentional.
OVERRIDES = {
    # JS canonicalizes ::ffff:127.0.0.1 to hex (::ffff:7f00:1) before the
    # regex-based check sees it, so the regex never matches and the JS
    # reports this loopback address as "safe" -- a real SSRF bypass. The
    # ipaddress-based Python port decodes the embedded v4 address regardless
    # of textual form and correctly blocks it. See _is_private_ipv6's
    # docstring in media_guard.py.
    "https://[::ffff:127.0.0.1]/x.png": False,
    # "https:///x.png" is a degenerate URL with no authority. JS's WHATWG
    # parser has a quirk that reads "x.png" as the *hostname* here (so it
    # passes as "safe" against a host that happens to look like a filename).
    # Python's urlsplit reports no hostname at all, which this module treats
    # as invalid -- stricter, not more permissive.
    "https:///x.png": False,
    # "999.999.999.999" isn't a valid IPv4 literal. JS's URL constructor
    # rejects the whole URL as invalid before any host logic runs. Python's
    # urlsplit doesn't validate octet ranges, so it reaches _is_private_ipv4,
    # whose "malformed -> treat as unsafe" rule (ported verbatim from JS)
    # blocks it anyway. Same outcome (blocked), different reason text.
    "https://999.999.999.999/x.png": False,
}


def _cases():
    data = json.loads(FIXTURE.read_text())
    for url, expected in data.items():
        yield url, OVERRIDES.get(url, expected["ok"])


@pytest.mark.parametrize("url,expected_ok", list(_cases()))
def test_matches_js_isUrlSafeForDownload(url, expected_ok):
    result = is_url_safe_for_download(url)
    assert result.ok == expected_ok, f"{url}: got {result}"


def test_ipv6_mapped_loopback_is_blocked_despite_hex_form():
    # The one case the plain JS-parity table can't express directly.
    assert is_url_safe_for_download("https://[::ffff:7f00:1]/x.png").ok is False


def test_blocked_reason_strings_include_ipv6_brackets():
    result = is_url_safe_for_download("https://[::1]/x.png")
    assert result.reason == "blocked private IPv6: [::1]"


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------


def test_check_content_length_under_cap_is_fine():
    check_content_length(1024, max_bytes=MAX_MEDIA_BYTES)
    check_content_length(None, max_bytes=MAX_MEDIA_BYTES)


def test_check_content_length_over_cap_raises():
    with pytest.raises(MediaTooLargeError):
        check_content_length(MAX_MEDIA_BYTES + 1, max_bytes=MAX_MEDIA_BYTES)


def test_default_cap_is_50mb():
    assert MAX_MEDIA_BYTES == 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# DNS-rebinding: resolve_safe_addresses / is_safe_address
# ---------------------------------------------------------------------------


def test_is_safe_address_rejects_private_v4():
    assert is_safe_address("127.0.0.1") is False
    assert is_safe_address("10.1.2.3") is False
    assert is_safe_address("169.254.169.254") is False


def test_is_safe_address_accepts_public_v4():
    assert is_safe_address("8.8.8.8") is True


def test_is_safe_address_rejects_garbage():
    assert is_safe_address("not-an-ip") is False


def test_resolve_safe_addresses_rejects_hostname_that_only_resolves_private():
    fake_infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    with patch("aspect.media.guard.socket.getaddrinfo", return_value=fake_infos):
        with pytest.raises(UnsafeAddressError):
            resolve_safe_addresses("evil.example.com", 443)


def test_resolve_safe_addresses_returns_public_addresses_only():
    fake_infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ]
    with patch("aspect.media.guard.socket.getaddrinfo", return_value=fake_infos):
        addrs = resolve_safe_addresses("mixed.example.com", 443)
    assert addrs == ["8.8.8.8"]


def test_resolve_safe_addresses_propagates_nxdomain():
    with patch(
        "aspect.media.guard.socket.getaddrinfo",
        side_effect=socket.gaierror("nodename nor servname provided"),
    ):
        with pytest.raises(socket.gaierror):
            resolve_safe_addresses("does-not-exist.invalid", 443)


# ---------------------------------------------------------------------------
# Streaming body cap
# ---------------------------------------------------------------------------


async def test_read_body_with_cap_rejects_declared_content_length():
    resp = _FakeStreamResponse([b"x" * 10], content_length=str(MAX_MEDIA_BYTES + 1))
    with pytest.raises(MediaTooLargeError):
        await read_body_with_cap(resp, max_bytes=MAX_MEDIA_BYTES)


async def test_read_body_with_cap_aborts_mid_stream_over_cap():
    resp = _FakeStreamResponse([b"a" * 10, b"b" * 10, b"c" * 10])
    with pytest.raises(MediaTooLargeError):
        await read_body_with_cap(resp, max_bytes=15)


async def test_read_body_with_cap_returns_bytes_under_cap():
    resp = _FakeStreamResponse([b"abc", b"def"])
    result = await read_body_with_cap(resp, max_bytes=1024)
    assert result == b"abcdef"
