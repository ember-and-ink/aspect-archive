"""`GET|POST /proxy/{url}` -- the passthrough a provider fetch falls back to.

WHY THIS EXISTS
CharacterLibrary was written as a SillyTavern extension, and SillyTavern shipped
a CORS-proxy middleware at exactly this path, which sixteen call sites in the old
frontend reached for. Every one of them tried the provider directly first and
fell back here when the browser's CORS policy (or a provider that simply doesn't
send the header) made the direct leg impossible. After the pivot no route
answered, so that fallback returned a 404 HTML page -- which the media pipeline
then classified as a permanently dead URL, marking characters "media complete"
having never fetched a byte. The client is a different program now and the
fallback is a deliberate one (`fetchWithFallback`, frontend/src/lib/providers/
shared.ts), but the route and the reason for it are unchanged.

So this is a bug fix that predates the proxy feature, and it is also where the
configured outbound proxy earns most of its keep: browsing Chub and running the
gallery extractors is the traffic a person actually wants routed.

WHAT IT WILL AND WON'T FETCH
`aspect.media.guard` gates every request -- the same guard the server-side media
downloader uses, scheme + literal-host checks plus a DNS preflight -- and every
redirect that request is answered with, hop by hop. That is what
keeps this from being an SSRF hole pointed at the LAN it runs inside, and it is
also what stops the route being aimed back at this server's own origin.

There is no enable/disable toggle. The route is useless to an attacker who can
already reach a machine on your LAN (the guard refuses every private and
loopback target, so it cannot be used to pivot inwards), and the frontend has no
working path without it. `gatherEnvInfo`'s probe reads any non-404 as "enabled",
so the guard's 400 on a self-referential URL reports correctly.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from aspect.media import guard as media_guard
from aspect.runtime import net

logger = logging.getLogger("aspect.api.cors_proxy")

router = APIRouter(tags=["proxy"])

# Headers that describe *this* hop and must not be replayed to the upstream.
# `host` would name our own server; `cookie` would forward the archive's own
# session material to a third party; `origin`/`referer` announce a localhost
# origin that some providers reject outright; `accept-encoding` and
# `content-length` are httpx's to set, since it re-encodes and re-frames the
# body. The rest are RFC 9110 hop-by-hop headers.
_DROP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "cookie",
        "origin",
        "referer",
        "accept-encoding",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Dropped on the way back for the same framing reason: httpx has already decoded
# the body, so a `content-encoding: gzip` we copied would describe bytes that are
# no longer gzipped and the browser would fail to parse them.
_DROP_RESPONSE_HEADERS = frozenset(
    {
        "content-encoding",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# The exact body SillyTavern's middleware answered when its own server-side fetch
# failed, and which `provider-utils.js:247` still special-cases to tell "your
# server couldn't reach the provider" apart from "the provider returned a 500".
# Keeping the string verbatim is what keeps that error message accurate.
UPSTREAM_UNREACHABLE_BODY = "Internal Server Error"


def _forwarded_request_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS}


def _forwarded_response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        k: v for k, v in response.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS
    }


# Two routes rather than one `api_route(methods=["GET", "POST"])`, and the
# reason is downstream: FastAPI writes a *single* operation id per function, so
# a two-method route emits the same id under both `get:` and `post:` in the
# OpenAPI document. That is not cosmetic -- openapi-typescript turns each into
# a TypeScript declaration of the same name and the frontend's `tsc -b` fails
# on the duplicate (see `make api-schema`). One function per method is what
# keeps the generated client compilable; both delegate to `_forward` below, so
# there is still one implementation.
@router.get("/proxy/{url:path}")
async def cors_proxy(url: str, request: Request) -> Response:
    """Fetch `url` server-side and hand the response back verbatim.

    The URL arrives percent-encoded as a single path segment (`proxyEncode` in
    provider-utils.js escapes `/` and `?` along with everything else), so
    Starlette's own path decoding hands it back whole, query string included.
    """
    return await _forward(url, request)


@router.post("/proxy/{url:path}")
async def cors_proxy_post(url: str, request: Request) -> Response:
    """`cors_proxy`, for the one caller that posts.

    `mega.js` sends a JSON command batch through the fallback; every other site
    is a GET.
    """
    return await _forward(url, request)


class _HopRefused(Exception):
    """Raised from the request hook to abandon a fetch the guard said no to."""


async def _forward(url: str, request: Request) -> Response:
    target = url.strip()
    if not target:
        return PlainTextResponse("no URL given", status_code=400)

    # 400, not 403, for every refusal below: `gatherEnvInfo` probes this route
    # with our own origin to find out whether it exists at all, and reads
    # anything that isn't a 404 as "enabled". A refusal is a correct answer,
    # not a missing route.
    safety = media_guard.is_url_safe_for_download(target)
    if not safety.ok:
        return PlainTextResponse(f"refused: {safety.reason}", status_code=400)

    refusals: list[str] = []

    async def guard_hop(hop: httpx.Request) -> None:
        """Vet every request the client is about to send, not just the first.

        httpx runs this for each redirect it follows, which is the point: a
        public URL that answers `302 Location: http://192.168.1.1/` would
        otherwise walk straight past a guard that only looked at `target`.
        """
        hop_url = str(hop.url)
        hop_safety = media_guard.is_url_safe_for_download(hop_url)
        if hop_safety.ok:
            # getaddrinfo blocks; off the event loop, exactly as the media
            # writer does it.
            reason = await asyncio.to_thread(media_guard.preflight_dns, hop_url)
        else:
            reason = hop_safety.reason
        if reason:
            refusals.append(reason)
            raise _HopRefused

    body = await request.body() if request.method == "POST" else None

    try:
        async with net.async_client(
            timeout=30.0, follow_redirects=True, event_hooks={"request": [guard_hop]}
        ) as client:
            async with client.stream(
                request.method,
                target,
                headers=_forwarded_request_headers(request),
                content=body,
            ) as upstream:
                try:
                    payload = await media_guard.read_body_with_cap(upstream)
                except media_guard.MediaTooLargeError:
                    # A fixed string, not the exception's own text: nothing
                    # raised server-side is echoed back to the caller.
                    return PlainTextResponse("refused: response too large", status_code=502)
                return Response(
                    content=payload,
                    status_code=upstream.status_code,
                    headers=_forwarded_response_headers(upstream),
                )
    except _HopRefused:
        return PlainTextResponse(f"refused: {refusals[-1]}", status_code=400)
    except httpx.HTTPError as exc:
        # The body has to be exactly this string -- see UPSTREAM_UNREACHABLE_BODY.
        logger.warning("cors proxy could not reach %s: %s", target, exc)
        return PlainTextResponse(UPSTREAM_UNREACHABLE_BODY, status_code=500)
