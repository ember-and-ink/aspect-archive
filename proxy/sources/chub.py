"""Map a Chub.ai (chub / charhub) character-card PNG export onto a clean,
SillyTavern-ready chara_card_v3 -- the fourth source path alongside
sources.janitor, sources.saucepan and datacat.

Unlike the JanitorAI paths, a Chub export is *already* a well-formed
chara_card_v3, carrying its own lorebook (`character_book`) and a rich
`extensions` block (a `chub` metadata block, a `depth_prompt` author's-note
injection, `fav`). So the import is deliberately a light touch -- a passthrough
that keeps the card almost verbatim and only:

  * sanitizes macros (typo + broken-bracket repair, pronoun fold) in the
    definition text fields and lorebook-entry content -- macros stay intact,
    there's no persona-name reversal (Chub is a fully open card, like datacat),
  * tames `creator_notes` -- Chub wraps the blurb in a page-sized styled shell
    with a `<style>` CSS block SillyTavern warns about, so the sheet is inlined
    and dropped while the layout it described survives (see notes_html), and
  * strips emoji/hash noise from tags.

Everything else is preserved AS IS: `extensions` (untouched -- including the
`depth_prompt` prompt and the `chub` block), `system_prompt`,
`post_history_instructions`, `character_version`, and the whole
`character_book`. Crucially that includes lorebook-entry fields our own
LoreEntry model doesn't carry (`priority`, `probability`, `selectiveLogic`) and
Chub's mixed int-or-string `position`. That's why the mapping edits the raw
parsed dict in place rather than round-tripping through the pydantic card models
-- a round-trip would silently drop those extras and coerce the int positions.

Identification + naming: a Chub card is recognized by `extensions.chub`. Its
`data.name`/`data.creator` are the character name and creator, and
`extensions.chub.id` (a unique numeric id, not a hash) drives the
`<name>_<id>.png` filename layout and the acquired-detection glob.
"""

from __future__ import annotations

import copy
from typing import Any

from proxy.text.notes_html import clean_creator_notes
from proxy.text.macros import MacroSanitizer
from proxy.text.tags import normalize_tags

# Reconstruct the canonical Chub character URL from the card's full_path so an
# imported card can record a source_url the way natively retrieved cards do.
_CHUB_CHARACTER_BASE = "https://chub.ai/characters/"

# Chub.ai's own avatar CDN convention; mirrors CHUB_AVATAR_BASE in
# frontend/src/lib/providers/chub.ts -- keep the two in sync.
CHUB_AVATAR_BASE = "https://avatars.charhub.io/avatars/"

# The chara_card_v3 fields carrying authored prose + macros. Sanitized in place;
# every other top-level field is left exactly as Chub wrote it.
_TEXT_FIELDS = (
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "system_prompt",
    "post_history_instructions",
)


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def chub_block(data: dict[str, Any]) -> dict[str, Any]:
    """The `extensions.chub` metadata block, or {} if absent."""
    block = (data.get("extensions") or {}).get("chub")
    return block if isinstance(block, dict) else {}


def is_chub(data: dict[str, Any]) -> bool:
    """Whether this embedded card is a Chub export (has the `chub` block).
    Guards the import loop against a PNG that isn't one."""
    return bool(chub_block(data))


def card_id(data: dict[str, Any]) -> str:
    """Chub's unique character id (numeric, e.g. "5655941" -- not a hash).
    Drives the `_<id>` filename fragment and the acquired-detection glob."""
    cid = chub_block(data).get("id")
    return str(cid).strip() if cid not in (None, "") else ""


def name(data: dict[str, Any]) -> str:
    return _s(data.get("name"))


def creator(data: dict[str, Any]) -> str:
    return _s(data.get("creator"))


def page_name(data: dict[str, Any]) -> str:
    """Chub's display page name (falls back to the character name)."""
    return _s(chub_block(data).get("pageName")) or _s(data.get("name"))


def source_url(data: dict[str, Any]) -> str | None:
    full_path = _s(chub_block(data).get("full_path"))
    return f"{_CHUB_CHARACTER_BASE}{full_path}" if full_path else None


def avatar_url(node: dict[str, Any]) -> str | None:
    """The full-resolution avatar for a raw Chub API node. `avatar.webp`
    (what `avatar_url` names) is a 200x200 thumbnail -- fine for a browse
    tile, never for the card. `chara_card_v2.png` is the original upload;
    its embedded card chunks are dropped when the writer re-encodes it."""
    full_path = _s(node.get("fullPath"))
    return (
        _s(node.get("max_res_url"))
        or (f"{CHUB_AVATAR_BASE}{full_path}/chara_card_v2.png" if full_path else None)
        or _s(node.get("avatar_url"))
        or None
    )


def build_v2_from_chub(
    node: dict[str, Any], linked_lorebook: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the V2 `data` object clean_card() consumes, straight from the raw
    Chub API node (GET /api/characters/{fullPath}?full=true) -- the same shape
    the browser's importCharacter posts to /build-chub. `linked_lorebook` is
    the raw response of GET /api/v4/projects/{id}/repository/files/raw%252Fcard.json/raw
    (the linked-project lorebook resolved via the git API), or None when the
    card has no `related_lorebooks`.

    Port of chub-api.js:buildCharacterCardFromChub -- field-for-field, keep
    the two in sync. `node.definition.extensions.chub` already carries `id`/
    `full_path` (Chub bakes its own provenance into every export), so no
    extra id-stamping is needed here; the caller only layers `extensions.jai`
    on top, same as every other source."""
    definition = node.get("definition") or {}

    character_book = definition.get("embedded_lorebook")
    if linked_lorebook:
        book = linked_lorebook.get("character_book")
        if not book:
            book = (linked_lorebook.get("data") or {}).get("character_book")
        if isinstance(book, dict) and book.get("entries"):
            character_book = book

    def_extensions = definition.get("extensions") or {}
    chub_ext = {
        **(def_extensions.get("chub") or {}),
        "tagline": node.get("tagline") or definition.get("tagline") or "",
        # The listing title -- the node's own `name`, which is the page heading
        # and *not* `definition.name`, the character name that becomes data.name
        # (the page "Your Bully Wants To Be Your Sex Slave?!" holds a character
        # called "Autumn"). Chub bakes no pageName into its own export, so it
        # exists only on the node and is lost unless stamped here. The key is
        # the one CharacterLibrary's chub provider writes, so page_name() reads
        # one place for both a live capture and a re-imported PNG.
        "pageName": node.get("name") or definition.get("project_name") or "",
    }

    full_path = node.get("fullPath") or ""

    return {
        "name": definition.get("name") or node.get("name") or "Unknown",
        "description": definition.get("personality") or "",
        "personality": definition.get("tavern_personality") or "",
        "scenario": definition.get("scenario") or "",
        "first_mes": definition.get("first_message") or "",
        "mes_example": definition.get("example_dialogs") or "",
        "creator_notes": definition.get("description") or "",
        "system_prompt": definition.get("system_prompt") or "",
        "post_history_instructions": definition.get("post_history_instructions") or "",
        "alternate_greetings": definition.get("alternate_greetings") or [],
        "tags": node.get("topics") or [],
        "creator": full_path.split("/")[0] if full_path else "",
        "character_version": definition.get("character_version") or "",
        "extensions": {**def_extensions, "chub": chub_ext},
        "character_book": character_book,
    }


def clean_card(
    data: dict[str, Any], sanitizer: MacroSanitizer
) -> tuple[dict[str, Any], list[str]]:
    """Return a cleaned deep copy of the parsed Chub `data` object plus any
    unresolved-macro warnings.

    Sanitizes the definition text fields and lorebook-entry content, converts
    `creator_notes` HTML to markdown, and cleans tags. Everything else --
    extensions, character_book structure and entry extras, system_prompt /
    post_history_instructions macros aside -- is carried through unchanged."""
    card = copy.deepcopy(data)
    warnings: list[str] = []

    def scrub(text: str) -> str:
        cleaned, unknown = sanitizer.sanitize(text)
        for macro_name in unknown:
            w = f"unresolved macro: {{{{{macro_name}}}}}"
            if w not in warnings:
                warnings.append(w)
        return cleaned

    for field in _TEXT_FIELDS:
        if isinstance(card.get(field), str):
            card[field] = scrub(card[field])

    if isinstance(card.get("alternate_greetings"), list):
        card["alternate_greetings"] = [
            scrub(g) if isinstance(g, str) else g for g in card["alternate_greetings"]
        ]

    # creator_notes: a laid-out Chub blurb keeps its structure (minus the CSS),
    # plainer notes flatten to markdown; then sanitize any macros in the prose.
    card["creator_notes"] = scrub(clean_creator_notes(card.get("creator_notes") or ""))

    # tags: intake normalizer -- strip leading emoji/"#", collapse whitespace,
    # drop empties, dedupe case-insensitively. See proxy/text/tags.py.
    if isinstance(card.get("tags"), list):
        card["tags"] = normalize_tags(card["tags"])

    # Lorebook entry *content* carries the same macros; sanitize only the prose,
    # leaving keys / positions / probabilities / ids exactly as Chub wrote them.
    book = card.get("character_book")
    if isinstance(book, dict):
        for entry in book.get("entries") or []:
            if isinstance(entry, dict) and isinstance(entry.get("content"), str):
                entry["content"] = scrub(entry["content"])

    return card, warnings


def to_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap a cleaned Chub `data` object in the canonical chara_card_v3
    envelope -- spec header plus the top-level V2 mirror -- the exact structure
    CharacterCardV3.to_dict emits, so PngWriter.write_payload embeds it the same
    way it embeds a natively built card."""
    payload = {"spec": "chara_card_v3", "spec_version": "3.0", "data": data}
    payload.update(data)  # V2-compat top-level mirror
    return payload
