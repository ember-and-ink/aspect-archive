"""The `/build-*` endpoints: one per place a card can come from.

Each route is the same shape -- take what the browser captured, hand it to that
site's mapper in `aspect.sources`, then run the shared tail: dedupe-check by card
id, build the card, fetch the avatar, write the PNG, announce it on the
dashboard. What differs between them is only how the neutral fields are
produced, and whether the card goes through CardBuilder at all (Chub does not --
see `aspect.sources.chub`'s raw-dict-passthrough rule).

The browser is a pure capture layer in every one of these paths: it fetches and
posts, the server maps and writes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from aspect import deps
from aspect.api.build_schemas import (
    BuildRequest,
    BuildResponse,
    ChubBuildRequest,
    DatacatBuildRequest,
    SaucepanBuildRequest,
)
from aspect.archive import catalog
from aspect.cards.models import CaptureRecord, CharacterBook, CharacterCardV3, ProfileFields
from aspect.runtime import dashboard
from aspect.sources import chub, datacat, janitor, saucepan
from aspect.sources.saucepan import SAUCEPAN_ORIGIN

logger = logging.getLogger("aspect.api.build")

router = APIRouter()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _datacat_block(
    *,
    card_id: str | None,
    source_kind: str,
    creator_id: str,
    creator_name: str,
    page_name: str,
    linked_at: str,
) -> dict[str, Any]:
    """extensions.datacat provenance -- the shape SillyTavern-CharacterLibrary's
    datacat provider reads and writes natively (modules/providers/datacat/
    datacat-provider.js), built from the same source fields as extensions.jai
    so a freshly downloaded card is already linkable there without a separate
    datacat lookup. `source_kind` is datacat's own vocabulary ("janitor" /
    "saucepan"), distinct from extensions.jai's sourceKind values."""
    return {
        "id": card_id,
        "sourceKind": source_kind,
        "creatorId": creator_id or None,
        "creatorName": creator_name,
        "pageName": page_name,
        "linkedAt": linked_at,
    }


def _record_download(
    *,
    source: str,
    name: str,
    creator: str,
    ok: bool = True,
    detail: str = "",
    duplicate: bool = False,
    filename: str = "",
) -> None:
    """Announce a card build on the dashboard's downloads column, and log the
    same line so the plain-logging path (no TTY) still says what was written.

    `duplicate` means the card was already on disk, so nothing was written at
    all -- reported rather than silently dropped, since from the browser the
    click looks identical to a real export."""
    if ok:
        logger.info(
            "%s %s card: %s by %s%s",
            "already have" if duplicate else "saved",
            source,
            name,
            creator or "unknown",
            f" ({filename})" if filename else "",
        )
    else:
        logger.warning("%s card not saved: %s — %s", source, name, detail)
    if dashboard.DASHBOARD is not None:
        dashboard.DASHBOARD.feed.record(
            source=source,
            name=name,
            creator=creator,
            ok=ok,
            detail=detail,
            duplicate=duplicate,
            filename=filename,
        )


def _check_duplicate(
    card_id: str | None, *, source: str, name: str, creator: str
) -> BuildResponse | None:
    """A card whose id is already on disk is left alone: the caller stops here,
    before the avatar fetch, and reports the file found. Re-acquiring one means
    deleting it first -- the same rule `make import` and the bulk sweep follow,
    so a card edited in SillyTavern is never silently replaced. Returns None
    when there's no on-disk match (the caller should proceed to build/write)."""
    already = deps.png_writer.find_by_id(card_id)
    if not already:
        return None
    _record_download(
        source=source, name=name, creator=creator, duplicate=True, filename=already[0].name
    )
    return BuildResponse(ok=True, path=str(already[0]), duplicate=True)


def build_card(
    *,
    profile: ProfileFields,
    greetings: list[str],
    book: CharacterBook | None,
    avatar_url: str | None,
    character_version: str,
    extensions: dict[str, Any],
    capture: CaptureRecord | None = None,
    warnings: list[str] | None = None,
) -> tuple[CharacterCardV3, list[str]]:
    """Source-specific half of the shared build/write tail: run the neutral
    fields through CardBuilder and stamp provenance. /build-jai (JanitorAI) and
    /build-saucepan differ only in how they produce these inputs -- everything
    from here down (and in fetch_avatar_and_write) is identical. Chub bypasses
    this entirely (see sources.chub's raw-dict-passthrough rule) and goes
    straight to fetch_avatar_and_write with its own payload."""
    card, build_warnings = deps.card_builder.build(
        profile, greetings, capture=capture, book=book, avatar_url=avatar_url
    )
    all_warnings = (warnings or []) + build_warnings

    card.character_version = character_version or "aspect-archive"
    card.extensions = extensions
    return card, all_warnings


async def fetch_avatar_and_write(
    *,
    write_fn: Callable[[bytes], Path],
    avatar_url: str | None,
    avatar_b64: str | None,
    source: str,
    name: str,
    creator: str,
    warnings: list[str],
    fields_present: dict[str, bool],
) -> BuildResponse:
    """The tail shared by every write path regardless of how the card payload
    was produced: fetch the avatar, write the PNG via `write_fn` (closes over
    either deps.png_writer.write for a CharacterCardV3 or deps.png_writer.write_payload
    for a raw dict), and announce the result on the dashboard."""
    avatar_bytes = await deps.avatar_fetcher.fetch(avatar_url, avatar_b64)
    path = write_fn(avatar_bytes)
    # The index never rescans on its own, and the browser reads the card back the
    # moment this returns (finishBrowseImport), so the write has to land in it
    # here. The v1 write routes refresh for the same reason.
    catalog.index().refresh()
    # Imported here, not at the top: `aspect.api.v1.discover` imports this module,
    # so a top-level import of the v1 package would be circular.
    from aspect.api.v1 import media as v1_media

    # Off the event loop -- queueing blocks until the job store's own loop has
    # taken the job, and that loop is this one.
    await asyncio.to_thread(v1_media.queue_direct_images, path.name)
    _record_download(source=source, name=name, creator=creator, filename=path.name)
    return BuildResponse(ok=True, path=str(path), warnings=warnings, fields_present=fields_present)


def _fields_present(card: CharacterCardV3) -> dict[str, bool]:
    return {
        "description": bool(card.description),
        "scenario": bool(card.scenario),
        "mes_example": bool(card.mes_example),
        "first_mes": bool(card.first_mes),
        "alternate_greetings": bool(card.alternate_greetings),
        "creator_notes": bool(card.creator_notes),
        "tags": bool(card.tags),
        "character_book": card.character_book is not None,
    }


async def _assemble_and_write(
    *,
    profile: ProfileFields,
    greetings: list[str],
    book: CharacterBook | None,
    avatar_url: str | None,
    avatar_b64: str | None,
    card_id: str | None,
    character_version: str,
    extensions: dict[str, Any],
    source: str,
    capture: CaptureRecord | None = None,
    warnings: list[str] | None = None,
) -> BuildResponse:
    """The CardBuilder path: dedupe-check, build_card, fetch_avatar_and_write.
    Used as-is by /build-jai and /build-saucepan; /build-chub uses the two
    halves directly since it never goes through CardBuilder."""
    duplicate = _check_duplicate(
        card_id, source=source, name=profile.name, creator=profile.creator or ""
    )
    if duplicate is not None:
        return duplicate

    card, all_warnings = build_card(
        profile=profile,
        greetings=greetings,
        book=book,
        avatar_url=avatar_url,
        character_version=character_version,
        extensions=extensions,
        capture=capture,
        warnings=warnings,
    )

    return await fetch_avatar_and_write(
        write_fn=lambda avatar_bytes: deps.png_writer.write(card, avatar_bytes, card_id=card_id),
        avatar_url=avatar_url,
        avatar_b64=avatar_b64,
        source=source,
        name=card.name,
        creator=card.creator or "",
        warnings=all_warnings,
        fields_present=_fields_present(card),
    )


@router.post("/build-jai")
async def build_jai(req: BuildRequest) -> BuildResponse:
    character = req.character_json or {}
    profile = janitor.to_profile_fields(character)
    if not profile.name:
        profile.name = req.character.name

    hidden = janitor.is_hidden(character)
    capture = deps.capture_store.get(profile.name)
    json_greetings = janitor.greetings(character)

    if hidden:
        # A hidden card's definition and its primary greeting both ride in on
        # the chat relay capture; the JSON supplies only the alternates.
        captured = capture.greetings if capture else []
        has_system = capture is not None and bool(
            capture.personality or capture.scenario or capture.mes_example
        )
        has_primary = bool(captured)
        if not (has_system and has_primary):
            _record_download(
                source="janitor",
                name=profile.name,
                creator=profile.creator,
                ok=False,
                detail="hidden — no chat capture yet",
            )
            return BuildResponse(
                ok=False,
                warnings=[
                    "hidden card not exportable — send a chat message in this "
                    "character's chat first so the proxy can capture its hidden "
                    "definition and primary greeting"
                ],
            )
        primary = captured[0]
        greetings = [primary] + [g for g in json_greetings if g != primary]
    else:
        greetings = json_greetings

    raw_scripts = [lb.raw for lb in req.lorebooks]
    book, lore_warnings = deps.lorebook_mapper.map(raw_scripts, character_name=profile.name)
    if capture is not None:
        book = deps.lorebook_mapper.merge(book, capture.lore_entries)

    avatar_url = req.avatar_url or janitor.avatar_url(character)

    # data.name is the real character name (chat_name); the JSON `name` field
    # is the card-title blurb (often a scenario hook, not an actual character
    # name -- see "She needs your help"), preserved as metadata instead.
    page_name = (character.get("name") or "").strip()
    card_id = req.character.id or character.get("id")
    linked_at = _utc_now_iso()
    extensions = {
        "jai": {
            "source_url": req.character.url,
            "id": card_id,
            "sourceKind": "janitor_core",
            "creatorName": profile.creator,
            "pageName": page_name,
            "linkedAt": linked_at,
        },
        "datacat": _datacat_block(
            card_id=card_id,
            source_kind="janitor",
            creator_id=janitor.creator_id(character),
            creator_name=profile.creator,
            page_name=page_name,
            linked_at=linked_at,
        ),
    }

    return await _assemble_and_write(
        profile=profile,
        greetings=greetings,
        book=book,
        avatar_url=avatar_url,
        avatar_b64=req.avatar_b64,
        card_id=card_id,
        character_version=req.character.url or "aspect-archive",
        extensions=extensions,
        source="janitor",
        capture=capture,
        warnings=lore_warnings,
    )


def _resolve_saucepan_lorebooks(raw: dict[str, Any]) -> None:
    """Reconcile the export's lorebooks with the on-disk cache, in place.

    The cache-aware userscript sends only the lorebooks it had to fetch (the
    cache misses) in `lorebooks`, plus `cached_lorebook_ids` for the rest. We
    write the fresh ones through to the cache and load the cached ids back in, so
    sources.saucepan sees every attached lorebook exactly as if all had been
    fetched. An older userscript that sends every lorebook inline (and no
    cached_lorebook_ids) still works -- it just also warms the cache."""
    fetched = [b for b in (raw.get("lorebooks") or []) if isinstance(b, dict)]
    present: set[str] = set()
    for book in fetched:
        lid = (book.get("id") or "").strip()
        if lid:
            deps.lorebook_cache.put("saucepan", lid, book)
            present.add(lid)

    combined = list(fetched)
    for raw_id in raw.get("cached_lorebook_ids") or []:
        lid = (raw_id or "").strip()
        if not lid or lid in present:
            continue
        blob = deps.lorebook_cache.get("saucepan", lid)
        if blob is not None:
            combined.append(blob)
            present.add(lid)
    raw["lorebooks"] = combined


@router.post("/build-saucepan")
async def build_saucepan(req: SaucepanBuildRequest) -> BuildResponse:
    """saucepan peer of /build-jai. The userscript posts the raw
    {id, definition, companion, lorebooks} it fetched; the server deobfuscates
    and maps it (sources.saucepan), then reuses the shared assemble/write tail.
    saucepan definitions carry macros intact, so there's no hidden-capture /
    name-reversal step -- it's a straight open-card build."""
    raw = req.character or {}
    _resolve_saucepan_lorebooks(raw)
    profile = saucepan.to_profile_fields(raw)
    greetings = saucepan.greetings(raw)
    book = saucepan.character_book(raw, character_name=profile.name)
    avatar_url = req.avatar_url or saucepan.avatar_url(raw)
    card_id = saucepan.companion_id(raw)

    warnings: list[str] = []
    if not saucepan.is_open(raw):
        warnings.append(
            "saucepan definition is not open — only public fields were available; "
            "description/scenario/example may be incomplete"
        )

    source_url = f"{SAUCEPAN_ORIGIN}/companion/{card_id}" if card_id else None
    page_name = saucepan.page_name(raw)
    linked_at = _utc_now_iso()
    extensions = {
        "jai": {
            "source_url": source_url,
            "id": card_id or None,
            "sourceKind": "saucepan_core",
            "creatorName": profile.creator,
            "pageName": page_name,
            "linkedAt": linked_at,
        },
        "datacat": _datacat_block(
            card_id=card_id or None,
            source_kind="saucepan",
            creator_id=saucepan.creator_id(raw),
            creator_name=profile.creator,
            page_name=page_name,
            linked_at=linked_at,
        ),
    }

    return await _assemble_and_write(
        profile=profile,
        greetings=greetings,
        book=book,
        avatar_url=avatar_url,
        avatar_b64=req.avatar_b64,
        card_id=card_id or None,
        character_version=source_url or "aspect-archive",
        extensions=extensions,
        source="saucepan",
        warnings=warnings,
    )


def _chub_fields_present(data: dict[str, Any]) -> dict[str, bool]:
    return {
        "description": bool(data.get("description")),
        "scenario": bool(data.get("scenario")),
        "mes_example": bool(data.get("mes_example")),
        "first_mes": bool(data.get("first_mes")),
        "alternate_greetings": bool(data.get("alternate_greetings")),
        "creator_notes": bool(data.get("creator_notes")),
        "tags": bool(data.get("tags")),
        "character_book": data.get("character_book") is not None,
    }


@router.post("/build-chub")
async def build_chub(req: ChubBuildRequest) -> BuildResponse:
    """Chub peer of /build-saucepan (see ChubBuildRequest for the request
    shape). The browser is a pure capture layer here too: it fetches the raw
    Chub node and linked lorebook (both CORS-open, no proxy needed) and posts
    them; the server builds and cleans (sources.chub) and writes straight
    through write_payload -- never through CardBuilder/pydantic, per
    sources.chub's raw-dict-passthrough rule (a Chub character_book carries
    priority/probability/selectiveLogic and an int-or-string position that a
    pydantic LoreEntry round-trip would drop or coerce)."""
    node = req.node or {}
    raw_data = chub.build_v2_from_chub(node, req.linked_lorebook)
    cleaned, warnings = chub.clean_card(raw_data, deps.chub_sanitizer)

    card_id = chub.card_id(cleaned) or None
    name = chub.name(cleaned)
    creator = chub.creator(cleaned)

    duplicate = _check_duplicate(card_id, source="chub", name=name, creator=creator)
    if duplicate is not None:
        return duplicate

    page_name = chub.page_name(cleaned)
    cleaned["extensions"] = {
        **(cleaned.get("extensions") or {}),
        "jai": {
            "source_url": chub.source_url(cleaned),
            "id": card_id,
            "sourceKind": "chub_core",
            "creatorName": creator,
            "pageName": page_name,
            "linkedAt": _utc_now_iso(),
        },
    }
    if req.gallery_id:
        cleaned["extensions"]["gallery_id"] = req.gallery_id

    payload = chub.to_payload(cleaned)
    avatar_url = req.avatar_url or chub.avatar_url(node)

    response = await fetch_avatar_and_write(
        write_fn=lambda avatar_bytes: deps.png_writer.write_payload(
            payload, avatar_bytes, creator=creator, name=name, card_id=card_id
        ),
        avatar_url=avatar_url,
        avatar_b64=req.avatar_b64,
        source="chub",
        name=name,
        creator=creator,
        warnings=warnings,
        fields_present=_chub_fields_present(cleaned),
    )
    if response.ok and not response.duplicate:
        # write_payload stamps extensions.gallery_id (and gallery.py's other
        # bookkeeping) into `payload` in place before this returns, so it's
        # already current by the time we hand it back.
        response.filename = Path(response.path).name if response.path else None
        response.card = payload
    return response


@router.post("/build-datacat")
async def build_datacat(req: DatacatBuildRequest) -> BuildResponse:
    """DataCat peer of /build-chub (see DatacatBuildRequest for the request
    shape). Unlike Chub, a datacat character carries nothing pydantic's
    LoreEntry round-trip would drop, so this goes through CardBuilder like
    /build and /build-saucepan -- build_card + fetch_avatar_and_write directly
    (not _assemble_and_write) so the built card is available afterward to
    populate the filename/card response fields the browser needs (see
    BuildResponse's docstring)."""
    character = req.character or {}
    v2 = datacat.build_v2_from_character(character)
    if v2 is None:
        return BuildResponse(ok=False, warnings=["datacat: no usable character data"])

    data = v2["data"]
    profile = datacat.to_profile_fields(data)
    greetings = datacat.greetings(data)
    book_dict = data.get("character_book")
    book = CharacterBook.model_validate(book_dict) if book_dict else None

    raw_card_id = character.get("character_id") or character.get("characterId")
    card_id = str(raw_card_id) if raw_card_id else None

    duplicate = _check_duplicate(card_id, source="datacat", name=profile.name, creator=profile.creator)
    if duplicate is not None:
        return duplicate

    source_kind = datacat.normalized_source_kind(character)
    if source_kind == "saucepan":
        source_url = f"{SAUCEPAN_ORIGIN}/companion/{card_id}" if card_id else None
    else:
        source_url = f"https://janitorai.com/characters/{card_id}" if card_id else None

    page_name = datacat.page_name(data)
    creator_id = datacat.creator_id(data)
    linked_at = _utc_now_iso()
    extensions: dict[str, Any] = {
        "jai": {
            "source_url": source_url,
            "id": card_id,
            "sourceKind": "datacat_core",
            "creatorName": profile.creator,
            "pageName": page_name,
            "linkedAt": linked_at,
        },
        "datacat": _datacat_block(
            card_id=card_id,
            source_kind=source_kind,
            creator_id=creator_id,
            creator_name=profile.creator,
            page_name=page_name,
            linked_at=linked_at,
        ),
    }
    if req.gallery_id:
        extensions["gallery_id"] = req.gallery_id

    avatar_url = req.avatar_url or datacat.resolve_avatar_url(character)

    card, all_warnings = build_card(
        profile=profile,
        greetings=greetings,
        book=book,
        avatar_url=avatar_url,
        character_version=source_url or "aspect-archive",
        extensions=extensions,
    )

    response = await fetch_avatar_and_write(
        write_fn=lambda avatar_bytes: deps.png_writer.write(card, avatar_bytes, card_id=card_id),
        avatar_url=avatar_url,
        avatar_b64=req.avatar_b64,
        source="datacat",
        name=card.name,
        creator=card.creator or "",
        warnings=all_warnings,
        fields_present=_fields_present(card),
    )
    if response.ok and not response.duplicate:
        response.filename = Path(response.path).name if response.path else None
        response.card = card.to_dict()
    return response
