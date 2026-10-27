"""SSRF guard + size cap for server-side media downloads — Phase 3C.

Port of `isUrlSafeForDownload` / `readBodyWithCap`
(`web/library-sections/30-media-localization-feature.js:756-869`).

In the browser this guard was largely decorative: a browser fetch from a
victim's own tab reaching their own LAN was already the user's problem, not
ours. Once FastAPI is the fetcher (§3/§6 of docs/PHASE_3C_PLAN.md) it is
issuing requests, from inside a home LAN, to URLs strangers wrote into
character cards. That makes this module load-bearing rather than decorative,
and it must exist before any outbound-fetch code does.

`is_url_safe_for_download` is a literal port of the JS: it checks the URL's
scheme and the *literal* hostname string against known-bad names, suffixes,
and private/loopback/link-local/CGNAT ranges — exactly what the browser could
check, since a browser can't inspect the IP a hostname resolves to before
`fetch()` connects.

Server-side, that's not good enough on its own: a hostname that looks
innocuous (`totally-fine.example.com`) can still resolve to `127.0.0.1` or a
cloud metadata address (DNS rebinding). `resolve_safe_addresses` closes that
gap by resolving the host and running every returned address through the same
private-IP predicates, and is meant to be called again at connect time by the
fetcher (task 4), not just once up front — a URL can be re-resolved to a
different, unsafe address between the check and the connect (TOCTOU), so the
fetcher must pin the resolved address it checked and connect to that address
directly rather than re-resolving the hostname.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

# 50 MB hard cap per file. Verbatim MAX_MEDIA_BYTES (30-…:757).
MAX_MEDIA_BYTES = 50 * 1024 * 1024

# Verbatim BLOCKED_HOSTNAME_SUFFIXES / BLOCKED_HOSTNAMES (30-…:759-767).
BLOCKED_HOSTNAME_SUFFIXES = (".local", ".localhost", ".internal")
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.goog",
        "kubernetes.default",
        "kubernetes.default.svc",
    }
)

_ALLOWED_SCHEMES = ("http", "https")


@dataclass(frozen=True)
class UrlSafety:
    ok: bool
    reason: str = ""


def _is_private_ipv4(host: str) -> bool:
    """Port of `isPrivateIPv4` (30-…:769-786)."""
    parts = host.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    octets = [int(p) for p in parts]
    if any(n > 255 for n in octets):
        return True  # malformed -> treat as unsafe, matches JS
    a, b = octets[0], octets[1]
    if a == 0:
        return True  # 0.0.0.0/8
    if a == 10:
        return True  # 10.0.0.0/8
    if a == 127:
        return True  # 127.0.0.0/8 loopback
    if a == 169 and b == 254:
        return True  # 169.254.0.0/16 link-local + AWS metadata
    if a == 172 and 16 <= b <= 31:
        return True  # 172.16.0.0/12 private
    if a == 192 and b == 168:
        return True  # 192.168.0.0/16 private
    if a == 100 and 64 <= b <= 127:
        return True  # 100.64.0.0/10 CGNAT
    if a >= 224:
        return True  # 224.0.0.0/4 multicast + 240.0.0.0/4 reserved
    return False


def _is_private_ipv6(host: str) -> bool:
    """Same ranges as JS's `isPrivateIPv6` (30-…:788-800), but built on
    `ipaddress` instead of ported string/regex matching.

    The straight port has a real bypass: `new URL()` canonicalizes an
    IPv4-mapped address like `::ffff:127.0.0.1` to its hex form
    (`::ffff:7f00:1`) before `.hostname` ever sees it, so the JS regex
    `/^::ffff:(\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3})$/` — which only matches
    the dotted-decimal spelling — never fires, and `isPrivateIPv4` never runs.
    `::ffff:127.0.0.1` (a loopback address) sails through as "safe" in the
    browser version. `ipaddress.IPv6Address.ipv4_mapped` decodes the embedded
    v4 address regardless of which textual form it arrived in, so this port
    closes that gap rather than reproducing it — the plan calls the guard
    load-bearing, not a byte-for-byte behavioral copy, and a known SSRF
    bypass is not something to carry forward on purpose.
    """
    h = host.lower().strip("[]")
    if ":" not in h:
        return False
    try:
        ip = ipaddress.IPv6Address(h)
    except ValueError:
        return False
    if ip.ipv4_mapped is not None:
        return _is_private_ipv4(str(ip.ipv4_mapped))
    if ip.is_unspecified or ip.is_loopback:
        return True
    if ip in ipaddress.ip_network("fc00::/7"):
        return True  # unique-local
    if ip in ipaddress.ip_network("fe80::/10"):
        return True  # link-local
    return False


def is_blocked_hostname(host: str) -> str | None:
    """Returns a reason string if `host` (already lowercased) is blocked, else None."""
    if host in BLOCKED_HOSTNAMES:
        return f"blocked hostname: {host}"
    for suffix in BLOCKED_HOSTNAME_SUFFIXES:
        if host == suffix[1:] or host.endswith(suffix):
            return f"blocked hostname suffix: {host}"
    return None


def is_url_safe_for_download(url: str) -> UrlSafety:
    """Port of `isUrlSafeForDownload` (30-…:802-822).

    Scheme + literal-hostname checks only — no DNS resolution. Use
    `resolve_safe_addresses` before connecting to close the DNS-rebinding gap
    this alone can't see.
    """
    try:
        parsed = urlsplit(url)
    except Exception:
        return UrlSafety(False, "invalid URL")

    if not parsed.scheme or not parsed.hostname:
        return UrlSafety(False, "invalid URL")
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return UrlSafety(False, f"blocked scheme: {parsed.scheme}:")

    host = parsed.hostname.lower()
    if not host:
        return UrlSafety(False, "empty hostname")
    # Python's urlsplit().hostname strips IPv6 brackets; JS's .hostname keeps
    # them, and that's what ends up in its reason strings.
    display_host = f"[{host}]" if ":" in host else host

    blocked = is_blocked_hostname(host)
    if blocked:
        return UrlSafety(False, blocked)

    if _is_private_ipv4(host):
        return UrlSafety(False, f"blocked private IPv4: {display_host}")
    if _is_private_ipv6(host):
        return UrlSafety(False, f"blocked private IPv6: {display_host}")

    return UrlSafety(True)


def is_safe_address(addr: str) -> bool:
    """True if a resolved IP literal (v4 or v6, no brackets) is not
    private/loopback/link-local/CGNAT/multicast/reserved."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return not _is_private_ipv4(str(ip))
    return not _is_private_ipv6(str(ip))


def resolve_safe_addresses(host: str, port: int) -> list[str]:
    """Resolve `host` and return only the addresses that pass the same
    private-IP predicates `is_url_safe_for_download` applies to a literal
    hostname. Raises `UnsafeAddressError` if resolution succeeds but every
    address is blocked (DNS rebinding to a private/internal target), and lets
    `socket.gaierror` propagate for a host that doesn't resolve at all.

    Not itself sufficient against TOCTOU: the fetcher must connect to one of
    the returned addresses directly (e.g. via an httpx transport that pins
    the resolved IP) rather than handing the hostname to the HTTP client and
    letting it re-resolve at connect time.
    """
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    safe: list[str] = []
    seen: set[str] = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        if addr in seen:
            continue
        seen.add(addr)
        if is_safe_address(addr):
            safe.append(addr)
    if not safe:
        raise UnsafeAddressError(f"{host} resolves only to blocked addresses: {sorted(seen)}")
    return safe


class UnsafeAddressError(Exception):
    pass


def preflight_dns(url: str) -> str | None:
    """A reason string if the URL's host resolves only to blocked addresses or
    doesn't resolve at all, else None. Not a full pin against TOCTOU (see this
    module's docstring) -- a best-effort check run immediately before the
    request, closing the common DNS-rebinding case without the complexity of
    pinning the resolved IP through TLS SNI.

    Lives here rather than beside either caller because both server-side
    fetchers -- the media downloader and the browser's CORS passthrough -- must
    run exactly this check, and a security check with two implementations is a
    security check with one of them out of date.
    """
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        resolve_safe_addresses(parsed.hostname or "", port)
    except UnsafeAddressError as exc:
        return str(exc)
    except socket.gaierror as exc:
        return f"DNS resolution failed: {exc}"
    return None


def check_content_length(declared: int | None, max_bytes: int = MAX_MEDIA_BYTES) -> None:
    """Port of the `Content-Length` pre-check in `readBodyWithCap` (30-…:840-843)."""
    if declared is not None and declared > max_bytes:
        raise MediaTooLargeError(f"response too large: {declared} > {max_bytes} bytes")


class MediaTooLargeError(Exception):
    pass


async def read_body_with_cap(response, max_bytes: int = MAX_MEDIA_BYTES) -> bytes:
    """Port of `readBodyWithCap` (30-…:836-869) for an `httpx.Response` in
    streaming mode (`client.stream(...)`). Aborts as soon as the accumulated
    byte count exceeds the cap, rather than buffering the whole (potentially
    huge) body first.
    """
    declared = response.headers.get("content-length")
    check_content_length(int(declared) if declared is not None else None, max_bytes)

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise MediaTooLargeError(f"response exceeded size cap: > {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
