"""External gallery extractors -- resolve a gallery *source* into direct image
URLs (or, for MEGA, into a reference the download step knows how to fetch and
decrypt itself).

docs/UI_REWRITE_PLAN.md §3.4 dropped all seven of `web/`'s extractors up front,
"to be reconsidered server-side later, on evidence of a real card that needs it".
Stage 6B is that reconsideration, and the evidence is a scan of all 3,868 cards:

    chub gallery    318 cards, 5,247 files already downloaded under the old
                              UI (`data/galleries/*/chubgallery_*`) -- see
                              `resolve_chub_gallery` below
    mega             61 cards
    catbox album     23 cards
    imgchest         19 cards
    imgbb album      15 cards
    postimg          11 cards
    gdrive            2 cards      dropped: negligible, and the one most
                                   dependent on browser session cookies
    imgbox            0 cards      dropped: not needed at all. Every card that
                                   mentions imgbox links direct
                                   `images2.imgbox.com/...` files, which the
                                   ordinary `embedded` phase already handles.

`civitai` was added later, on the same kind of evidence arriving the other way
round: cards whose galleries silently never downloaded. It is the case that
motivated `discovery.enumerate_sources` -- a Civitai post link is neither an
image URL nor (at the time) a known album page, so *nothing* recorded that it
had been seen, and no amount of re-running the pipeline would have surfaced it.
See `proxy/media/civitai.py`.

These are emphatically *not* the media pipeline -- they are one optional phase
of it. A card's own embedded and lorebook URLs never needed an extractor, which
is why localizing kept working after §3.4 dropped these.

**Two kinds of source, one name.** `chub` and `mega` don't fit the
URL-pattern-in-card-text model the other four use -- a Chub gallery is
triggered by the card's own `extensions.chub.id`, not a link anyone wrote, and
a MEGA folder needs its content decrypted (AES ECB/CBC/CTR via `proxy.media.mega`),
not just fetched. They're still extractors in the sense that matters: each one
turns "this card has a gallery source" into a list of images the ordinary
download pipeline can save. `resolve_chub_gallery` is called directly by
`_discovered_items` (keyed on the card's Chub link, not `find_gallery_urls`);
`mega` is registered here like the other four, via `Extractor.fetch` instead of
`Extractor.parse`, since listing a MEGA folder means calling MEGA's API rather
than scraping an HTML page.
"""

from __future__ import annotations

import html as html_module
import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Iterable

import httpx

from proxy.config import settings
from proxy.media import civitai as media_civitai, mega as media_mega
from proxy.state import ui_settings

logger = logging.getLogger("jai_proxy.media.extractors")

# Album pages only. A direct file URL (files.catbox.moe/x.png,
# i.postimg.cc/h/x.png, cdn.imgchest.com/files/x.webp, i.ibb.co/h/x.png) is
# already an image and belongs to the embedded phase -- matching it here would
# fetch the same bytes twice under a different prefix.
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


@dataclass(frozen=True)
class GalleryImage:
    url: str
    filename: str | None = None


# Parsing is pure (HTML in, images out) and fetching is the caller's job, so
# every extractor is testable against a fixture with no transport at all.
Parser = Callable[[str], list["GalleryImage"]]


@dataclass(frozen=True)
class Extractor:
    id: str
    pattern: re.Pattern[str]
    # Exactly one of the two is set: `parse` for the album-page extractors
    # (text already fetched by `resolve_gallery_urls`), `fetch` for a source
    # that has to talk to its own API instead of scraping HTML (mega).
    parse: Parser | None = None
    fetch: Callable[[httpx.Client, str], list["GalleryImage"]] | None = None


def _get_text(client: httpx.Client, url: str) -> str:
    response = client.get(url, follow_redirects=True)
    response.raise_for_status()
    return response.text


def _basename(url: str) -> str | None:
    tail = url.rsplit("/", 1)[-1].split("?")[0]
    return tail or None


def _dedupe(images: Iterable[GalleryImage]) -> list[GalleryImage]:
    seen: set[str] = set()
    out: list[GalleryImage] = []
    for image in images:
        if image.url not in seen:
            seen.add(image.url)
            out.append(image)
    return out


# ---- catbox ----------------------------------------------------------------

_CATBOX_FILE = re.compile(r">https://files\.catbox\.moe/([^<]+)<")


def _parse_catbox(text: str) -> list[GalleryImage]:
    """Album pages list their files as bare text links between tags."""
    return _dedupe(
        GalleryImage(f"https://files.catbox.moe/{name.strip()}", name.strip())
        for name in _CATBOX_FILE.findall(text)
    )


# ---- postimg ---------------------------------------------------------------

_POSTIMG_CARD = re.compile(
    r'data-hotlink="([^"]+)"\s+data-name="([^"]+)"\s+data-ext="([^"]+)"'
)


def _parse_postimg(text: str) -> list[GalleryImage]:
    images = []
    for hotlink, name, ext in _POSTIMG_CARD.findall(text):
        filename = f"{name}.{ext}"
        images.append(
            GalleryImage(f"https://i.postimg.cc/{hotlink}/{filename}", filename)
        )
    return _dedupe(images)


# ---- imgchest --------------------------------------------------------------

_IMGCHEST_DATA_PAGE = re.compile(r'data-page="([^"]+)"')
_IMGCHEST_CDN = re.compile(
    r"https?://cdn\.imgchest\.com/files/[^\s\"'<>]+?\.(?:png|jpe?g|gif|webp)",
    re.IGNORECASE,
)


def _parse_imgchest(text: str) -> list[GalleryImage]:
    """Prefer the embedded JSON blob; fall back to scraping CDN URLs.

    Both paths are kept because the legacy extractor kept both: the blob is
    complete and ordered, but its shape follows imgchest's frontend and has
    changed before, and the regex still finds the images when it does.
    """
    if "PostPassword" in text:
        # Password-gated post: nothing to resolve, and not an error worth
        # retrying either.
        return []

    match = _IMGCHEST_DATA_PAGE.search(text)
    if match:
        try:
            data = json.loads(html_module.unescape(match.group(1)))
            files = data.get("props", {}).get("post", {}).get("files") or []
            images = [
                GalleryImage(f["link"], _basename(f["link"]))
                for f in files
                if isinstance(f, dict) and isinstance(f.get("link"), str)
            ]
            if images:
                return _dedupe(images)
        except (ValueError, AttributeError, TypeError):
            pass

    return _dedupe(
        GalleryImage(found, _basename(found)) for found in _IMGCHEST_CDN.findall(text)
    )


# ---- imgbb -----------------------------------------------------------------

_IMGBB_DATA_OBJECT = re.compile(r"data-object='([^']+)'")
_IMGBB_OG_IMAGE = re.compile(r'<meta property="og:image" content="([^"]+)"')


def _parse_imgbb_objects(text: str) -> list[GalleryImage]:
    from urllib.parse import unquote

    images: list[GalleryImage] = []
    for raw in _IMGBB_DATA_OBJECT.findall(text):
        try:
            decoded = unquote(html_module.unescape(raw))
            obj = json.loads(decoded)
        except ValueError:
            continue
        image_url = (obj.get("image") or {}).get("url")
        if isinstance(image_url, str) and image_url:
            images.append(GalleryImage(image_url, _basename(image_url)))
    return images


def _parse_imgbb(text: str) -> list[GalleryImage]:
    """Album pages carry one `data-object` per image; a single-image page only
    has the og:image meta tag.

    The legacy extractor also walked imgbb's paginated JSON endpoint with a
    scraped auth token. That is not ported: it needs a token lifted out of an
    inline script, and page one already covers the album sizes these cards
    actually link.
    """
    images = _parse_imgbb_objects(text)
    if images:
        return _dedupe(images)

    match = _IMGBB_OG_IMAGE.search(text)
    if match and "//i.ibb.co/" in match.group(1):
        return [GalleryImage(match.group(1), _basename(match.group(1)))]
    return []


# ---- mega -------------------------------------------------------------------


def _fetch_mega(client: httpx.Client, url: str) -> list[GalleryImage]:
    return [GalleryImage(image.url, image.filename) for image in media_mega.extract_images(client, url)]


# ---- civitai ---------------------------------------------------------------


def _fetch_civitai(client: httpx.Client, url: str) -> list[GalleryImage]:
    """Like mega, an API rather than a page -- see `proxy/media/civitai.py` for
    why (two hosts merged, and a documented endpoint in place of the reference
    extractor's HTML scrape). The images it hands back are ordinary CDN URLs,
    so everything downstream of here is the plain download path."""
    return [
        GalleryImage(image.url, image.filename)
        for image in media_civitai.extract_images(client, url, _settings_key("civitaiApiKey"))
    ]


# ---- chub (first-party gallery, not a page link) -----------------------------

_CHUB_GALLERY_API = "https://gateway.chub.ai/api/gallery/project/{id}?limit=100&count=false"
_CHUB_ORIGIN = "https://chub.ai"
_CHUB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": _CHUB_ORIGIN,
    "Referer": f"{_CHUB_ORIGIN}/",
}


def _settings_key(name: str) -> str | None:
    """One root-level credential from `data/settings.json`, or None.

    The same flat keys the browser writes (Settings -> Providers), read the
    same narrow, defensive way `runtime.net` reads `httpProxyUrl`: a wrong
    type, an unparseable file or an unreadable disk all degrade to "no
    credential" rather than raising from inside a download loop. Two
    extractors need one of these (`chubToken`, `civitaiApiKey`), and a
    credential read with two implementations is one of them out of date.
    """
    try:
        blob = ui_settings.SettingsStore(settings.settings_file).read()
    except ui_settings.SettingsError:
        return None
    value = blob.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def resolve_chub_gallery(client: httpx.Client, project_id: str) -> list[GalleryImage] | None:
    """A Chub-sourced card's own first-party gallery -- Chub's hosted image
    feature for that character (`gateway.chub.ai/api/gallery/project/{id}`),
    not a link anyone wrote into the card's text. Triggered by
    `extensions.chub.id`, so unlike the other extractors it is called
    directly by `_discovered_items` rather than through
    `find_gallery_urls`/`resolve_gallery_urls`.

    None means "could not reach Chub", `[]` means "Chub has no gallery for
    this project" -- the same distinction `resolve_gallery_url` draws, and for
    the same reason: only the second is a fact the source ledger may record as
    settled."""
    if not project_id:
        return []
    headers = dict(_CHUB_HEADERS)
    token = _settings_key("chubToken")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = client.get(_CHUB_GALLERY_API.format(id=project_id), headers=headers)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("chub gallery fetch failed for project %s: %s", project_id, exc)
        return None

    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, list):
        return []
    images = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        image_url = node.get("primary_image_path")
        if isinstance(image_url, str) and image_url:
            images.append(GalleryImage(image_url, _basename(image_url)))
    return _dedupe(images)


EXTRACTORS: tuple[Extractor, ...] = (
    Extractor("catbox", re.compile(r"catbox\.moe/c/[a-zA-Z0-9]+"), parse=_parse_catbox),
    Extractor("postimg", re.compile(r"post(?:img|images)\.(?:cc|org)/gallery/[a-zA-Z0-9]+"), parse=_parse_postimg),
    Extractor("imgchest", re.compile(r"imgchest\.com/p/[a-zA-Z0-9]+"), parse=_parse_imgchest),
    Extractor("imgbb", re.compile(r"//(?:www\.)?ibb\.co(?:\.com)?/album/[a-zA-Z0-9]+"), parse=_parse_imgbb),
    Extractor("mega", re.compile(r"mega\.(?:nz|co\.nz)/folder/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+"), fetch=_fetch_mega),
    Extractor("mega", re.compile(r"mega\.(?:nz|co\.nz)/#F![A-Za-z0-9_-]+![A-Za-z0-9_-]+"), fetch=_fetch_mega),
    Extractor("civitai", media_civitai.URL_PATTERNS[0], fetch=_fetch_civitai),
    Extractor("civitai", media_civitai.URL_PATTERNS[1], fetch=_fetch_civitai),
)


def extractor_for(url: str) -> Extractor | None:
    for extractor in EXTRACTORS:
        if extractor.pattern.search(url):
            return extractor
    return None


def find_urls(text: str) -> list[str]:
    """Every http(s) URL in a block of card text, deduplicated, order preserved.

    Public because `discovery.enumerate_sources` needs the *unfiltered* list --
    a URL nothing here can handle still has to be seen and recorded, which is
    the difference between "this card has no more media" and "this card has
    media we couldn't fetch".
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_IN_TEXT.finditer(text):
        # Markdown and prose leave punctuation glued to the end of a URL --
        # including emphasis: `[**https://ibb.co/album/X**](...)` is a real
        # card's link text, and the `**` 404s. `_` is NOT stripped despite
        # also being emphasis: some image ids genuinely end in one.
        url = match.group(0).rstrip(".,;:!?)}]*")
        if url in seen:
            continue
        seen.add(url)
        found.append(url)
    return found


def find_gallery_urls(text: str) -> list[str]:
    """Album page URLs in a block of card text, deduplicated, order preserved."""
    return [url for url in find_urls(text) if extractor_for(url) is not None]


def resolve_gallery_urls(client: httpx.Client, urls: Iterable[str]) -> list[GalleryImage]:
    """Fetch each album page and resolve it to its images.

    Synchronous on purpose: the only callers are the media job's planner, which
    runs on a worker thread (`asyncio.to_thread`), and the submit route, which
    is a sync `def` on FastAPI's threadpool. Blocking there is correct; doing
    this on the event loop would stall every other request for the length of a
    whole-archive run.

    One failing album must not sink a card's media run -- these are third-party
    pages that go away, rate-limit, and change markup -- so a page that raises
    is logged and skipped.
    """
    out: list[GalleryImage] = []
    for url in urls:
        try:
            images = resolve_gallery_url(client, url)
        except GalleryGone:
            continue
        if images:
            out.extend(images)
    return _dedupe(out)


class GalleryGone(Exception):
    """The album host answered 404/410: the album is gone, not unreachable."""


_GONE_STATUSES = frozenset({404, 410})


def resolve_gallery_url(client: httpx.Client, url: str) -> list[GalleryImage] | None:
    """One album page's images, or None if the extractor could not reach it.

    The distinction the plural form throws away, and the one the source ledger
    needs: an album that resolves to nothing is a fact worth recording (the
    post was reached and is empty), whereas an album that raised is a
    transient nothing that must leave the card un-satisfied so the next run
    tries again.

    A 404/410 is neither, and raises `GalleryGone`: the album was reached and
    is permanently absent. Folding it into None is what kept a card with one
    deleted imgchest post re-resolving every gallery it has on every
    Localize all, forever.
    """
    extractor = extractor_for(url)
    if extractor is None:
        return None
    try:
        if extractor.fetch is not None:
            return _dedupe(extractor.fetch(client, url))
        return _dedupe(extractor.parse(_get_text(client, url)))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in _GONE_STATUSES:
            logger.info("gallery extractor %s: %s is gone (%d)", extractor.id, url, exc.response.status_code)
            raise GalleryGone(f"HTTP {exc.response.status_code}") from exc
        logger.warning("gallery extractor %s failed on %s: %s", extractor.id, url, exc)
        return None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("gallery extractor %s failed on %s: %s", extractor.id, url, exc)
        return None
