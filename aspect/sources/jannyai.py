"""Map a JannyAI (jannyai.com) character-card export onto the neutral fields the
CardBuilder consumes -- the fourth definition-only source path, structurally a
twin of datacat.

JannyAI is a JanitorAI-style card site; its PNG export embeds a chara_card_v3
whose provenance lives in `extensions.jannyai` (a distinct marker block, so it's
told apart from chub/datacat/native cards). Every JannyAI export observed shares
datacat's shape, which is why the import rebuilds it exactly the way a datacat
card is rebuilt:

  * No lorebook, no alternate greetings, no personality field. The whole
    definition sits in `description`; the single greeting is `first_mes`. So the
    card is rebuilt through the CardBuilder (macro sanitize, creator-notes
    de-HTML) with `book=None` -- there is nothing structural to preserve.
  * Macros intact. Definitions keep {{user}}/{{char}} as real macros (like
    datacat/saucepan, unlike the hidden-capture path), so there's no
    literal-persona-name reversal -- a straight open-card build. `creator_notes`
    arrives as raw HTML, run through the same clean_creator_notes normalizer.

The `extensions.jannyai` block carries the JannyAI character id, creator handle
(`creatorUsername`, an "@name"), URL slug, `pageName`, and `tagline` (the blurb).
`data.name` is the character name -- the same value the native path and the
other mappers key card filenames on.
"""

from __future__ import annotations

from typing import Any

from aspect.text.notes_html import clean_creator_notes
from aspect.cards.models import ProfileFields

# A JannyAI card originates from jannyai.com; reconstruct the canonical character
# URL from the id + slug so imported cards match the source_url/character_version
# convention of the other import paths.
_JANNYAI_CHARACTER_BASE = "https://jannyai.com/characters/"


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def jannyai_block(data: dict[str, Any]) -> dict[str, Any]:
    """The `extensions.jannyai` provenance block, or {} if absent."""
    block = (data.get("extensions") or {}).get("jannyai")
    return block if isinstance(block, dict) else {}


def is_jannyai(data: dict[str, Any]) -> bool:
    """Whether this card looks like a JannyAI export (has the jannyai block).
    Guards the import loop against a stray PNG that isn't one."""
    return bool(jannyai_block(data))


def card_id(data: dict[str, Any]) -> str:
    """The JannyAI character id (a UUID, from the jannyai block). Drives the
    `_<id8>` filename fragment and the acquired-detection glob."""
    return _s(jannyai_block(data).get("id"))


def name(data: dict[str, Any]) -> str:
    """The character name (`data.name`) -- the same value the native path and
    the other mappers key card filenames on. Mirrors datacat.name."""
    return _s(data.get("name"))


def creator(data: dict[str, Any]) -> str:
    """Creator handle, with the "@" sigil stripped so the on-disk folder matches
    the plain-username convention of the other sources. The jannyai block's
    `creatorUsername` is preferred, falling back to the card's own `creator`
    (they agree in practice); the raw "@handle" is preserved in provenance."""
    handle = _s(jannyai_block(data).get("creatorUsername")) or _s(data.get("creator"))
    return handle.lstrip("@")


def page_name(data: dict[str, Any]) -> str:
    """The JannyAI page name (the display name; the separate card-title blurb is
    carried as `tagline` in the jannyai block, preserved via provenance)."""
    return _s(jannyai_block(data).get("pageName")) or _s(data.get("name"))


def source_url(data: dict[str, Any]) -> str | None:
    """The canonical jannyai.com character URL (`<id>_<slug>`), or None if the
    card carries no id."""
    cid = card_id(data)
    if not cid:
        return None
    slug = _s(jannyai_block(data).get("slug"))
    tail = f"{cid}_{slug}" if slug else cid
    return f"{_JANNYAI_CHARACTER_BASE}{tail}"


def to_profile_fields(data: dict[str, Any]) -> ProfileFields:
    """Map a JannyAI card's `data` object onto ProfileFields. Like datacat, the
    whole definition lives in `description` (its `personality` field is unused),
    so that maps straight across; `creator_notes` is de-HTML'd."""
    return ProfileFields(
        name=_s(data.get("name")),
        creator=creator(data),
        tags=[t for t in (data.get("tags") or []) if isinstance(t, str) and t.strip()],
        description=_s(data.get("description")),
        scenario=_s(data.get("scenario")),
        mes_example=_s(data.get("mes_example")),
        creator_notes=clean_creator_notes(data.get("creator_notes") or ""),
    )


def greetings(data: dict[str, Any]) -> list[str]:
    """The card's greetings: the primary `first_mes` first, then any
    `alternate_greetings` (none observed in practice). Blank/dupe entries are
    dropped; the CardBuilder makes element 0 the first_mes."""
    out: list[str] = []
    for g in [data.get("first_mes"), *(data.get("alternate_greetings") or [])]:
        text = g if isinstance(g, str) else ""
        if text.strip() and text not in out:
            out.append(text)
    return out
