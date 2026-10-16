"""Re-fetching a card's original image from its source.

The repair for a card whose avatar arrived low-res -- Discover saved Chub's
200x200 `avatar.webp` thumbnail from 2026-08-19 until 2026-10-16 -- or was
edited down since. Only the pixels change: `edit.replace_avatar` re-embeds the
card's own data untouched, so this refreshes the art and never the content.
What comes back is the source's *current* art; a creator who has since replaced
their image gets the new one, which is the point of a refresh.

It replaces only on a clear improvement. A download that fails, is not an
image, is itself thumbnail-sized, or is no larger than what the card already
has leaves the card exactly as it was -- `AvatarFetcher`'s placeholder fallback
is deliberately not used, since that would trade a small face for a grey box.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from PIL import Image

from proxy.cards import edit, pngtools
from proxy.config import settings
from proxy.runtime import net
from proxy.sources.chub import CHUB_AVATAR_BASE
from proxy.sources.datacat_client import DatacatImageResolver

# Below this on *both* sides an avatar is a thumbnail, not a portrait. Chub's
# `avatar.webp` is 200x200; every card from every other path clears it.
LOW_RES_PX = 250

Origin = Literal["chub", "datacat"]


class RefetchError(Exception):
    """The card was left alone, and this says why."""


@dataclass(frozen=True)
class Refetched:
    url: str
    before: tuple[int, int]
    after: tuple[int, int]


def is_low_res(width: int, height: int) -> bool:
    return 0 < width < LOW_RES_PX and 0 < height < LOW_RES_PX


def _ids(data: dict[str, Any]) -> tuple[str, str]:
    extensions = data.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    chub = extensions.get("chub")
    datacat = extensions.get("datacat")
    full_path = chub.get("full_path") if isinstance(chub, dict) else None
    datacat_id = datacat.get("id") if isinstance(datacat, dict) else None
    return (
        full_path if isinstance(full_path, str) else "",
        datacat_id if isinstance(datacat_id, str) else "",
    )


def origin(data: dict[str, Any]) -> Origin | None:
    """Where this card's original image can be asked for, without asking.

    Chub cards carry `extensions.chub.full_path`, which names the original
    upload directly. JanitorAI, saucepan and DataCat cards carry
    `extensions.datacat.id`, which DataCat's API turns into the untouched
    source-CDN URL. JannyAI and hand-dropped cards carry neither."""
    full_path, datacat_id = _ids(data)
    if full_path:
        return "chub"
    if datacat_id:
        return "datacat"
    return None


_resolver: DatacatImageResolver | None = None
_resolver_lock = threading.Lock()


def _datacat_resolver() -> DatacatImageResolver:
    # One for the process, so its anonymous session token is reused across
    # refetches. `persist=False`: a server request has no business rewriting .env.
    global _resolver
    with _resolver_lock:
        if _resolver is None:
            _resolver = DatacatImageResolver(persist=False)
        return _resolver


def resolve_url(data: dict[str, Any]) -> str | None:
    """The full-resolution original's URL -- a network call for DataCat."""
    full_path, datacat_id = _ids(data)
    if full_path:
        # The original upload. `avatar.webp` beside it is the 200px thumbnail.
        return f"{CHUB_AVATAR_BASE}{full_path}/chara_card_v2.png"
    if datacat_id:
        return _datacat_resolver().resolve(datacat_id)
    return None


def download(url: str) -> bytes | None:
    try:
        with net.sync_client(timeout=settings.request_timeout, follow_redirects=True) as client:
            resp = client.get(url)
    except httpx.HTTPError:
        return None
    return resp.content if resp.status_code < 400 else None


def refetch(
    path: Path,
    *,
    resolve: Callable[[dict[str, Any]], str | None] | None = None,
    fetch: Callable[[str], bytes | None] | None = None,
) -> Refetched:
    """Give the card at `path` its source's full-resolution image, or raise
    RefetchError and leave the file untouched."""
    resolve = resolve or resolve_url
    fetch = fetch or download
    _, data = edit.read_card(path)
    before = pngtools.image_size(path.read_bytes())

    url = resolve(data)
    if not url:
        raise RefetchError("no original image can be found for this card")
    raw = fetch(url)
    if not raw:
        raise RefetchError(f"could not download {url}")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            size = image.size
    except (OSError, ValueError, Image.DecompressionBombError):
        raise RefetchError(f"{url} is not an image") from None

    if max(size) < LOW_RES_PX:
        raise RefetchError(f"the source only has a {size[0]}×{size[1]} image -- no better than a thumbnail")
    if max(size) <= max(before):
        raise RefetchError(
            f"the source image ({size[0]}×{size[1]}) is no larger than the card's ({before[0]}×{before[1]})"
        )

    edit.replace_avatar(path, raw)
    return Refetched(url=url, before=before, after=pngtools.image_size(path.read_bytes()))
