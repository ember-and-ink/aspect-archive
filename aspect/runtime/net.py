"""The one place an outbound HTTP client is built.

WHY THIS EXISTS
Every request this server makes to the outside world -- media downloads, card
avatars, the DataCat session, the browser's CORS passthrough -- should go
through the same optional proxy, and none of them should ever pick one up by
accident. Before this module there were five independent `httpx.Client(...)`
constructions, four of which passed `settings.http_proxy` and one of which
(`aspect.cards.avatar_fetch`) did not, and all five inherited httpx's
`trust_env=True` default -- so an ambient `HTTPS_PROXY` in the container
environment silently proxied everything whether or not the archive was
configured for it.

Both halves matter, and the second is the one worth stating plainly: `trust_env`
is pinned **off** here. The resolved proxy is the only thing that ever routes a
request, so "no proxy configured" means no proxy, and turning it off in the UI
actually turns it off.

WHERE THE URL COMES FROM
`data/settings.json`'s `httpProxyUrl`, else the `ASPECT_HTTP_PROXY`
environment variable (`settings.http_proxy`), else nothing.

Reading the settings blob here is a deliberate exception to the rule
`aspect.state.ui_settings` argues for -- that the server stays ignorant of the
frontend's schema. The exception is exactly one key, read defensively (a wrong
type, an unparseable file or an unreadable disk all degrade to "no proxy"
rather than raising), because the alternative -- a second configuration surface
with its own file, endpoint and UI -- costs more than the coupling does. Nothing
else in the server may read this file; see the note in `ui_settings`.

The result is cached against the settings file's mtime and size, because
`download_batch` builds a client per run and the media job worker would
otherwise stat-and-parse the blob in a loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from aspect.config import settings
from aspect.state import ui_settings

logger = logging.getLogger("aspect.runtime.net")

# The key the frontend writes in `data/settings.json`. Namespaced by name rather
# than nested, matching the flat shape the rest of that blob uses.
SETTINGS_KEY = "httpProxyUrl"

# Schemes httpx can route through. SOCKS needs the `socksio` package, which is
# an optional httpx extra and is not a dependency here -- accepted only when it
# is importable, so a SOCKS URL degrades to a logged "no proxy" instead of an
# ImportError raised from inside a download loop.
_PLAIN_SCHEMES = frozenset({"http", "https"})
_SOCKS_SCHEMES = frozenset({"socks5", "socks5h"})


@dataclass(frozen=True)
class _CacheEntry:
    key: tuple[Any, ...]
    url: str | None


_cache: _CacheEntry | None = None


def _socks_available() -> bool:
    try:
        import socksio  # noqa: F401
    except ImportError:
        return False
    return True


def validate(url: str, origin: str) -> str | None:
    """Returns the URL if httpx can actually route through it, else None.

    Never raises: a bad proxy URL must not be able to take down a media run, and
    the operator finds out through the status endpoint's red dot and this log
    line rather than through a traceback.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        logger.warning("ignoring unparseable proxy URL from %s", origin)
        return None
    scheme = parsed.scheme.lower()
    if not scheme or not parsed.hostname:
        logger.warning(
            "ignoring proxy URL from %s: needs a scheme and host, e.g. http://host:port", origin
        )
        return None
    if scheme in _SOCKS_SCHEMES and not _socks_available():
        logger.warning(
            "ignoring %s:// proxy from %s: SOCKS support needs the httpx[socks] extra "
            "(socksio is not installed)",
            scheme,
            origin,
        )
        return None
    if scheme not in _PLAIN_SCHEMES and scheme not in _SOCKS_SCHEMES:
        logger.warning("ignoring proxy URL from %s: unsupported scheme %s://", origin, scheme)
        return None
    return url


def _stored_url() -> tuple[str | None, str]:
    """The `httpProxyUrl` from the settings blob, and where it came from.

    Every failure mode -- no file, damaged JSON, unreadable disk, a non-string
    value -- returns None. Unlike `GET /api/v1/settings`, which must distinguish
    "no settings" from "your settings are damaged" because the frontend would
    otherwise overwrite them, nothing here writes anything back, so degrading to
    "no proxy" is safe and keeps a damaged blob from breaking downloads too.
    """
    try:
        blob = ui_settings.SettingsStore(settings.settings_file).read()
    except ui_settings.SettingsError as exc:
        logger.warning("could not read proxy setting: %s", exc)
        return None, "settings.json"
    value = blob.get(SETTINGS_KEY)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "settings.json"
    if not isinstance(value, str):
        logger.warning(
            "ignoring %s in settings.json: expected a string, got %s",
            SETTINGS_KEY,
            type(value).__name__,
        )
        return None, "settings.json"
    return value.strip(), "settings.json"


def _cache_key() -> tuple[Any, ...]:
    """Cheap enough to compute per client build; changes whenever either source
    could have. `st_mtime_ns` and size together catch a same-second rewrite."""
    path = settings.settings_file
    try:
        stat = path.stat()
        stamp: tuple[Any, ...] = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (None, None)
    return (str(path), *stamp, settings.http_proxy)


def resolved_proxy() -> str | None:
    """The proxy URL every outbound client should use, or None for direct.

    Precedence: `data/settings.json`'s `httpProxyUrl`, then the
    `ASPECT_HTTP_PROXY` environment variable. The UI wins because it is the
    one a person can change without restarting the container; the env var stays
    as the way to configure a deployment before anyone opens the browser.
    """
    global _cache
    key = _cache_key()
    if _cache is not None and _cache.key == key:
        return _cache.url

    raw, origin = _stored_url()
    if raw is None:
        raw, origin = (settings.http_proxy or "").strip() or None, "ASPECT_HTTP_PROXY"
    url = validate(raw, origin) if raw else None
    _cache = _CacheEntry(key=key, url=url)
    return url


def reset_cache() -> None:
    """Drop the memoized lookup. For tests, and for the settings PUT handler --
    an mtime is only granular enough to be trusted when it changed, and a write
    that lands in the same nanosecond as the last read is not worth reasoning
    about when we know for certain the blob just changed."""
    global _cache
    _cache = None


def redact(url: str | None) -> str | None:
    """A proxy URL safe to log or hand to the browser: credentials removed.

    `http://user:hunter2@host:3128` -> `http://user:***@host:3128`. The username
    is kept because it is what makes two otherwise identical proxy entries
    distinguishable to whoever configured them.
    """
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "(unparseable)"
    if parsed.password is None:
        return url
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{parsed.username or ''}:***@{host}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def async_client(*, proxy: Any = ..., **kwargs: Any) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` wired to the configured proxy, env-proxy pinned off.

    Pass `proxy=None` explicitly to force a direct client (the status check's
    control leg); leave it unset for the configured behaviour.
    """
    return httpx.AsyncClient(
        proxy=resolved_proxy() if proxy is ... else proxy,
        trust_env=False,
        **kwargs,
    )


def sync_client(*, proxy: Any = ..., **kwargs: Any) -> httpx.Client:
    """The blocking twin of `async_client`. Same contract."""
    return httpx.Client(
        proxy=resolved_proxy() if proxy is ... else proxy,
        trust_env=False,
        **kwargs,
    )


# --- Long-lived clients ------------------------------------------------------
# A client built once at import (`aspect.deps.avatar_fetcher`, `aspect.api.datacat`'s
# session singleton) freezes whatever the proxy was when the process started, so
# changing the setting in the UI would appear to do nothing until a restart.
# These holders build the client on first use instead and rebuild it whenever the
# resolved proxy has changed since, which keeps "long-lived singleton" and "live
# setting" from being mutually exclusive.
#
# An injected client (every test does this, with a MockTransport) is handed back
# untouched and never rebuilt -- injection is an explicit override of exactly the
# thing this holder manages.


class AsyncClientHolder:
    """Proxy-aware, self-rebuilding `httpx.AsyncClient`."""

    def __init__(self, injected: httpx.AsyncClient | None = None, **kwargs: Any) -> None:
        self._injected = injected
        self._kwargs = kwargs
        self._client: httpx.AsyncClient | None = None
        self._proxy: str | None = None

    @property
    def injected(self) -> bool:
        return self._injected is not None

    async def get(self) -> httpx.AsyncClient:
        if self._injected is not None:
            return self._injected
        proxy = resolved_proxy()
        if self._client is None or proxy != self._proxy:
            stale, self._client = self._client, httpx.AsyncClient(
                proxy=proxy, trust_env=False, **self._kwargs
            )
            self._proxy = proxy
            if stale is not None:
                await stale.aclose()
        return self._client

    async def aclose(self) -> None:
        """Closes only a client this holder built -- an injected one belongs to
        whoever injected it."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class SyncClientHolder:
    """The blocking twin of `AsyncClientHolder`. Same contract."""

    def __init__(self, injected: httpx.Client | None = None, **kwargs: Any) -> None:
        self._injected = injected
        self._kwargs = kwargs
        self._client: httpx.Client | None = None
        self._proxy: str | None = None

    @property
    def injected(self) -> bool:
        return self._injected is not None

    def get(self) -> httpx.Client:
        if self._injected is not None:
            return self._injected
        proxy = resolved_proxy()
        if self._client is None or proxy != self._proxy:
            stale, self._client = self._client, httpx.Client(
                proxy=proxy, trust_env=False, **self._kwargs
            )
            self._proxy = proxy
            if stale is not None:
                stale.close()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
