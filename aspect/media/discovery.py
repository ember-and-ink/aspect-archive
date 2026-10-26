"""Media URL discovery -- salvage item 1 of docs/UI_REWRITE_PLAN.md §1.3.

A verbatim port of `extractMediaUrls` and `collectCardTextChunks` /
`findCharacterMediaUrls` from
`web/library-sections/30-media-localization-feature.js`. Discovery moves
server-side with the rest of Stage 5 (§3.4): the client no longer scans a
card's own text for image URLs, it asks `POST /characters/{id}/media/scan`.

The regexes, their order, and the final "truncated twin" dedupe step are
copied exactly -- this is the piece that cost a session to get right the
first time (see the paren-handling comments below), so nothing here is
rederived. `tests/media/test_discovery.py` ports
`web/tests/media-urls.test.js` case for case as the acceptance suite.

Everything downstream of "here is a URL" -- the guard, the sniff, the
UNSUPPORTED_EXT_RE image-only policy -- already lives in `aspect.media.writer`
and is reused here rather than duplicated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from aspect.media.writer import UNSUPPORTED_EXT_RE

# Match ![](url) markdown -- allow one level of balanced parens in the path so
# postimg "(1)" filenames don't truncate at the first ), still stop at
# whitespace (sizing params/titles).
_MARKDOWN_RE = re.compile(r"!\[.*?\]\((https?://(?:[^\s()]|\([^\s()]*\))+)")

# Match <img src="url"> HTML format.
_HTML_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>')

# Match CSS url() patterns: background-image: url('...'), content: url("..."), etc.
_CSS_URL_RE = re.compile(r'url\(["\']?(https?://[^"\')\s]+\.(?:png|jpg|jpeg|gif|webp|svg))["\']?\)', re.I)

# Match raw URLs for media files. Parens terminate the match -- see the JS
# source's comment on the {{random:(a),(b)}} macro and postimg's "(1).png".
_BARE_URL_RE = re.compile(
    r'(https?://[^\s<>"{}|\\^`\[\]()]+\.(?:png|jpg|jpeg|gif|webp|svg)(?:/[^\s<>\'"{}|\\^`\[\]()]+)?)', re.I
)

_TEXT_FIELDS = (
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "creator_notes",
    "system_prompt",
    "post_history_instructions",
)


def _is_archivable(url: str) -> bool:
    return not UNSUPPORTED_EXT_RE.search(urlsplit(url).path)


def extract_media_urls(text: str | None) -> list[str]:
    """The URLs embedded in one blob of card prose, in first-seen order."""
    if not text:
        return []

    urls: list[str] = []

    for m in _MARKDOWN_RE.finditer(text):
        url = m.group(1)
        # An unbalanced-paren URL makes the balanced branch swallow the
        # markdown closer; give it back -- unless what follows the match is
        # itself another ')', in which case the trailing ')' was real.
        end = m.end()
        if url.endswith(")") and (end >= len(text) or text[end] != ")"):
            url = url[:-1]
        urls.append(url)

    for m in _HTML_IMG_RE.finditer(text):
        src = m.group(1)
        if src.startswith("http"):
            urls.append(src)

    for m in _CSS_URL_RE.finditer(text):
        urls.append(m.group(1))

    for m in _BARE_URL_RE.finditer(text):
        urls.append(m.group(1))

    unique = list(dict.fromkeys(u for u in urls if _is_archivable(u)))
    # A proper prefix of a longer capture continuing with '/' is a truncated
    # twin the raw pattern makes of ext-mid-path urls.
    return [u for u in unique if not any(o != u and o.startswith(u) and o[len(u)] == "/" for o in unique)]


def collect_card_text_chunks(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """`(main, lorebook)` -- the same surfaces `collectCardTextChunks` walks,
    over a card's V2 `data` dict (as `aspect.cards.edit.read_card` returns
    it, not the pydantic model -- a raw dict survives fields the archive
    doesn't otherwise validate)."""
    main: list[str] = []
    lorebook: list[str] = []

    for field in _TEXT_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value:
            main.append(value)

    extensions = data.get("extensions")
    if isinstance(extensions, dict):
        for provider_data in extensions.values():
            tagline = provider_data.get("tagline") if isinstance(provider_data, dict) else None
            if isinstance(tagline, str) and tagline:
                main.append(tagline)

    alt_greetings = data.get("alternate_greetings")
    if isinstance(alt_greetings, list):
        for greeting in alt_greetings:
            if isinstance(greeting, str) and greeting:
                main.append(greeting)

    entries = (data.get("character_book") or {}).get("entries")
    if entries:
        entry_list = entries if isinstance(entries, list) else list(entries.values())
        for entry in entry_list:
            content = entry.get("content") if isinstance(entry, dict) else None
            if isinstance(content, str) and content:
                lorebook.append(content)

    return main, lorebook


def find_character_media_urls(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """`(embedded, lorebook)` remote media URLs found in a card, deduped
    within each list and against each other (a URL in both surfaces counts
    only as embedded)."""
    main_chunks, lorebook_chunks = collect_card_text_chunks(data)

    embedded: list[str] = []
    seen: set[str] = set()
    for chunk in main_chunks:
        for url in extract_media_urls(chunk):
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                embedded.append(url)

    lorebook: list[str] = []
    for chunk in lorebook_chunks:
        for url in extract_media_urls(chunk):
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                lorebook.append(url)

    return embedded, lorebook


# --------------------------------------------------------------------------
# Source enumeration -- every media source a card carries, classified, offline
# --------------------------------------------------------------------------
#
# The three walks above answer "which image URLs does this card have". That is
# strictly less than "what media does this card have", and the gap is what let
# Civitai-linked cards look finished for months: their gallery link is neither
# an image URL nor -- until `media/civitai.py` -- a known album page, so no
# caller ever produced it and nothing ever recorded that it existed.
#
# `enumerate_sources` closes that by classifying *every* http(s) URL a card
# carries against what this build can actually do with it, plus the one source
# that isn't a link (a Chub-sourced card's own first-party gallery). It is pure
# regex over strings -- no HTTP, no disk -- which is what makes it usable as the
# planner's skip check across the whole archive.

READY = "ready"
UNHANDLED = "unhandled"
IGNORED = "ignored"

_IGNORED_REASON = "audio/video not archived"


@dataclass(frozen=True)
class SourceRef:
    """One media source on a card, and what we can do about it *now*.

    `key` is the URL, or `chub:<project id>` for the Chub gallery. `handler` is
    `embedded`/`lorebook` for a direct image, an extractor id for a gallery, or
    None when nothing here can fetch it -- and that None, recorded, is what
    makes a card re-arm when a handler for it later exists (see
    `manifest.sources_satisfied`).
    """

    key: str
    handler: str | None
    status: str
    reason: str | None = None


def enumerate_sources(data: dict[str, Any]) -> list[SourceRef]:
    """Every media source on one card, first-seen order, deduped by key.

    Ordering matters for classification, not for output: a URL is an image
    first (the embedded/lorebook phases), an album page second, and unhandled
    only if it is neither. `extract_media_urls` and `find_gallery_urls` stay
    the authorities for those first two, so nothing here re-derives what
    surface a URL belongs to.
    """
    # Imported here rather than at module scope: `extractors` imports `mega`,
    # which imports `guard`, and `extractors` also reads settings -- none of
    # which this module's other callers need pulled in.
    from aspect.media import extractors

    main_chunks, lorebook_chunks = collect_card_text_chunks(data)
    embedded, lorebook = find_character_media_urls(data)

    handlers: dict[str, str] = {}
    for url in embedded:
        handlers[url] = "embedded"
    for url in lorebook:
        handlers[url] = "lorebook"
    for url in extractors.find_gallery_urls("\n".join([*main_chunks, *lorebook_chunks])):
        handlers.setdefault(url, extractors.extractor_for(url).id)

    refs: list[SourceRef] = []
    seen: set[str] = set()

    def add(ref: SourceRef) -> None:
        if ref.key not in seen:
            seen.add(ref.key)
            refs.append(ref)

    for chunk in [*main_chunks, *lorebook_chunks]:
        for url in extractors.find_urls(chunk):
            handler = handlers.get(url)
            if handler is not None:
                add(SourceRef(url, handler, READY))
            elif UNSUPPORTED_EXT_RE.search(urlsplit(url).path):
                # Seen and deliberately skipped -- the images-only policy, not
                # a failure. Recorded so that a future policy change re-arms
                # exactly these cards.
                add(SourceRef(url, None, IGNORED, _IGNORED_REASON))
            else:
                add(SourceRef(url, None, UNHANDLED))

    # A URL the image regexes found but the bare-URL scan cannot see again --
    # a CSS `url()` value, or an `<img src>` inside an attribute the plain
    # pattern stops short of. Never dropped just because the second pass reads
    # the text differently from the first.
    for url, handler in handlers.items():
        add(SourceRef(url, handler, READY))

    from aspect.sources import chub as chub_source

    project_id = chub_source.card_id(data)
    if project_id:
        add(SourceRef(f"chub:{project_id}", "chub", READY))

    return refs
