from __future__ import annotations

import re
from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Intake-side tag normalization -- syntactic only. No dictionary, no renames,
# no deletions: this is the one shared choke point every /build-* route runs
# tags through so the archive stops taking on new mess (see
# docs/PHASE_5_TAGS_PLAN.md §3). Merging/renaming existing vocabulary is a
# separate, curated, previewed pass (the tag manager) -- deliberately not
# here.
# ---------------------------------------------------------------------------


def clean_tag(t: str) -> str:
    """Strip a leading emoji / "#" / punctuation run from a tag chip's text
    so "\U0001f469‍\U0001f9b0 Female" -> "Female", "#ottergirl" -> "ottergirl".
    SillyTavern doesn't support emoji in tags, so the raw JanitorAI tag names
    (which are emoji-prefixed) must be cleaned before they land on a card."""
    t = re.sub(r"^[\s#]+", "", t)
    i = 0
    while i < len(t) and not t[i].isalnum():
        i += 1
    return t[i:].strip()


def normalize_tag(t: str) -> str:
    """One tag through the full syntactic pipeline: strip the leading
    emoji/#/punctuation run, then collapse internal whitespace runs to one
    space. Does NOT split on commas -- "Can Be Wholesome, Can Be Sexy" is a
    genuine single JanitorAI tag on hundreds of cards -- and does not touch
    casing, which is a merge decision, not a syntactic one."""
    return re.sub(r"\s+", " ", clean_tag(t)).strip()


def normalize_tags(tags: Iterable[str]) -> list[str]:
    """A card's tag list through §3 of the Phase 5 plan: clean each tag, drop
    what cleans down to empty, then dedupe case-insensitively within the card
    -- keeping the first occurrence's casing and original order."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            continue
        cleaned = normalize_tag(raw)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out
