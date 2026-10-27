"""How a card becomes a filename.

Two functions, kept apart from `builder` because they are the archive's naming
contract rather than part of building a card: the dedupe checks, the bulk-export
skip list, the gallery-folder resolver and the rename tooling all derive names
the same way, and none of them should have to import the builder to do it.

The filename is `<safe name>_<id fragment>.png`, and the fragment alone is the
identity -- see the dedupe rule in `aspect.cards.builder.PngWriter.find_by_id`.
"""

from __future__ import annotations

import re

_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def safe_filename(name: str) -> str:
    slug = _UNSAFE_FILENAME_RE.sub("_", name.strip()).strip("_")
    return slug or "unnamed"


def id_fragment(card_id: str | None) -> str:
    """A short, filename-safe slice of the card id used to disambiguate cards
    that share a creator and name. For a JanitorAI UUID this is the first
    segment (8 hex chars) -- collision-safe within a single creator's folder.
    Returns "" when there's no usable id, so the filename degrades to just the
    name."""
    token = _UNSAFE_FILENAME_RE.sub("", (card_id or "").strip())
    return token[:8]
