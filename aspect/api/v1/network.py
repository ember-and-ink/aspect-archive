"""`/api/v1/proxy/status` -- does the configured outbound proxy actually work.

A proxy setting that is merely *stored* tells you nothing: a typo'd port, a
tunnel that died, or a URL the client silently ignored all look identical from
the settings form. So the check is empirical, and it runs two legs at once --
one through the proxy, one deliberately direct -- and compares the external
address each one reports. Only a *difference* proves traffic is being carried;
identical addresses mean the proxy is configured but not changing anything,
which is worth telling someone about rather than showing a green light for.

The echo service is api.ipify.org, the one third-party host this server calls
that is not a card source. Both legs are fetched concurrently and neither is
allowed to fail the other -- a dead direct leg (offline, or a firewall that only
permits the proxy) still lets the proxy leg report a green result.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Query

from aspect.api.schemas import ProxyStatusOut
from aspect.runtime import net

logger = logging.getLogger("aspect.api")

router = APIRouter()

# Plain-text so there is nothing to parse and nothing to go wrong in a failure
# mode we'd then have to distinguish from a real error.
IPIFY_URL = "https://api.ipify.org"
_TIMEOUT = 5.0


async def _external_ip(proxy: str | None) -> tuple[str | None, str | None]:
    """`(ip, error)` -- exactly one is set. Never raises: both legs of the check
    are failure modes worth reporting, not exceptions worth propagating."""
    try:
        async with net.async_client(proxy=proxy, timeout=_TIMEOUT) as client:
            resp = await client.get(IPIFY_URL)
            resp.raise_for_status()
            return resp.text.strip(), None
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # e.g. an httpx transport that refuses the proxy URL
        return None, str(exc)


@router.get("/proxy/status", response_model=ProxyStatusOut, summary="Is the outbound proxy working")
async def proxy_status(
    url: str | None = Query(
        None,
        description="Test this proxy URL instead of the stored one. Nothing is persisted.",
    ),
) -> ProxyStatusOut:
    """`async def`, unlike the rest of `/api/v1`.

    The convention there is a plain `def` because those handlers block on the
    filesystem and belong in FastAPI's threadpool. This one blocks on two
    network round trips that are meant to run *concurrently*, which is the one
    thing the threadpool cannot give us.

    `url` exists because the settings panel needs to answer "does what I just
    typed work" *before* it is stored. The frontend's save path is debounced and
    fire-and-forget, so a check that read the stored value after a save would be
    racing a 400 ms timer and would sometimes report on the previous proxy. It
    tests only -- nothing is written, and omitting it reads the stored value.
    """
    # The blob may have been rewritten by PUT /settings a moment ago; an mtime
    # is only worth trusting when it has changed, and this route exists to
    # answer for what is configured right now.
    net.reset_cache()
    if url is None:
        proxy = net.resolved_proxy()
    else:
        candidate = url.strip()
        proxy = net.validate(candidate, "the settings form") if candidate else None
        if candidate and proxy is None:
            return ProxyStatusOut(
                configured=True,
                url=net.redact(candidate),
                state="error",
                error="not a usable proxy URL -- expected something like http://host:port",
            )

    started = time.monotonic()
    (proxy_ip, proxy_error), (direct_ip, direct_error) = await asyncio.gather(
        _external_ip(proxy) if proxy else _noop(),
        _external_ip(None),
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    if not proxy:
        return ProxyStatusOut(
            configured=False,
            state="unset",
            direct_ip=direct_ip,
            latency_ms=latency_ms,
            error=direct_error,
        )

    if proxy_error is not None:
        logger.warning("proxy check failed via %s: %s", net.redact(proxy), proxy_error)
        state = "error"
    elif direct_ip is not None and proxy_ip == direct_ip:
        state = "bypassed"
    else:
        state = "ok"

    return ProxyStatusOut(
        configured=True,
        url=net.redact(proxy),
        state=state,
        proxy_ip=proxy_ip,
        direct_ip=direct_ip,
        latency_ms=latency_ms,
        error=proxy_error,
    )


async def _noop() -> tuple[str | None, str | None]:
    return None, None
