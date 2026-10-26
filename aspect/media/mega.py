"""MEGA folder galleries -- server-side, ported from
`web/modules/gallery-extractors/mega.js`.

MEGA encrypts everything client-side, so unlike the extractors in
`media/extractors.py` (which resolve an album *page* to a plain, directly
fetchable image URL), this has to speak MEGA's own API and decrypt content
itself:

  1. Unwrap the folder's node keys (AES-128 ECB) to find each file's key.
  2. Decrypt each file's attributes (AES-128 CBC, zero IV) to recover its
     real filename, so non-images are filtered before anything is fetched.
  3. Fetch the encrypted bytes and decrypt them (AES-128 CTR).

Listing (steps 1-2) happens once per card, synchronously, at discovery time --
mirrors `extractors.resolve_gallery_urls`. Fetching + decrypting (step 3)
happens per file, at actual download time, inside the async job pipeline:
`writer.download_item` recognizes a `mega://` pseudo-URL and calls
`fetch_and_decrypt` here instead of doing a plain GET. The pseudo-URL carries
the file's key, CTR nonce and declared size in its query string, so nothing
needs threading through the job queue's plain `{"url":..., "filename":...}`
item dicts -- the URL alone is enough to redo the download later, same as
every other item.
"""

from __future__ import annotations

import base64
import json
import logging
import random
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx
from Crypto.Cipher import AES
from Crypto.Util import Counter

from aspect.media import guard as media_guard

logger = logging.getLogger("aspect.media.mega")

MEGA_API = "https://g.api.mega.co.nz/cs"

_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif"}

# A folder can list far more than a card needs; matches the JS extractor's cap.
MAX_FILES = 100


class MegaApiError(Exception):
    pass


@dataclass(frozen=True)
class MegaImage:
    url: str  # a `mega://` pseudo-URL -- see module docstring
    filename: str


@dataclass(frozen=True)
class _PseudoUrl:
    folder_id: str
    handle: str
    file_key: bytes
    nonce: bytes
    size: int


# ---- MEGA's own base64 (URL-safe, unpadded) --------------------------------


def _b64_decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


# ---- Folder URL + pseudo-URL ------------------------------------------------


def parse_folder_url(url: str) -> tuple[str, bytes] | None:
    """`(folder_id, master_key)`, or None if `url` isn't a MEGA folder link.

    Supports both `mega.nz/folder/{id}#{key}` and the legacy
    `mega.nz/#F!{id}!{key}`."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None

    if parsed.path.startswith("/folder/") and parsed.fragment:
        folder_id = parsed.path[len("/folder/") :].split("/", 1)[0]
        if not folder_id:
            return None
        try:
            return folder_id, _b64_decode(parsed.fragment)
        except Exception:
            return None

    if parsed.fragment.startswith("F!"):
        parts = parsed.fragment[2:].split("!")
        if len(parts) >= 2 and parts[0]:
            try:
                return parts[0], _b64_decode(parts[1])
            except Exception:
                return None
    return None


def is_mega_url(url: str) -> bool:
    return url.startswith("mega://")


def _build_pseudo_url(folder_id: str, handle: str, file_key: bytes, nonce: bytes, size: int) -> str:
    return f"mega://{folder_id}/{handle}?k={_b64_encode(file_key)}&n={_b64_encode(nonce)}&s={size}"


def _parse_pseudo_url(url: str) -> _PseudoUrl | None:
    if not is_mega_url(url):
        return None
    parsed = urlsplit(url)
    folder_id = parsed.netloc
    handle = parsed.path.lstrip("/")
    query = parse_qs(parsed.query)
    if not folder_id or not handle:
        return None
    try:
        file_key = _b64_decode(query["k"][0])
        nonce = _b64_decode(query["n"][0])
        size = int(query["s"][0])
    except (KeyError, IndexError, ValueError):
        return None
    return _PseudoUrl(folder_id, handle, file_key, nonce, size)


# ---- Key hierarchy + attribute decrypt --------------------------------------


def _try_decrypt_node_key(node: dict, key_map: dict[str, bytes]) -> bytes | None:
    for entry in (node.get("k") or "").split("/"):
        sep = entry.find(":")
        if sep < 1:
            continue
        parent_key = key_map.get(entry[:sep])
        if parent_key is None:
            continue
        try:
            enc = _b64_decode(entry[sep + 1 :])
            if not enc or len(enc) % 16 != 0:
                continue
            return AES.new(parent_key, AES.MODE_ECB).decrypt(enc)
        except Exception:
            continue
    return None


def _derive_file_key(dec_key: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(dec_key[:16], dec_key[16:32]))


def _decrypt_attributes(key: bytes, attr_data: bytes) -> dict | None:
    if not attr_data or len(attr_data) % 16 != 0:
        return None
    try:
        decrypted = AES.new(key, AES.MODE_CBC, iv=bytes(16)).decrypt(attr_data)
    except ValueError:
        return None
    text = decrypted.decode("utf-8", errors="ignore")
    if not text.startswith("MEGA{"):
        return None
    try:
        return json.loads(text[4:].rstrip("\x00"))
    except ValueError:
        return None


# ---- MEGA API ----------------------------------------------------------------

_api_seq = random.randint(0, 0xFFFFFFFF)


def _next_seq() -> int:
    global _api_seq
    _api_seq += 1
    return _api_seq


def _mega_api_sync(client: httpx.Client, folder_id: str, commands: list[dict]) -> list:
    url = f"{MEGA_API}?id={_next_seq()}&n={folder_id}"
    response = client.post(url, content=json.dumps(commands))
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, list) or not result:
        raise MegaApiError("MEGA API: empty response")
    if isinstance(result[0], int):
        raise MegaApiError(f"MEGA API error {result[0]}")
    return result


async def _mega_api_async(client: httpx.AsyncClient, folder_id: str, commands: list[dict]) -> list:
    url = f"{MEGA_API}?id={_next_seq()}&n={folder_id}"
    response = await client.post(url, content=json.dumps(commands))
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, list) or not result:
        raise MegaApiError("MEGA API: empty response")
    if isinstance(result[0], int):
        raise MegaApiError(f"MEGA API error {result[0]}")
    return result


async def _get_download_url(client: httpx.AsyncClient, folder_id: str, handle: str) -> str | None:
    result = await _mega_api_async(client, folder_id, [{"a": "g", "g": 1, "n": handle}])
    url = (result[0] or {}).get("g")
    if not url:
        return None
    # MEGA hands out http:// storage hosts; they all speak TLS, and this
    # server's own safety guard refuses a plain http:// download target.
    return "https://" + url[len("http://") :] if url.lower().startswith("http://") else url


# ---- Listing (discovery time, sync) ------------------------------------------


def extract_images(client: httpx.Client, folder_url: str) -> list[MegaImage]:
    """Everything a MEGA folder link resolves to: list it, walk the key
    hierarchy, decrypt each file's name, keep the images. One failing folder
    must not sink a card's media run, so every MEGA-specific failure is
    caught and logged rather than raised."""
    parsed = parse_folder_url(folder_url)
    if parsed is None:
        return []
    folder_id, master_key = parsed
    if len(master_key) != 16:
        return []

    try:
        result = _mega_api_sync(client, folder_id, [{"a": "f", "c": 1, "r": 1}])
    except (httpx.HTTPError, MegaApiError, ValueError) as exc:
        logger.warning("mega folder %s: list failed: %s", folder_id, exc)
        return []

    nodes = (result[0] or {}).get("f") or []
    if not nodes:
        return []

    handle_set = {n.get("h") for n in nodes}
    key_map: dict[str, bytes] = {}
    for n in nodes:
        if (n.get("t") or 0) >= 1 and n.get("p") not in handle_set:
            key_map[n["h"]] = master_key
            break

    passes, max_passes, resolving = 0, len(nodes), True
    while resolving:
        resolving = False
        passes += 1
        if passes > max_passes:
            break
        for n in nodes:
            if n.get("t") != 1 or n.get("h") in key_map:
                continue
            dec = _try_decrypt_node_key(n, key_map)
            if dec and len(dec) == 16:
                key_map[n["h"]] = dec
                resolving = True

    images: list[MegaImage] = []
    for n in nodes:
        if n.get("t") != 0:
            continue
        node_key = _try_decrypt_node_key(n, key_map)
        if not node_key or len(node_key) < 32:
            continue
        file_key = _derive_file_key(node_key)
        try:
            attr_data = _b64_decode(n.get("a") or "")
        except Exception:
            continue
        attrs = _decrypt_attributes(file_key, attr_data)
        filename = attrs.get("n") if attrs else None
        if not isinstance(filename, str) or not filename:
            continue
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in _IMAGE_EXTENSIONS:
            continue

        handle = n.get("h")
        nonce = node_key[16:24]
        pseudo_url = _build_pseudo_url(folder_id, handle, file_key, nonce, n.get("s") or 0)
        images.append(MegaImage(pseudo_url, filename))
        if len(images) >= MAX_FILES:
            break

    return images


# ---- Fetch + decrypt (download time, async) ---------------------------------


async def fetch_and_decrypt(client: httpx.AsyncClient, pseudo_url: str) -> tuple[bytes | None, str | None, str | None]:
    """`(body, content_type, error)` -- same contract as `writer._fetch`, for a
    `mega://` pseudo-URL. Two network round trips: MEGA's API for a signed,
    short-lived download URL, then the encrypted bytes themselves.

    `content_type` is always None -- `writer.finish_item`'s sniff step is the
    one source of truth for what was actually downloaded (see writer.py's
    module docstring, "sniffing never trusts the source"); a MIME guessed
    from the decrypted filename would only ever be a second, redundant guess.
    """
    parsed = _parse_pseudo_url(pseudo_url)
    if parsed is None:
        return None, None, "invalid mega reference"

    try:
        download_url = await _get_download_url(client, parsed.folder_id, parsed.handle)
    except (httpx.HTTPError, MegaApiError) as exc:
        return None, None, str(exc)
    if not download_url:
        return None, None, "MEGA API: no download URL"

    safety = media_guard.is_url_safe_for_download(download_url)
    if not safety.ok:
        return None, None, safety.reason

    try:
        async with client.stream("GET", download_url, follow_redirects=True) as response:
            if response.status_code >= 400:
                return None, None, f"HTTP {response.status_code}"
            try:
                encrypted = await media_guard.read_body_with_cap(response, media_guard.MAX_MEDIA_BYTES)
            except media_guard.MediaTooLargeError as exc:
                return None, None, str(exc)
    except httpx.HTTPError as exc:
        return None, None, str(exc)

    counter = Counter.new(64, prefix=parsed.nonce, initial_value=0)
    decrypted = AES.new(parsed.file_key, AES.MODE_CTR, counter=counter).decrypt(encrypted)
    if parsed.size and len(decrypted) > parsed.size:
        decrypted = decrypted[: parsed.size]

    return decrypted, None, None
