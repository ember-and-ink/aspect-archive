"""Civitai post/image galleries -- server-side, ported from
`~/workspaces/SillyTavern-CharacterLibrary/modules/gallery-extractors/civitai.js`.

Civitai was one of the seven extractors `docs/UI_REWRITE_PLAN.md` §3.4 dropped
up front, "to be reconsidered server-side later, on evidence of a real card
that needs it". The evidence turned up the plain way: cards whose galleries
simply never downloaded. A Civitai link is invisible to *both* halves of
discovery -- `https://civitai.com/posts/1981754` has no image extension, so
`media/discovery.py`'s regexes skip it, and until this module existed it
matched no `EXTRACTORS` pattern either. Not a failed fetch, not a dead URL: a
URL nothing ever looked at.

Like `media/mega.py`, this lives outside `media/extractors.py` because it
speaks an API rather than scraping one page, and is registered there with
`Extractor.fetch` instead of `Extractor.parse`. Unlike MEGA, what it returns
are ordinary `https://image.civitai.com/...` URLs the normal download path
fetches with a plain GET -- no pseudo-URL, no decryption. (That CDN 301s to
`image-b2.civitai.com`; `writer._fetch` already follows redirects.)

**Two hosts, merged.** `civitai.red` is the NSFW-inclusive mirror. It can
expose images hidden on the main site, while the main site can expose SFW
content the mirror filters out, so both are queried and the results merged by
image id -- exactly as the JS did. One host failing must never sink the other.

**Which API call.** The reference repo states the public API cannot look up an
image by id, and scrapes the `/images/{id}` HTML page instead. That is no
longer true (verified 2026-09-04): `?imageId={id}` answers with exactly that
one image. So both URL shapes go through the documented endpoint, and the HTML
scrape is kept only as a fallback for when the API returns nothing -- a regex
over someone else's markup is the thing you want *second*.

**Auth.** Public content needs none (also verified, `nsfw=X` included). A key
in `data/settings.json`'s root `civitaiApiKey` is sent as a bearer token when
present, which is what private/hidden content needs. The reference kept the
key in cl-helper's memory behind four routes because a browser extractor
cannot read a server's config; here the settings blob already *is* the config,
so there is nothing to set up.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("aspect.media.civitai")

# The mirror first would be equally correct; `.com` is listed first only so the
# merge below keeps the main site's metadata on an image both hosts return.
HOSTS = ("civitai.com", "civitai.red")

# Verbatim from the JS extractor -- a browser UA, because Civitai's edge serves
# a challenge page to obviously-scripted clients.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# A post can hold more than a card needs; matches PER_POST_LIMIT in the JS.
PER_POST_LIMIT = 200

POST_PATH_RE = re.compile(r"^/posts/(\d+)", re.IGNORECASE)
IMAGE_PATH_RE = re.compile(r"^/images/(\d+)", re.IGNORECASE)

# Whole-URL patterns, for `extractors.EXTRACTORS`. Deliberately *not* matching
# `image.civitai.com/...` -- those are direct files the embedded phase already
# handles -- nor `/models/{id}`, whose images belong to a model version rather
# than to anything the card linked.
URL_PATTERNS = (
    re.compile(r"civitai\.(?:com|red)/posts/\d+", re.IGNORECASE),
    re.compile(r"civitai\.(?:com|red)/images/\d+", re.IGNORECASE),
)

_CDN_URL_RE = re.compile(r"https://image\.civitai\.com/[^\s\"'<>)\\]+", re.IGNORECASE)
# https://image.civitai.com/{identityPrefix}/{uuid}/{variant}/{filename}
_CDN_UUID_RE = re.compile(r"image\.civitai\.com/[^/]+/([a-f0-9-]{8,})/", re.IGNORECASE)
_ORIGINAL_RE = re.compile(r"original=true", re.IGNORECASE)
_WIDTH_RE = re.compile(r"width=(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class CivitaiImage:
    url: str
    filename: str


def is_civitai_url(url: str) -> bool:
    return any(pattern.search(url) for pattern in URL_PATTERNS)


def _headers(api_key: str | None, *, accept: str) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _get(client: httpx.Client, url: str, api_key: str | None, *, accept: str) -> httpx.Response | None:
    """One request that is allowed to fail. Every caller queries two hosts and
    merges, so a host that 500s, rate-limits or blocks us contributes nothing
    rather than raising through the other one's results."""
    try:
        response = client.get(url, headers=_headers(api_key, accept=accept), follow_redirects=True)
        response.raise_for_status()
        return response
    except httpx.HTTPError as exc:
        logger.debug("civitai %s failed: %s", url, exc)
        return None


def _api_items(client: httpx.Client, host: str, query: str, api_key: str | None) -> list[dict]:
    response = _get(client, f"https://{host}/api/v1/images?{query}", api_key, accept="application/json")
    if response is None:
        return []
    try:
        data = response.json()
    except ValueError as exc:
        logger.debug("civitai %s returned non-JSON: %s", host, exc)
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _from_api(client: httpx.Client, query: str, api_key: str | None) -> list[CivitaiImage]:
    """Both hosts, merged by the item's own id (falling back to its URL for an
    item that somehow has none), first host to report an image wins."""
    by_id: dict[object, CivitaiImage] = {}
    for host in HOSTS:
        for item in _api_items(client, host, query, api_key):
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            # The images-only policy's discovery point (the other two are
            # `writer.UNSUPPORTED_EXT_RE` and the sniff): a post's videos never
            # enter the item list, rather than being fetched and then dropped.
            kind = item.get("type")
            if isinstance(kind, str) and kind.lower() != "image":
                continue
            key = item.get("id") or url
            if key in by_id:
                continue
            by_id[key] = CivitaiImage(url, _filename_for(url, key))
    return list(by_id.values())


def _from_html(client: httpx.Client, image_id: str, api_key: str | None) -> list[CivitaiImage]:
    """Fallback for `/images/{id}` when the API answers with nothing: scrape
    the page for CDN URLs and keep the largest variant of each image.

    Kept from the JS extractor because it is the only path that survives the
    API refusing an image the logged-out page still renders. Everything about
    it -- matching markup, guessing at variants -- is why it is the fallback.
    """
    by_uuid: dict[str, str] = {}
    for host in HOSTS:
        response = _get(client, f"https://{host}/images/{image_id}", api_key, accept="text/html")
        if response is None:
            continue
        for match in _CDN_URL_RE.finditer(response.text):
            url = match.group(0).rstrip(".,;:!?)}]")
            uuid_match = _CDN_UUID_RE.search(url)
            if uuid_match is None:
                continue
            uuid = uuid_match.group(1)
            current = by_uuid.get(uuid)
            if current is None or _prefer_larger(url, current):
                by_uuid[uuid] = url
    return [CivitaiImage(url, _filename_for(url, uuid)) for uuid, url in by_uuid.items()]


def _prefer_larger(candidate: str, current: str) -> bool:
    """Port of `preferLarger`: `original=true` beats every sized variant, and
    among sized variants the larger `width=N` wins."""
    candidate_original = bool(_ORIGINAL_RE.search(candidate))
    current_original = bool(_ORIGINAL_RE.search(current))
    if candidate_original != current_original:
        return candidate_original
    candidate_width = _WIDTH_RE.search(candidate)
    current_width = _WIDTH_RE.search(current)
    return int(candidate_width.group(1) if candidate_width else 0) > int(
        current_width.group(1) if current_width else 0
    )


def _filename_for(url: str, fallback_key: object) -> str:
    tail = urlsplit(url).path.rsplit("/", 1)[-1]
    if tail and re.search(r"\.[a-z0-9]{2,5}$", tail, re.IGNORECASE):
        return tail
    return f"civitai_{fallback_key}.jpeg"


def extract_images(client: httpx.Client, url: str, api_key: str | None = None) -> list[CivitaiImage]:
    """Every image behind one Civitai post or image URL.

    Synchronous for the same reason `extractors.resolve_gallery_urls` is: the
    callers are the media job's planner (on a worker thread) and the submit
    route (on FastAPI's threadpool).
    """
    path = urlsplit(url).path
    post = POST_PATH_RE.match(path)
    if post is not None:
        return _from_api(client, f"postId={post.group(1)}&limit={PER_POST_LIMIT}&nsfw=X", api_key)

    image = IMAGE_PATH_RE.match(path)
    if image is None:
        return []
    images = _from_api(client, f"imageId={image.group(1)}&nsfw=X", api_key)
    return images or _from_html(client, image.group(1), api_key)
