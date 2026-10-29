"""Read-only previews of a provider card, before it is anything on disk.

Discover lets you look at a card on Chub or DataCat and decide whether to keep
it. Deciding means reading it -- description, greetings, lorebook, creator notes
-- and that means mapping the provider's payload into a tavern card. That
mapping already exists, in Python, in `aspect.sources.chub` and
`aspect.sources.datacat`, and it is *the same code* `/build-chub` and
`/build-datacat` run when the card is actually written.

So the preview runs it too, rather than the browser re-implementing it in
TypeScript. The point is not to save the typing: it is that a second mapper
would drift, and a preview that disagrees with what lands in the archive is
worse than no preview. Everything below the mapper call is shared with the
build routes by construction -- `catalog.summarize_data` is the counting the
archive applies to a card on disk, called here on a card that is not.

The preview itself writes nothing, fetches no avatar and checks no duplicates.
The browser already knows what it holds (`POST /characters/have`), and
acquiring a card is a separate, deliberate action.

The one write below is `/discover/seen`: when each Following card first came
back, which is what "Recent only" filters on and the only part of browsing
that has nowhere else to live. The JanitorAI userscript writes its own
`janitor` bucket through the same route, from every tile it renders. See
`aspect.state.seen`.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from aspect import deps
from aspect.api.build import build_card
from aspect.archive import catalog
from aspect.cards.models import CharacterBook
from aspect.config import settings
from aspect.sources import chub, datacat
from aspect.state import seen as seen_state

logger = logging.getLogger("aspect.api.discover")

router = APIRouter()

SAUCEPAN_ORIGIN = "https://saucepan.ai"

# The buckets `/discover/seen` will write: the two providers Discover browses,
# plus `janitor`, stamped by the JanitorAI userscript from the tiles it renders
# (its own "Recent only"). The store itself is bucket-agnostic (a fourth
# provider's rows survive a read), but a typo reaching it would silently create
# a bucket nothing reads.
Provider = Literal["chub", "datacat", "janitor"]

# Ids accepted in one request. A request is one whole Following feed -- Chub's
# timeline plus 24 per followed author, DataCat's 50 per followed creator -- so
# this sits well above a large follow list and only bounds a client bug.
MAX_IDS_PER_REQUEST = 20_000


class DiscoverPreviewIn(BaseModel):
    """What the browser captured on the provider, unchanged.

    The fields mirror `ChubBuildRequest` / `DatacatBuildRequest` exactly, minus
    the write-only ones (`avatar_b64`, `gallery_id`), so the same captured
    payload can be previewed and then built without being re-fetched or
    reshaped.
    """

    provider: Literal["chub", "datacat"]
    # -- chub
    node: dict[str, Any] | None = Field(
        default=None,
        description="Chub's `GET /api/characters/{fullPath}?full=true` node. Required for provider=chub.",
    )
    linked_lorebook: dict[str, Any] | None = Field(
        default=None,
        description="The linked-project lorebook, when the node carries `related_lorebooks`. Resolved browser-side through Chub's v4 git API.",
    )
    # -- datacat
    character: dict[str, Any] | None = Field(
        default=None,
        description="DataCat's `GET /api/characters/{id}` detail payload, with `scripts[]` already hydrated. Required for provider=datacat.",
    )


class DiscoverPreviewOut(BaseModel):
    """A provider card described in the archive's own terms.

    Deliberately *not* a `CardDetailOut`: there is no file, so there is no
    filename, size, mtime, thumbnail or gallery, and inventing them would make
    the type lie. What it does share is every field derived from the card
    itself, under the same names, so the detail panes render a preview and an
    archived card with the same components.
    """

    provider: str
    name: str
    creator: str
    page_name: str
    tags: list[str]
    source_kind: str
    source_url: str
    card_id: str = Field(description="The provider's own id for the character.")
    fragment: str = Field(description="The `_<id8>` slice the archive would file it under.")
    character_version: str
    greetings: int = Field(description="Primary greeting plus alternates.")
    lore_entries: int
    description_chars: int
    prompt_chars: int
    has_creator_notes: bool
    has_example_dialogue: bool
    create_date: str
    spec: str
    spec_version: str
    card: dict[str, Any] = Field(description="The card's `data` object, exactly as the build route would write it.")
    avatar_url: str | None = Field(
        default=None,
        description="The provider's image for the card. Served from the provider, not the archive -- nothing has been downloaded.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="What the same cleaning pass the build route runs would flag -- unresolved macros, mostly. Shown so a card's problems are visible before it is kept.",
    )


def _chub_preview(req: DiscoverPreviewIn) -> DiscoverPreviewOut:
    """`build_chub`'s first half, stopping before the write.

    Mirrors `aspect/api/build.py:build_chub` line for line down to `to_payload`;
    the only difference is that `extensions.jai` carries no `linkedAt` -- the
    card has not been acquired, and stamping a time would be a claim about
    something that has not happened.
    """
    node = req.node or {}
    raw_data = chub.build_v2_from_chub(node, req.linked_lorebook)
    cleaned, warnings = chub.clean_card(raw_data, deps.chub_sanitizer)

    card_id = chub.card_id(cleaned) or ""
    cleaned["extensions"] = {
        **(cleaned.get("extensions") or {}),
        "jai": {
            "source_url": chub.source_url(cleaned),
            "id": card_id,
            "sourceKind": "chub_core",
            "creatorName": chub.creator(cleaned),
            "pageName": chub.page_name(cleaned),
        },
    }
    payload = chub.to_payload(cleaned)
    avatar_url = chub.avatar_url(node)
    return _assemble(
        provider="chub",
        payload=payload,
        data=cleaned,
        avatar_url=avatar_url,
        warnings=warnings,
    )


def _datacat_preview(req: DiscoverPreviewIn) -> DiscoverPreviewOut:
    """`build_datacat`'s first half, stopping before the write.

    Goes through `build_card` (CardBuilder) rather than using the mapped dict
    directly, because that is what `/build-datacat` does -- CardBuilder is
    where a datacat card's greetings get assembled and its fields normalised,
    so a preview that skipped it would show a different card than the one that
    lands.
    """
    character = req.character or {}
    v2 = datacat.build_v2_from_character(character)
    if v2 is None:
        raise HTTPException(status_code=422, detail="datacat: no usable character data")

    data = v2["data"]
    profile = datacat.to_profile_fields(data)
    book_dict = data.get("character_book")
    book = CharacterBook.model_validate(book_dict) if book_dict else None

    raw_card_id = character.get("character_id") or character.get("characterId")
    card_id = str(raw_card_id) if raw_card_id else ""

    source_kind = datacat.normalized_source_kind(character)
    if source_kind == "saucepan":
        source_url = f"{SAUCEPAN_ORIGIN}/companion/{card_id}" if card_id else None
    else:
        source_url = f"https://janitorai.com/characters/{card_id}" if card_id else None

    avatar_url = datacat.resolve_avatar_url(character)
    card, warnings = build_card(
        profile=profile,
        greetings=datacat.greetings(data),
        book=book,
        avatar_url=avatar_url,
        character_version=source_url or "aspect-archive",
        extensions={
            "jai": {
                "source_url": source_url,
                "id": card_id,
                "sourceKind": "datacat_core",
                "creatorName": profile.creator,
                "pageName": datacat.page_name(data),
            },
        },
    )
    payload = card.to_dict()
    return _assemble(
        provider="datacat",
        payload=payload,
        data=payload["data"],
        avatar_url=avatar_url,
        warnings=warnings,
    )


def _assemble(
    *,
    provider: str,
    payload: dict[str, Any],
    data: dict[str, Any],
    avatar_url: str | None,
    warnings: list[str],
) -> DiscoverPreviewOut:
    """The counting half, shared by both providers and with the archive itself.

    `summarize_data` is `catalog.summarize`'s content half (`aspect/archive/
    catalog.py`), so a preview's greeting count, lore-entry count and prompt
    weight are produced by the same code that produces them for a card on disk.
    """
    summary = catalog.summarize_data(data, payload)
    return DiscoverPreviewOut(
        provider=provider,
        name=summary["name"],
        creator=summary["creator"],
        page_name=summary["page_name"],
        tags=list(summary["tags"]),
        source_kind=summary["source_kind"],
        source_url=summary["source_url"],
        card_id=summary["card_id"],
        fragment=summary["fragment"],
        character_version=summary["character_version"],
        greetings=summary["greeting_count"],
        lore_entries=summary["lore_entry_count"],
        description_chars=summary["description_chars"],
        prompt_chars=summary["prompt_chars"],
        has_creator_notes=summary["has_creator_notes"],
        has_example_dialogue=summary["has_example_dialogue"],
        create_date=summary["create_date"],
        spec=str(payload.get("spec", "")),
        spec_version=str(payload.get("spec_version", "")),
        card=data,
        avatar_url=avatar_url,
        warnings=warnings,
    )


@router.post(
    "/discover/preview",
    response_model=DiscoverPreviewOut,
    summary="Read a provider card without keeping it",
)
def preview(req: DiscoverPreviewIn) -> DiscoverPreviewOut:
    """Map a captured provider payload into a tavern card and describe it.

    The one door Discover's card view goes through. See the module docstring
    for why this is a server route and not a TypeScript mapper.
    """
    if req.provider == "chub":
        if not req.node:
            raise HTTPException(status_code=422, detail="chub: `node` is required")
        return _chub_preview(req)
    if not req.character:
        raise HTTPException(status_code=422, detail="datacat: `character` is required")
    return _datacat_preview(req)


# ---------------------------------------------------------------------------
# First-seen stamps -- what "Recent only" filters on
# ---------------------------------------------------------------------------


def _seen_store() -> seen_state.SeenStore:
    """Built per call so a test that repoints `settings.seen_file` -- or an
    operator who moves the archive -- is honoured rather than captured at
    import time. The same construction `system.py:_settings_store` uses."""
    return seen_state.SeenStore(settings.seen_file)


class SeenIn(BaseModel):
    provider: Provider
    ids: list[str] = Field(
        description="Every provider id one Following feed load returned -- the provider's own ids, not the archive's `_<id8>` fragments. Blanks and duplicates are dropped.",
    )


class SeenOut(BaseModel):
    provider: str
    added: int = Field(description="How many of the given ids had never been seen before, and were stamped now.")
    first_seen: dict[str, int] = Field(
        description="Each given id's first-seen stamp, epoch milliseconds on the server's clock. Covers exactly the ids sent, not the whole history.",
    )


@router.post(
    "/discover/seen",
    response_model=SeenOut,
    summary="Record a Following feed and read back when each card was first seen",
)
def record_seen(body: SeenIn) -> SeenOut:
    """Stamp the ids never seen before, and answer every id's first stamp.

    One call per feed load rather than a read plus a write: the client never
    needs one without the other, and doing both under one lock means two tabs
    loading at once cannot each decide a card is new. An id already recorded
    keeps its stamp -- there is no route that moves or removes one. See
    `aspect.state.seen`.
    """
    if len(body.ids) > MAX_IDS_PER_REQUEST:
        raise HTTPException(
            status_code=422,
            detail=f"{len(body.ids)} ids in one request, over the {MAX_IDS_PER_REQUEST}-id ceiling",
        )
    try:
        added, first_seen = _seen_store().record(body.provider, body.ids)
    except seen_state.SeenError as exc:
        # A damaged file is a 500 rather than "everything is new", for the
        # store's reason: the next write would replace the history.
        logger.error("seen stamps not recorded: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return SeenOut(provider=body.provider, added=added, first_seen=first_seen)
