from __future__ import annotations

import re
from typing import Any

from proxy.text.tags import clean_tag
from proxy.text.notes_html import clean_creator_notes
from proxy.cards.models import ProfileFields

# JanitorAI avatars are served from this CDN prefix; the JSON carries only the
# bare filename in `avatar`.
_AVATAR_BASE = "https://ella.janitorai.com/bot-avatars/"

# Characters other than whitespace that carry no visible content: Unicode
# format marks (zero-width space/joiners, LRM/RLM, BiDi controls, word joiner)
# + BOM. A greeting that is only these -- e.g. Akane Kujo's LRM "separator"
# entry -- is not a real greeting and must not become an alternate greeting.
_INVISIBLE_RE = re.compile(r"[\s​-‏‪-‮⁠﻿]")

# Creators pad the greeting carousel with placeholder entries -- ".", "...", a
# stray line, an invisible separator. Across every real fixture the shortest
# genuine authored greeting is ~350 chars and the longest such placeholder is
# ~28, so a 100-char floor cleanly drops the junk without touching real
# greetings.
_MIN_GREETING_CHARS = 100


def _s(value: Any) -> str:
    """Coerce a possibly-missing/None JSON field to a stripped string. Hidden
    cards omit the definition keys entirely; open cards may carry None."""
    return value.strip() if isinstance(value, str) else ""


def _has_visible_content(s: str) -> bool:
    return bool(_INVISIBLE_RE.sub("", s or ""))


def _is_real_greeting(g: object) -> bool:
    return isinstance(g, str) and len(g) >= _MIN_GREETING_CHARS and _has_visible_content(g)


def is_hidden(character: dict[str, Any]) -> bool:
    """A creator-hidden card: the server withholds the definition and the
    primary greeting. `showdefinition is False` (not merely falsy) is the flag
    -- an open card has it True; a malformed/absent value is treated as open."""
    return character.get("showdefinition") is False


def tags(character: dict[str, Any]) -> list[str]:
    """Official emoji-prefixed `tags` first (emoji stripped), then the
    creator's `custom_tags`, deduped -- matching the real janitorai-export
    output order (pinned by test_janitor_mapper.test_akane_maps_to_trusted_reference_metadata)."""
    out: list[str] = []

    def add(raw: str) -> None:
        cleaned = clean_tag(raw)
        if cleaned and cleaned not in out:
            out.append(cleaned)

    for t in character.get("tags") or []:
        if isinstance(t, dict):
            add(_s(t.get("name")))
    for t in character.get("custom_tags") or []:
        if isinstance(t, str):
            add(t)
    return out


def avatar_url(character: dict[str, Any]) -> str | None:
    filename = _s(character.get("avatar"))
    return f"{_AVATAR_BASE}{filename}" if filename else None


def creator_id(character: dict[str, Any]) -> str:
    """The JanitorAI creator's UUID, carried in the JSON alongside
    `creator_name`. Feeds extensions.datacat.creatorId -- the field
    SillyTavern-CharacterLibrary's datacat provider keys creator lookups on."""
    return _s(character.get("creator_id"))


def to_profile_fields(character: dict[str, Any]) -> ProfileFields:
    """Map a JanitorAI /hampter/characters/<id> JSON payload onto the
    ProfileFields the CardBuilder already consumes. For hidden cards the
    definition fields (personality/scenario/example_dialogs) are absent, so
    they come back empty -- the CardBuilder then fills them from the chat
    capture. Everything else (name/creator/tags/creator_notes) is present in
    the JSON for open and hidden cards alike."""
    return ProfileFields(
        name=_s(character.get("chat_name")) or _s(character.get("name")),
        creator=_s(character.get("creator_name")),
        tags=tags(character),
        description=_s(character.get("personality")),
        scenario=_s(character.get("scenario")),
        mes_example=_s(character.get("example_dialogs")),
        creator_notes=clean_creator_notes(character.get("description") or ""),
    )


def greetings(character: dict[str, Any]) -> list[str]:
    """The card's greetings, already authored markdown. `first_messages` is the
    canonical list; `first_message` is a legacy single-greeting fallback.
    Nulls and content-free (invisible-only) entries are dropped.

    For a HIDDEN card `first_messages[0]` (the primary greeting shown on chat
    open) is nulled out by the server, so this returns only the alternate
    greetings -- the server prepends the primary from the chat capture."""
    first_messages = character.get("first_messages")
    if isinstance(first_messages, list):
        kept = [g for g in first_messages if _is_real_greeting(g)]
        if kept:
            return kept
    single = character.get("first_message")
    return [single] if _is_real_greeting(single) else []


# What a card keeps of each attached script. `message_count` is left out on
# purpose: it changes every time anyone chats, so it would date the card rather
# than describe the script.
_SCRIPT_FIELDS = ("id", "type", "title", "description", "is_public", "updated_at")


def script_manifest(character: dict[str, Any]) -> list[dict[str, Any]]:
    """Every script the character has attached, public or not, as
    `extensions.jai.scripts` records it.

    The character JSON lists them all -- private lorebooks and "advanced" code
    scripts included -- even though only public lorebooks can be fetched and
    embedded. Keeping the list is what lets the archive know a card carries a
    lorebook it holds no content for, and recognise the same lorebook on
    another card. DataCat mirrors the same `scripts` array, so its rows go
    through here too; its creator fields may be named either way."""
    scripts = character.get("scripts")
    out: list[dict[str, Any]] = []
    for script in scripts if isinstance(scripts, list) else []:
        if not isinstance(script, dict) or not _s(script.get("id")):
            continue
        row: dict[str, Any] = {}
        for key in _SCRIPT_FIELDS:
            value = script.get(key)
            if key == "is_public":
                row[key] = value is True
            elif isinstance(value, str):
                row[key] = value.strip()
        creator_id = _s(script.get("user_id")) or _s(script.get("creator_id"))
        creator_name = _s(script.get("user_name")) or _s(script.get("creator_name"))
        if creator_id:
            row["creator_id"] = creator_id
        if creator_name:
            row["creator_name"] = creator_name
        out.append(row)
    return out
