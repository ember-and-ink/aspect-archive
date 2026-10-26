"""Media localization naming — verbatim port of the JS name derivation.

`web/library-sections/30-media-localization-feature.js` (`extractSanitizedUrlName`)
and `web/modules/media-dedup.js` (`mediaKey`, the prefix-priority ladder) build the
filenames the frontend has always saved localized media under:
`{prefix}_{index}_{name}.{ext}`. The on-the-fly URL replacement that makes
localized media actually *render* (`33-…:119,174-190`) reconstructs that same
name from a remote URL to find its local file. If this module sanitizes even
slightly differently from the JS, media downloads fine and then shows up as a
broken remote link across the whole archive — so every function here mirrors
its JS counterpart line for line, not just in spirit.

Keep in sync with the two JS files above; `tests/test_media_names.py` runs the
same cases through both.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# CDN URLs often end with a generic variant segment (/public, /original,
# /thumbnail). Verbatim copy of CDN_VARIANT_NAMES in
# 30-media-localization-feature.js:6-8 — keep the two lists identical.
CDN_VARIANT_NAMES = frozenset(
    {
        "public", "original", "raw", "full", "thumbnail", "thumb",
        "medium", "small", "large", "xl", "default", "image", "photo",
        "download", "view", "highres", "hires", "high", "lowres", "lores",
        "low", "preview", "avatar",
    }
)

_NON_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# JS's `new URL()` percent-encodes a handful of ASCII punctuation marks and
# every non-ASCII code point in the path before anything else touches it
# (WHATWG URL "path percent-encode set"). `urlsplit` does neither, so a raw
# unicode or control character in a URL's path would sanitize differently
# here than in the browser. Both encoders land on '_' either way once
# `_NON_NAME_CHARS` runs, EXCEPT that JS's percent-encoding turns one
# multi-byte character into several `%XX` triples -- each contributing its
# own run of substituted characters and eating into the 40-char cap
# differently. Reproduced here so the two stay byte-for-byte identical
# (verified against `new URL()` under node for the control-char/space/quote/
# angle-bracket/backtick/brace set and the full non-ASCII range).
_JS_PATH_PERCENT_ENCODE = frozenset(
    {0x22, 0x23, 0x3C, 0x3E, 0x60, 0x7B, 0x7D}  # " # < > ` { }
) | {c for c in range(0x00, 0x20) if c not in (0x09, 0x0A, 0x0D)} | {0x20, 0x7F}
# Tab/newline/CR are stripped from the whole URL by the parser, not encoded.
_JS_STRIPPED_CHARS = "\t\n\r"


def _whatwg_percent_encode_path(path: str) -> str:
    out: list[str] = []
    for ch in path:
        cp = ord(ch)
        if cp in _JS_PATH_PERCENT_ENCODE or cp > 0x7E:
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
        else:
            out.append(ch)
    return "".join(out)

# Filenames the pipeline writes: {prefix}_{index}_{name}.{ext}
# Verbatim copy of PREFIXED_NAME_RE in media-dedup.js:83.
PREFIXED_NAME_RE = re.compile(r"^(?:localized_media|lorebook_media|[a-z]+gallery)_[A-Za-z0-9]+_(.+)$")

# Mirrors PREFIX_PRIORITY in media-dedup.js:89 — a file already carrying a
# higher-priority prefix is never reclassified down to a lower one.
PREFIX_PRIORITY = {"localized_media": 4, "lorebook_media": 3, "extgallery": 2}

_BARE_GALLERY_PREFIX_RE = re.compile(r"^[a-z]+gallery_")

MIN_KEY_LENGTH = 4


def extract_sanitized_url_name(url: str) -> str:
    """Port of `extractSanitizedUrlName` (30-media-localization-feature.js:10-36).

    Derives a filesystem-safe name from a URL's last path segment, prepending
    the parent segment when the last segment is a generic CDN variant name.
    Returns '' on anything `urllib.parse` can't make sense of, matching the
    JS `try { new URL(url) } catch { return '' }`.
    """
    try:
        cleaned = "".join(c for c in url if c not in _JS_STRIPPED_CHARS).strip()
        parsed = urlsplit(cleaned)
        if not parsed.scheme or not parsed.netloc:
            return ""
        path = parsed.path
        if parsed.scheme in ("http", "https", "ws", "wss", "ftp", "file"):
            path = path.replace("\\", "/")
        path = _whatwg_percent_encode_path(path)
        path_parts = [p for p in path.split("/") if p]
        if not path_parts:
            return ""

        last_part = path_parts[-1]
        if "." in last_part:
            name_without_ext = last_part[: last_part.rindex(".")]
        else:
            name_without_ext = last_part
        sanitized = _NON_NAME_CHARS.sub("_", name_without_ext)[:40]

        if len(path_parts) >= 2 and sanitized.lower() in CDN_VARIANT_NAMES:
            parent = _NON_NAME_CHARS.sub("_", path_parts[-2])[:30]
            if len(parent) >= 4:
                return f"{parent}_{sanitized}"[:40]

        return sanitized
    except Exception:
        return ""


def media_key(name: str | None) -> str:
    """Port of `mediaKey` (media-dedup.js:101-105).

    Normalizes a bare filename into a dedup key: extension dropped, sanitized
    the same way `extract_sanitized_url_name` sanitizes URL-derived names,
    lowercased for lookup.
    """
    if not name or not isinstance(name, str):
        return ""
    if "." in name:
        base = name[: name.rindex(".")]
    else:
        base = name
    return _NON_NAME_CHARS.sub("_", base)[:40].lower()


def keys_for_item(url: str | None, filename: str | None = None) -> list[str]:
    """Port of `keysForItem` (media-dedup.js:118-125).

    Candidate dedup keys for one download, most specific first: the
    extractor-supplied filename (if any), then the URL-derived name. Both are
    checked because files saved before this module existed were named from
    the URL, while newer saves prefer the extractor's real filename.
    """
    keys: list[str] = []
    from_name = media_key(filename)
    if len(from_name) >= MIN_KEY_LENGTH:
        keys.append(from_name)
    from_url = media_key(extract_sanitized_url_name(url or ""))
    if len(from_url) >= MIN_KEY_LENGTH and from_url not in keys:
        keys.append(from_url)
    return keys


def prefix_priority(file_name: str) -> int:
    """Port of `prefixPriority` (media-dedup.js:140-145)."""
    for prefix, value in PREFIX_PRIORITY.items():
        if file_name.startswith(prefix + "_"):
            return value
    return 1 if _BARE_GALLERY_PREFIX_RE.match(file_name) else 0


def local_filename(prefix: str, index: int, name: str, ext: str) -> str:
    """The `{prefix}_{index}_{name}.{ext}` format the frontend has always
    written (30-…:1046 builds the base; archive-api.js:611-612 appends the
    extension on upload). `ext` is passed without its leading dot.
    """
    return f"{prefix}_{index}_{name}.{ext.lstrip('.')}"
