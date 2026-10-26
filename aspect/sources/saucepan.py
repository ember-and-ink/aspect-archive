"""Maps a raw saucepan.ai character export into the neutral shapes the shared
card machinery consumes -- the saucepan peer of `sources.janitor`.

Input is the thin JSON the (refactored) saucepan userscript fetches straight
from saucepan's API and posts to `/build-saucepan`, one object per character:

    {
      "id": "<companion uuid>",
      "definition": { "sections": [{title, content:<fragments>}, ...], "card": ... },
      "companion":  <GET /api/v2/companions/<id> response>,
      "lorebooks":  [ {id, list, chapters:[{index, title, text_fragments}, ...]}, ... ]
    }

Every long text field arrives obfuscated (see `saucepan_fragments`); this module
deobfuscates as it maps. Output types match sources.janitor exactly so both feed
the same CardBuilder -> PngWriter tail:

    to_profile_fields(raw) -> ProfileFields
    greetings(raw)         -> list[str]      (starting scenarios, blanks dropped)
    character_book(raw)    -> CharacterBook | None
    avatar_url(raw)        -> str

Field mapping (decided against real captures):
    name           <- companion.name          (display_name kept as page-name meta)
    creator        <- companion.author_handle
    tags           <- companion.tags
    description    <- "Companion Core" section (fallback full_description_fragments)
    mes_example    <- "Example Dialogue" section
    scenario       <- "Advanced Prompt" (raw), then every other non-core section
                      appended under a "--- <Title> ---" label. Scenario is the most
                      visible field in SillyTavern, so response-formatting / extra
                      authored sections land there rather than buried in notes.
    creator_notes  <- companion.short_description, then one markdown image per
                      companion.portraits[] entry (avatar md is prepended
                      downstream, so notes end up: avatar, blurb, gallery).
                      Portraits are saucepan's own image gallery; surfacing them
                      here as markdown lets the shared media pipeline pick them
                      up the same way it picks up the avatar, via the
                      markdown-image regex in aspect.media.discovery.

Macros ({{user}}/{{char}}) come back intact from the definition API -- unlike the
JanitorAI chat-relay path, nothing here is account-handle-substituted, so no
name reversal is needed.
"""

from __future__ import annotations

from typing import Any

from aspect.cards.models import CharacterBook, LoreEntry, ProfileFields
from aspect.sources.saucepan_fragments import deobfuscate_fragments as _deob

SAUCEPAN_ORIGIN = "https://saucepan.ai"

# Section titles with a dedicated home; everything else flows into scenario.
_CORE = "Companion Core"
_EXAMPLE = "Example Dialogue"
_ADVANCED = "Advanced Prompt"


def _companion(raw: dict[str, Any]) -> dict[str, Any]:
    """The inner companion object. `companion` is the whole
    `/api/v2/companions/<id>` response `{companion: {...}, is_favorited, ...}`;
    unwrap it, but tolerate a caller that already passed the inner object."""
    outer = raw.get("companion") or {}
    inner = outer.get("companion")
    return inner if isinstance(inner, dict) else outer


def _deob_sections(raw: dict[str, Any]) -> list[tuple[str, str]]:
    """Definition sections as ordered (title, deobfuscated-text) pairs. Order is
    load-bearing: it decides how extra sections stack into scenario."""
    definition = raw.get("definition") or {}
    sections = definition.get("sections") or []
    return [((s.get("title") or "").strip(), _deob(s.get("content"))) for s in sections]


def _build_scenario(sections: list[tuple[str, str]]) -> str:
    """Advanced Prompt leads (raw); every other non-core, non-example section is
    appended under a `--- <Title> ---` label so nothing authored is dropped and
    it all stays in SillyTavern's most visible field."""
    parts: list[str] = []
    advanced = next((v for t, v in sections if t == _ADVANCED), "").strip()
    if advanced:
        parts.append(advanced)
    for title, value in sections:
        if title in (_CORE, _EXAMPLE, _ADVANCED):
            continue
        value = value.strip()
        if value:
            parts.append(f"--- {title} ---\n{value}")
    return "\n\n".join(parts)


def portrait_urls(raw: dict[str, Any]) -> list[tuple[str, str]]:
    """`(caption, url)` pairs for companion.portraits[] -- saucepan's own image
    gallery, separate from the single card avatar. Uses the 'highres' CDN
    variant (full-size), matching what saved/rendered pages actually show.
    Skipped entirely when `unlocked_portraits` is False: a locked gallery's
    image ids resolve to paywalled/placeholder content, not the real picture."""
    comp = _companion(raw)
    if not comp.get("unlocked_portraits", True):
        return []
    out: list[tuple[str, str]] = []
    for portrait in comp.get("portraits") or []:
        if not isinstance(portrait, dict):
            continue
        image_id = (portrait.get("image") or {}).get("id")
        if not image_id:
            continue
        caption = (portrait.get("name") or "").strip()
        out.append((caption, f"{SAUCEPAN_ORIGIN}/cdn/{image_id}/highres"))
    return out


def to_profile_fields(raw: dict[str, Any]) -> ProfileFields:
    comp = _companion(raw)
    sections = _deob_sections(raw)
    by_title = {title: value for title, value in sections}

    description = by_title.get(_CORE, "").strip()
    if not description:
        # Hidden/locked definitions return a decoy "card" error instead of real
        # sections; the v2 public blurb is the only prose left to fall back on.
        description = _deob(comp.get("full_description_fragments")).strip()

    name = (comp.get("name") or "").strip()
    creator_notes = (comp.get("short_description") or "").strip()
    gallery = portrait_urls(raw)
    if gallery:
        images_md = "\n\n".join(f"![{caption or name}]({url})" for caption, url in gallery)
        creator_notes = f"{creator_notes}\n\n{images_md}".strip()

    return ProfileFields(
        name=name,
        creator=(comp.get("author_handle") or "").strip(),
        tags=[t for t in (comp.get("tags") or []) if isinstance(t, str)],
        description=description,
        scenario=_build_scenario(sections),
        mes_example=by_title.get(_EXAMPLE, "").strip(),
        creator_notes=creator_notes,
    )


def greetings(raw: dict[str, Any]) -> list[str]:
    """Starting scenarios in order -> greetings. saucepan ships "Blank" /
    "Choose Your Own Adventure!" placeholders as all-decoy fragment bags that
    reassemble to nothing; those are dropped."""
    comp = _companion(raw)
    out: list[str] = []
    for scenario in comp.get("starting_scenarios_fragments") or []:
        text = _deob(scenario.get("message")).strip()
        if text:
            out.append(text)
    return out


def character_book(raw: dict[str, Any], character_name: str = "") -> CharacterBook | None:
    """Fold every chapter of every attached lorebook into one V3 character_book,
    one entry per chapter (content = deobfuscated prose, key = lowercased title).
    saucepan chapters carry no trigger keywords, so the title is the only key we
    have. Returns None when nothing has content."""
    entries: list[LoreEntry] = []
    order = 0
    for lorebook in raw.get("lorebooks") or []:
        for chapter in lorebook.get("chapters") or []:
            content = _deob(chapter.get("text_fragments")).strip()
            if not content:
                continue
            title = (chapter.get("title") or f"Chapter {chapter.get('index')}").strip()
            order += 1
            entries.append(
                LoreEntry(
                    id=order,
                    keys=[title.lower()] if title else [],
                    content=content,
                    comment=title,
                    name=title,
                    insertion_order=order * 10,
                    enabled=True,
                )
            )
    if not entries:
        return None
    name = f"{character_name} Lorebook".strip() if character_name.strip() else ""
    return CharacterBook(name=name, entries=entries)


def avatar_url(raw: dict[str, Any]) -> str:
    image = _companion(raw).get("image") or {}
    image_id = image.get("id")
    return f"{SAUCEPAN_ORIGIN}/cdn/{image_id}/card" if image_id else ""


def companion_id(raw: dict[str, Any]) -> str:
    return (raw.get("id") or _companion(raw).get("id") or "").strip()


def creator_id(raw: dict[str, Any]) -> str:
    """The saucepan author's UUID (`author_id`, the `author_handle` sibling).
    Feeds extensions.datacat.creatorId -- same role as janitor.creator_id."""
    return (_companion(raw).get("author_id") or "").strip()


def page_name(raw: dict[str, Any]) -> str:
    """The card-title blurb (display_name, e.g. "Eve | I Did Nothing Wrong") --
    kept as metadata; the real character name is companion.name."""
    return (_companion(raw).get("display_name") or "").strip()


def is_open(raw: dict[str, Any]) -> bool:
    """Whether the definition is public (real sections available). saucepan's
    hidden cards return a decoy error in place of `definition.sections`."""
    return bool(_companion(raw).get("open_definition"))
