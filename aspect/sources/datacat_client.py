"""Recovers a datacat-imported card's original avatar URL by calling datacat's
own (anonymous, no-login) API -- the same one SillyTavern-CharacterLibrary's
"Restore Original Avatars" tool uses. The datacat export this project imports
(see aspect/datacat.py) never carries the source image, only the
JanitorAI character id -- datacat's character-detail payload does, at full
resolution, inside its embedded v2 card.

Session: POST /api/liberator/identify with a throwaway device token returns an
anonymous session token, no credentials involved. Confirmed live 2026-07-30:
this and the character-detail GET both work from a plain server-side request,
no browser/cookies/CF challenge needed -- datacat's own auth gate is just this
handshake.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from aspect.config import ROOT, settings
from aspect.runtime import net

logger = logging.getLogger(__name__)

DATACAT_API_BASE = "https://datacat.run"

_ENV_TOKEN_KEY = "ASPECT_DATACAT_SESSION_TOKEN"
_ENV_TOKEN_LINE_RE = re.compile(rf"^{_ENV_TOKEN_KEY}=.*$", re.MULTILINE)
# A stored token that gets rejected outright (vs. e.g. the character simply
# not existing) is treated as stale, not a hard failure -- one retry with a
# freshly identified session before giving up on that lookup.
_AUTH_FAILURE_STATUS = {401, 403}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": DATACAT_API_BASE,
    "Referer": f"{DATACAT_API_BASE}/",
}

_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.0
# Retried as transient; anything else (4xx, a parsed response) returns as-is.
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


def _request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response | None:
    """The one place every outbound datacat call goes through: retries a
    transient failure (network error or 429/5xx) up to `_MAX_ATTEMPTS` times
    with linear backoff, then gives up and returns None -- callers treat that
    as "couldn't resolve this one" and move on, matching the rest of the
    import pipeline's one-bad-card-must-not-abort-the-batch contract. Never
    raises."""
    last_error: Exception | str | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_error = exc
        else:
            if resp.status_code not in _RETRY_STATUS_CODES:
                return resp
            last_error = f"HTTP {resp.status_code}"
        if attempt < _MAX_ATTEMPTS:
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
    logger.warning("datacat request failed after %d attempts: %s %s (%s)", _MAX_ATTEMPTS, method, url, last_error)
    return None


def _persist_session_token(token: str, env_path: Path) -> None:
    """Write a freshly-fetched anonymous session token into `.env` so the next
    run reuses it instead of hitting /api/liberator/identify again. Replaces
    an existing ASPECT_DATACAT_SESSION_TOKEN line if there is one,
    otherwise appends it; every other line is left untouched."""
    line = f"{_ENV_TOKEN_KEY}={token}"
    existing = env_path.read_text() if env_path.exists() else ""
    if _ENV_TOKEN_LINE_RE.search(existing):
        updated = _ENV_TOKEN_LINE_RE.sub(line, existing)
    else:
        sep = "" if not existing or existing.endswith("\n") else "\n"
        updated = f"{existing}{sep}{line}\n"
    env_path.write_text(updated)


def _original_avatar_url(character: dict[str, Any]) -> str | None:
    """The untouched source-CDN avatar URL (JanitorAI's ella.janitorai.com,
    or Saucepan's cdn.saucepan.ai for a saucepan-sourced character) out of a
    datacat character-detail payload. datacat's own `avatar`/
    `avatar_variant_urls` fields are its re-hosted, downscaled copies (a
    768px "hero" at best) -- never used here since the whole point is the
    untouched original the local import can no longer produce."""
    candidates: list[Any] = [
        ((character.get("chara_card_v2_json") or {}).get("data") or {}).get("avatar"),
    ]
    for variant in character.get("content_variants") or []:
        content = (variant or {}).get("content") or {}
        candidates.append(((content.get("chara_card_v2_json") or {}).get("data") or {}).get("avatar"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
    return None


class DatacatImageResolver:
    """Resolves a datacat character id to its original source-CDN avatar URL.

    A session token is expensive only in the sense that it's one extra round
    trip -- but hitting /api/liberator/identify on every run when the same
    anonymous token keeps working is needless load, so a token is loaded from
    `settings.datacat_session_token` (ASPECT_DATACAT_SESSION_TOKEN in
    .env) up front if one's there, and a freshly-fetched one is written back
    to .env (`persist=True`, the default) so the next run picks it up too. If
    a stored token turns out to have been rejected, resolve() drops it and
    identifies a fresh one automatically -- no manual .env cleanup needed."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        persist: bool = True,
        env_path: Path | None = None,
    ) -> None:
        self._clients = net.SyncClientHolder(client, timeout=15.0, headers=_HEADERS)
        self._session_token: str | None = settings.datacat_session_token or None
        self._persist = persist
        self._env_path = env_path or (ROOT / ".env")

    def _ensure_session(self) -> str | None:
        if self._session_token:
            return self._session_token
        resp = _request(
            self._clients.get(),
            "POST",
            f"{DATACAT_API_BASE}/api/liberator/identify",
            json={"deviceToken": str(uuid.uuid4())},
        )
        if resp is None or resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not data.get("success"):
            return None
        token = data.get("sessionToken")
        if not isinstance(token, str) or not token:
            return None
        self._session_token = token
        if self._persist:
            _persist_session_token(token, self._env_path)
            print(f"[datacat] fetched a new anonymous session token, saved to {self._env_path}")
        return self._session_token

    def _fetch_character(self, character_id: str, token: str) -> httpx.Response | None:
        return _request(
            self._clients.get(),
            "GET",
            f"{DATACAT_API_BASE}/api/characters/{character_id}",
            headers={"X-Session-Token": token},
        )

    def resolve(self, character_id: str) -> str | None:
        """The original avatar URL for a datacat character id, or None if it
        can't be recovered (no session, the character is gone from datacat's
        index, or its payload carries no original-CDN avatar)."""
        if not character_id:
            return None
        token = self._ensure_session()
        if not token:
            return None
        resp = self._fetch_character(character_id, token)
        if resp is not None and resp.status_code in _AUTH_FAILURE_STATUS:
            self._session_token = None
            token = self._ensure_session()
            if not token:
                return None
            resp = self._fetch_character(character_id, token)
        if resp is None or resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not data.get("success"):
            return None
        return _original_avatar_url(data.get("character") or {})

    def close(self) -> None:
        self._clients.close()


# ---------------------------------------------------------------------------
# Live session transport for the browser's browse/import flow (Phase 3B S2,
# see docs/PHASE_3B_PLAN.md). This is the "promotion" the plan describes: the
# same anonymous handshake above, plus token-lifecycle and read-only proxy
# routes, including the extraction-submit bodies for DataCat's two upstream
# kinds (JanitorAI vs Saucepan), which are not documented anywhere public.
# ---------------------------------------------------------------------------


class DatacatSessionError(Exception):
    """Raised by DatacatSession.proxy_get; carries the HTTP status the route
    handler should answer with (401/403/502)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# Read-only API paths forwarded by dc-proxy. A safety valve against the proxy
# becoming an arbitrary datacat.run relay.
_DC_ALLOWED_PATHS = [
    re.compile(r"^/api/characters/fresh\b"),
    re.compile(r"^/api/characters/recent-public\b"),
    re.compile(r"^/api/characters/[a-f0-9-]+$"),
    re.compile(r"^/api/creators/[a-f0-9-]+$"),
    re.compile(r"^/api/creators/[a-f0-9-]+/characters\b"),
    re.compile(r"^/api/tags/faceted\b"),
    re.compile(r"^/api/extraction/status-projection$"),
]


def dc_proxy_path_allowed(path: str) -> bool:
    return any(p.search(path) for p in _DC_ALLOWED_PATHS)


class DatacatSession:
    """A server-held DataCat session backing /api/v1/datacat/* -- the live
    counterpart to DatacatImageResolver above (which is a one-shot, .env-
    persisted token for offline batch scripts). This one is in-memory only:
    a browse session is ephemeral, not something a later `make import` run
    should inherit."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._clients = net.AsyncClientHolder(client, timeout=15.0, headers=_HEADERS)
        self._token: str | None = None

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def set_token(self, token: str) -> None:
        self._token = token

    def clear_token(self) -> None:
        self._token = None

    async def _test_token(self, token: str) -> httpx.Response:
        return await (await self._clients.get()).get(
            f"{DATACAT_API_BASE}/api/characters/recent-public",
            params={"limit": 1, "offset": 0, "summary": 1, "minTotalTokens": 889},
            headers={"X-Session-Token": token},
        )

    async def init(self, force: bool = False) -> dict[str, Any]:
        """dc-init: reuse the held token if it still validates, else identify
        a fresh anonymous session. Never raises -- failures come back as
        {"ok": False, "reason": ...} for the route to pass straight through."""
        if self._token and not force:
            try:
                check = await self._test_token(self._token)
                if check.status_code < 400:
                    return {"ok": True, "cached": True, "token": self._token}
            except httpx.HTTPError:
                pass
            self._token = None

        try:
            resp = await (await self._clients.get()).post(
                f"{DATACAT_API_BASE}/api/liberator/identify",
                json={"deviceToken": str(uuid.uuid4())},
            )
        except httpx.HTTPError as exc:
            return {"ok": False, "reason": str(exc)}
        if resp.status_code >= 400:
            return {"ok": False, "reason": f"identify returned {resp.status_code}: {resp.text[:200]}"}
        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "reason": "identify response was not JSON"}
        token = data.get("sessionToken")
        if not (data.get("success") and isinstance(token, str) and token):
            return {"ok": False, "reason": "identify response missing sessionToken"}
        self._token = token
        return {"ok": True, "token": token}

    async def validate(self) -> dict[str, Any]:
        """dc-validate: probe the held token against a cheap real endpoint.
        There is no dedicated "is this token valid" call on datacat.run, so
        this reuses the recent-public page-1 request."""
        if not self._token:
            return {"valid": False, "reason": "no token stored"}
        try:
            resp = await self._test_token(self._token)
        except httpx.HTTPError as exc:
            return {"valid": False, "reason": str(exc)}
        if resp.status_code >= 400:
            return {"valid": False, "reason": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return {"valid": True, "totalCount": data.get("totalCount", 0)}

    async def proxy_get(self, path: str, query: str) -> httpx.Response:
        """dc-proxy/{path}: authenticated GET passthrough for one of the
        allow-listed read-only paths. Raises DatacatSessionError for the
        route to translate into the matching HTTP status."""
        if not self._token:
            raise DatacatSessionError(401, "No DataCat session token configured")
        if not dc_proxy_path_allowed(path):
            raise DatacatSessionError(403, "Proxy path not allowed")
        url = f"{DATACAT_API_BASE}{path}"
        if query:
            url = f"{url}?{query}"
        try:
            client = await self._clients.get()
            return await client.get(url, headers={"X-Session-Token": self._token})
        except httpx.HTTPError as exc:
            raise DatacatSessionError(502, f"Failed to reach DataCat: {exc}") from exc

    async def _public_session_id(self) -> str | None:
        """A logged-in, non-background public session id datacat's extraction
        endpoint wants when the result should land on the public feed."""
        try:
            resp = await (await self._clients.get()).get(
                f"{DATACAT_API_BASE}/api/users", headers={"X-Session-Token": self._token}
            )
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        for user in data.get("users") or []:
            if not user.get("isPublic"):
                continue
            for session in user.get("sessions") or []:
                if session.get("purpose") != "BACKGROUND_SCRAPER" and session.get("status") == "logged_in":
                    return session.get("id")
        return None

    async def extract(
        self, url: str, *, public_feed: bool = True, always_reextract: bool = False
    ) -> tuple[int, dict[str, Any]]:
        """dc-extract: submit a JanitorAI or Saucepan character URL to
        datacat's extraction queue. Returns (status, body) rather than
        raising -- every branch here (missing session, bad URL, upstream
        failure) has its own status code the route answers with verbatim."""
        if not self._token:
            return 401, {"error": "No DataCat session token configured"}
        if not isinstance(url, str) or not url or len(url) > 512:
            return 400, {"error": "url string is required"}
        try:
            parsed = httpx.URL(url)
        except Exception:
            return 400, {"error": "Invalid URL"}
        host = parsed.host or ""
        is_janitor = bool(re.match(r"^(www\.)?janitorai\.com$", host, re.I)) or bool(
            re.match(r"^(www\.)?jannyai\.com$", host, re.I)
        )
        is_saucepan = bool(re.match(r"^(www\.)?saucepan\.ai$", host, re.I))
        if not is_janitor and not is_saucepan:
            return 400, {"error": "Only JanitorAI or Saucepan character URLs are supported"}
        path = parsed.path
        if is_janitor and not re.match(r"^/characters/[a-f0-9-]{8,64}(_[\w-]+)?/?$", path, re.I):
            return 400, {"error": "Invalid character URL path"}
        if is_saucepan and not re.match(r"^/companion/[a-f0-9-]{8,64}/?$", path, re.I):
            return 400, {"error": "Invalid character URL path"}
        extraction_kind = "saucepan" if is_saucepan else "janitor"

        request_id = str(uuid.uuid4())
        session_id = await self._public_session_id() if public_feed else None

        if extraction_kind == "saucepan":
            endpoint = f"{DATACAT_API_BASE}/api/saucepan-extract/run"
            body: dict[str, Any] = {
                "companion": url,
                "sourceKind": "one_off",
                "sourceRef": request_id,
                "includeSearch": True,
                "extractHidden": False,
                "idempotencyKey": request_id,
                "alwaysReextract": always_reextract,
                "vpnNamespace": "general_scraper",
                "netnsRole": "general_scraper",
            }
        else:
            endpoint = f"{DATACAT_API_BASE}/api/character/smart-extract-v2"
            body = {
                "url": url,
                "openLoginIfNoSession": True,
                "sessionId": session_id,
                "appearOnPublicFeed": bool(public_feed and session_id),
                "useSeparateWorkerServer": True,
                "inlinePostExtractCreatorProfile": True,
                "idempotencyKey": request_id,
                "extractSourceMode": "core_plus_janny",
                "alwaysReextract": always_reextract,
            }

        try:
            resp = await (await self._clients.get()).post(
                endpoint,
                json=body,
                headers={"X-Session-Token": self._token, "X-Request-Id": request_id},
            )
        except httpx.HTTPError:
            return 502, {"error": "Failed to reach DataCat"}
        try:
            data = resp.json()
        except ValueError:
            data = {"error": "DataCat returned a non-JSON response"}
        return resp.status_code, data

    async def close(self) -> None:
        await self._clients.aclose()
