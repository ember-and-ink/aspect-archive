from __future__ import annotations

import base64
import io

import httpx
from PIL import Image

from aspect.config import settings
from aspect.runtime import net


def _valid_image(data: bytes) -> bool:
    try:
        Image.open(io.BytesIO(data)).verify()
        return True
    except Exception:
        return False


def _make_placeholder() -> bytes:
    img = Image.new("RGBA", (256, 256), (60, 60, 70, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


PLACEHOLDER_PNG = _make_placeholder()


class AvatarFetcher:
    """Fetches a character's avatar image. JanitorAI avatars are public CDN
    URLs, so a plain GET is enough -- no auth, no GM_xmlhttpRequest needed
    server-side. Falls back to a base64 avatar the userscript may have sent
    (e.g. if the URL fetch fails), and finally to a small placeholder PNG so
    a card is always exportable."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Held rather than built: `aspect.deps` constructs one of these at import,
        # so a client built here would pin the proxy setting to whatever it was
        # when the process started. See aspect.runtime.net.AsyncClientHolder.
        self._clients = net.AsyncClientHolder(client, timeout=settings.request_timeout)

    async def fetch(self, url: str | None, avatar_b64: str | None = None) -> bytes:
        if url:
            data = await self._try_url(url)
            if data is not None:
                return data
        if avatar_b64:
            data = self._try_b64(avatar_b64)
            if data is not None:
                return data
        return PLACEHOLDER_PNG

    async def _try_url(self, url: str) -> bytes | None:
        try:
            resp = await (await self._clients.get()).get(url)
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        return resp.content if _valid_image(resp.content) else None

    def _try_b64(self, avatar_b64: str) -> bytes | None:
        try:
            data = base64.b64decode(avatar_b64)
        except Exception:
            return None
        return data if _valid_image(data) else None

    async def aclose(self) -> None:
        await self._client.aclose()
