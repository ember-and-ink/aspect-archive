"""Map a datacat character-card export onto the neutral fields the CardBuilder
consumes -- the third source path alongside sources.janitor and saucepan.

datacat is a closed-source retriever that pulls JanitorAI cards very much the
way this project's own engine does, and writes the result straight into a PNG
as an embedded `chara`/`ccv3` character card (chara_card_v3). Bulk-grabbing a
bucket of those cards is faster than the send-a-chat-then-capture flow, so the
import pipeline re-homes them into the cards folder as `<name>_<id8>.png`.

Two facts about datacat exports shape the mapping:

  * No lorebook. datacat does not retrieve `character_book`; this project's
    retriever does. So imported cards simply carry no book (that's the tradeoff
    -- fine for the many hidden cards whose creators use no lorebook, and the
    reason a full retrieval of the same card is never overwritten by an import).
  * Macros intact. Like saucepan (and unlike this project's hidden-capture
    path), datacat definitions keep {{user}}/{{char}} as real macros, so there
    is no literal-persona-name reversal step -- a straight open-card build.
    `creator_notes` arrives as raw JanitorAI HTML, so it's run through the same
    clean_creator_notes normalizer every other source uses.

The card's `data.name` is the character name (the same value this project keys
on) and `extensions.datacat.{id,creatorName,pageName}` carry the JanitorAI
character id, creator, and page name.
"""

from __future__ import annotations

import json
import re
from typing import Any

from aspect.cards import pngtools
from aspect.text.notes_html import clean_creator_notes
from aspect.cards.models import ProfileFields

# A datacat card originates from JanitorAI; reconstruct the canonical character
# URL from the id so imported cards match the source_url/character_version
# convention of natively retrieved cards.
_JANITOR_CHARACTER_BASE = "https://janitorai.com/characters/"

# DataCat's own re-hosted avatar CDN for JanitorAI-sourced characters: bare
# filenames from `character.avatar` are relative to this. Mirrors
# DATACAT_JANITOR_IMAGE_BASE in web/modules/providers/datacat/datacat-api.js
# -- keep the two in sync.
DATACAT_JANITOR_IMAGE_BASE = "https://ella.janitorai.com/bot-avatars/"


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def extract_card(png: bytes) -> dict[str, Any] | None:
    """Return the embedded character-card `data` object from a datacat PNG, or
    None if the PNG carries no readable card. Thin alias for the shared
    pngtools.extract_embedded_card reader (datacat writes the same
    base64(JSON) `ccv3`/`chara` chunks every tavern card does)."""
    return pngtools.extract_embedded_card(png)


def datacat_block(data: dict[str, Any]) -> dict[str, Any]:
    """The `extensions.datacat` provenance block, or {} if absent."""
    block = (data.get("extensions") or {}).get("datacat")
    return block if isinstance(block, dict) else {}


def is_datacat(data: dict[str, Any]) -> bool:
    """Whether this card looks like a datacat export (has the datacat block).
    Guards the import loop against a stray PNG that isn't one."""
    return bool(datacat_block(data))


def card_id(data: dict[str, Any]) -> str:
    """The JanitorAI character id (from the datacat block). Drives the `_<id8>`
    filename fragment and the acquired-detection glob."""
    return _s(datacat_block(data).get("id"))


def name(data: dict[str, Any]) -> str:
    """The character name (`data.name`) -- the same value the native path and
    sources.chub key card filenames on. Mirrors chub.name."""
    return _s(data.get("name"))


def creator(data: dict[str, Any]) -> str:
    """Creator name -- the datacat block first, falling back to the card's own
    `creator` field (they agree in practice)."""
    return _s(datacat_block(data).get("creatorName")) or _s(data.get("creator"))


def creator_id(data: dict[str, Any]) -> str:
    """The upstream creator's UUID, carried in the datacat block. Feeds
    extensions.datacat.creatorId -- same role as janitor.creator_id /
    saucepan.creator_id."""
    return _s(datacat_block(data).get("creatorId"))


def page_name(data: dict[str, Any]) -> str:
    """The source page's listing title, out of the datacat block.

    Cards written by the browser-capture path carry the real listing title here
    (see `_v2_extensions_block`), the same blurb the native JanitorAI path
    stores. Older CharacterLibrary-written PNGs recorded the character name
    instead, and a card with no block at all falls back to it -- so this is
    "the best title we have", never blank for a card that has a name."""
    return _s(datacat_block(data).get("pageName")) or _s(data.get("name"))


def source_url(data: dict[str, Any]) -> str | None:
    cid = card_id(data)
    return f"{_JANITOR_CHARACTER_BASE}{cid}" if cid else None


def to_profile_fields(data: dict[str, Any]) -> ProfileFields:
    """Map a datacat card's `data` object onto ProfileFields. datacat puts the
    whole definition in `description` (its `personality` field is unused), so
    that maps straight across; `creator_notes` is de-HTML'd."""
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
    `alternate_greetings`. Blank/dupe entries are dropped; the CardBuilder makes
    element 0 the first_mes."""
    out: list[str] = []
    for g in [data.get("first_mes"), *(data.get("alternate_greetings") or [])]:
        text = g if isinstance(g, str) else ""
        if text.strip() and text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# Browser-capture V2 builder (Phase 3B, see docs/PHASE_3B_PLAN.md).
#
# Everything above this line maps an already-embedded datacat PNG's `data`
# object (the bulk file-import path, scripts/import_cards.py). Everything
# below builds that same V2 `data` shape from scratch out of the raw JSON the
# browser captures live from datacat.run's REST API -- the port of
# buildV2FromDatacat / buildV2FromDownload and their helpers in
# web/modules/providers/datacat/datacat-api.js. The result feeds straight
# back into to_profile_fields/greetings above, so the two paths converge on
# one mapping the moment the V2 dict exists.
# ---------------------------------------------------------------------------


# Pictographs, dingbats/misc symbols, regional-indicator flags -- plus the
# variation selector and ZWJ that glue multi-codepoint emoji together.
_EMOJI_RANGES = ((0x1F300, 0x1FAFF), (0x2600, 0x27BF), (0x1F1E6, 0x1F1FF))
_EMOJI_JOINERS = "\uFE0F\u200D"
_MARKER_BODY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ _")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _is_url(value: Any) -> bool:
    return isinstance(value, str) and bool(_URL_RE.match(value))


def _is_emoji_char(ch: str) -> bool:
    code = ord(ch)
    return ch in _EMOJI_JOINERS or any(lo <= code <= hi for lo, hi in _EMOJI_RANGES)


def _strip_emoji_prefix(text: str) -> str:
    """Drop a leading run of emoji and the whitespace after it; text that does
    not start with one comes back untouched."""
    end = 0
    while end < len(text) and _is_emoji_char(text[end]):
        end += 1
    return text[end:].lstrip() if end else text


def _is_marker(token: str) -> bool:
    """Whether `token` is exactly one `##[A-Z _]*(START|END)##` delimiter."""
    if len(token) < 4 or not (token.startswith("##") and token.endswith("##")):
        return False
    body = token[2:-2]
    return body.endswith(("START", "END")) and _MARKER_BODY_CHARS.issuperset(body)


def strip_datacat_markers(text: str | None) -> str:
    """Strip recovery-sourced `##DESCRIPTION START##`-style delimiter lines.
    /download bodies never carry these; content_variants recovery bodies do.

    One marker off the front, one off the back, found by position rather than
    by regex: this runs on text a stranger wrote, and the patterns it replaces
    (`[ \\t]*##...##\\s*$`, unanchored) went quadratic on a long run of tabs."""
    if not isinstance(text, str) or not text:
        return text or ""
    text = text.strip()
    if text.startswith("##"):
        close = text.find("##", 2)
        if close != -1 and _is_marker(text[: close + 2]):
            text = text[close + 2 :].strip()
    if text.endswith("##"):
        start = text.rfind("##", 0, len(text) - 2)
        if start != -1 and _is_marker(text[start:]):
            text = text[:start]
    return text.strip()


def resolve_tag_names(tags: Any) -> list[str]:
    """Plain tag name strings out of datacat's mixed tag shapes: JanitorAI
    emoji-prefixed {name,slug} objects, or Saucepan's plain slug strings."""
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for t in tags:
        if isinstance(t, str):
            name = t.strip()
        elif isinstance(t, dict):
            raw = _s(t.get("name")) or _s(t.get("slug"))
            name = _strip_emoji_prefix(raw).strip() or raw
        else:
            name = ""
        if name:
            out.append(name)
    return out


def pick_recovery_variant(character: dict[str, Any]) -> dict[str, Any]:
    """The active server-side recovery variant for a Saucepan hidden-definition
    character (DataCat's "Character Repair" job). {} when none is present, so
    callers can `.get()` off the result unconditionally."""
    variants = character.get("content_variants")
    if not isinstance(variants, list):
        return {}
    for v in variants:
        if isinstance(v, dict) and v.get("isPrimary") and not v.get("isRecoveryPlaceholder"):
            content = v.get("content")
            if isinstance(content, dict):
                return content
    return {}


def _companion_full_description(character: dict[str, Any]) -> str:
    for path in (
        (character.get("companion_snapshot") or {}),
        ((character.get("intercepted_chat_data") or {}).get("companion_snapshot") or {}),
    ):
        if isinstance(path, dict):
            desc = path.get("full_description")
            if isinstance(desc, str) and desc:
                return desc
    return ""


def extract_character_book_from_scripts(character: dict[str, Any]) -> dict[str, Any] | None:
    """V2 character_book out of character.scripts[] -- datacat stores lorebook
    entries JSON-encoded in script.script; private scripts are metadata stubs
    with no content. Multi-script merge: first script's title/settings win,
    entries concatenate. None when there's nothing usable, so this reads as
    "no lorebook" the same way a plain datacat file-import (no book at all)
    does."""
    scripts = character.get("scripts")
    if not isinstance(scripts, list) or not scripts:
        return None
    usable = [
        s for s in scripts
        if isinstance(s, dict) and s.get("type") == "lorebook" and s.get("is_public") and s.get("script")
    ]
    if not usable:
        return None

    all_entries: list[dict[str, Any]] = []
    for s in usable:
        try:
            parsed = json.loads(s["script"])
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, list):
            continue
        for e in parsed:
            if not isinstance(e, dict):
                continue
            key = e.get("key")
            if isinstance(key, list):
                keys = [str(k) for k in key]
            else:
                keys_raw = e.get("keysRaw")
                keys = [k.strip() for k in re.split(r",\s*", str(keys_raw)) if k.strip()] if keys_raw else []
            insertion_order = e.get("insertion_order")
            if not isinstance(insertion_order, (int, float)):
                insertion_order = e.get("priority") if isinstance(e.get("priority"), (int, float)) else 100
            all_entries.append({
                "keys": keys,
                "secondary_keys": [],
                "content": e.get("content") or "",
                "extensions": {},
                "enabled": e.get("enabled") is not False,
                "insertion_order": int(insertion_order),
                "case_sensitive": False,
                "name": e.get("name") or "",
                "id": e.get("id") if isinstance(e.get("id"), int) else len(all_entries),
                "comment": "",
                "selective": False,
                "constant": e.get("constant") is True,
                "position": "before_char",
            })
    if not all_entries:
        return None

    first = usable[0]
    scan_depth = 4
    settings_raw = first.get("settings")
    if isinstance(settings_raw, str) and settings_raw:
        try:
            parsed_settings = json.loads(settings_raw)
        except (TypeError, ValueError):
            parsed_settings = None
        if isinstance(parsed_settings, dict) and isinstance(parsed_settings.get("depth"), (int, float)):
            scan_depth = parsed_settings["depth"]

    return {
        "name": first.get("title") or "Lorebook",
        "description": first.get("description") or "",
        "scan_depth": scan_depth,
        "token_budget": 0,
        "recursive_scanning": False,
        "extensions": {},
        "entries": all_entries,
    }


def resolve_avatar_url(character: dict[str, Any] | None, *, prefer_original: bool = True) -> str | None:
    """The best avatar URL out of a datacat character-detail payload.

    `prefer_original`: the embedded V2 card's avatar (and, failing that, the
    hero variant) still points at the untouched source-CDN original; datacat's
    own top-level `avatar` is its re-hosted, downscaled copy. Mirrors
    resolveDatacatAvatarUrl({preferOriginal}) in datacat-api.js, minus the
    browser-only thumbnail-width and download-safety concerns."""
    if not character:
        return None

    def abs_only(candidate: Any) -> str | None:
        return candidate if _is_url(candidate) else None

    candidates: list[Any]
    if prefer_original:
        v2_data = ((character.get("chara_card_v2_json") or {}).get("data")) or {}
        variants = character.get("content_variants")
        variant0_avatar = None
        if isinstance(variants, list) and variants:
            variant0 = (variants[0] or {}).get("content") or {}
            variant0_v2 = (variant0.get("chara_card_v2_json") or {}).get("data") or {}
            variant0_avatar = variant0_v2.get("avatar")
        hero = (
            (character.get("avatar_variant_urls") or {}).get("hero")
            or (character.get("avatarVariantUrls") or {}).get("hero")
        )
        candidates = [abs_only(v2_data.get("avatar")), abs_only(variant0_avatar), hero, character.get("avatar")]
    else:
        candidates = [character.get("avatar")]

    for avatar in candidates:
        if not isinstance(avatar, str) or not avatar:
            continue
        return avatar if _is_url(avatar) else f"{DATACAT_JANITOR_IMAGE_BASE}{avatar}"
    return None


def hydrate_scripts_complete(character: dict[str, Any]) -> bool:
    """True when every public lorebook script the row advertises actually
    carries content. hydrateDatacatScripts (browser-side, per PHASE_3B_PLAN
    §5 B3) fetches the missing ones before POSTing; this is only a sanity
    check the server can log a warning from, never a fetch trigger -- the
    hampter script endpoint only accepts browser TLS fingerprints."""
    scripts = character.get("scripts")
    if not isinstance(scripts, list):
        return True
    for s in scripts:
        if isinstance(s, dict) and s.get("type") == "lorebook" and s.get("is_public") and not s.get("script"):
            return False
    return True


def normalized_source_kind(character: dict[str, Any]) -> str:
    """DataCat's `primary_content_source_kind` is not a clean two-value enum
    in practice -- a JanitorAI row can carry e.g. "janitor_core", not the bare
    "janitor" this project's extensions.datacat.sourceKind vocabulary expects
    (see server._datacat_block's docstring). Collapses to that vocabulary the
    same way buildV2FromDatacat's sourceKind local does in datacat-api.js:
    "saucepan" only for an exact match, "janitor" for everything else
    (including a missing/unknown value)."""
    return "saucepan" if character.get("primary_content_source_kind") == "saucepan" else "janitor"


def _v2_extensions_block(character: dict[str, Any]) -> dict[str, Any]:
    return {
        "datacat": {
            "id": character.get("character_id") or character.get("characterId"),
            "sourceKind": character.get("primary_content_source_kind"),
            "creatorId": character.get("creator_id") or character.get("creatorId"),
            "creatorName": character.get("creator_name") or character.get("creatorName"),
            # The listing title, which datacat mirrors from the source page into
            # `name` -- the character's own name is `chat_name`, and that is what
            # becomes data.name. Without this the block carries no pageName,
            # page_name() falls through to data.name, and the card records its
            # character name as its listing (a "Narrator" card whose page was
            # actually titled something loses the only title it had).
            "pageName": _s(character.get("name")),
        }
    }


def build_v2_from_character(character: dict[str, Any] | None) -> dict[str, Any] | None:
    """Port of buildV2FromDatacat(): a V2 character card straight from the
    /api/characters/:id detail payload. See that function's docstring in
    datacat-api.js for the full field-mapping rationale (source-dependent:
    JanitorAI rows carry the body in `personality`, Saucepan repair variants
    overload `description`)."""
    if not character:
        return None

    tag_names = resolve_tag_names(character.get("tags"))
    recovered = pick_recovery_variant(character)
    is_saucepan = character.get("primary_content_source_kind") == "saucepan"
    v2_data = ((character.get("chara_card_v2_json") or {}).get("data")) or {}

    if is_saucepan:
        description = (
            recovered.get("description") or recovered.get("personality")
            or v2_data.get("description") or character.get("description") or ""
        )
        scenario = recovered.get("scenario") or character.get("scenario") or v2_data.get("scenario") or ""
        first_message = recovered.get("first_message") or character.get("first_message") or v2_data.get("first_mes") or ""
        creator_notes = _companion_full_description(character) or v2_data.get("creator_notes") or ""
    else:
        description = (
            character.get("personality") or recovered.get("personality")
            or strip_datacat_markers(v2_data.get("description")) or ""
        )
        scenario = character.get("scenario") or recovered.get("scenario") or v2_data.get("scenario") or ""
        first_message = character.get("first_message") or recovered.get("first_message") or v2_data.get("first_mes") or ""
        creator_notes = character.get("description") or recovered.get("description") or v2_data.get("creator_notes") or ""

    alt_greetings: list[Any] = []
    for candidate in (character.get("alternate_greetings"), recovered.get("alternate_greetings"), v2_data.get("alternate_greetings")):
        if isinstance(candidate, list) and candidate:
            alt_greetings = candidate
            break

    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": character.get("chat_name") or character.get("chatName") or character.get("name") or "Unknown",
            "description": description,
            "personality": "",
            "scenario": scenario,
            "first_mes": first_message,
            "mes_example": v2_data.get("mes_example") or "",
            "system_prompt": v2_data.get("system_prompt") or "",
            "post_history_instructions": v2_data.get("post_history_instructions") or "",
            "creator_notes": creator_notes,
            "creator": character.get("creator_name") or character.get("creatorName") or "",
            "character_version": "1.0",
            "tags": tag_names,
            "alternate_greetings": alt_greetings,
            "extensions": _v2_extensions_block(character),
            "character_book": extract_character_book_from_scripts(character),
        },
    }
