"""`GET /api/v1/duplicates` -- candidate duplicate cards, scoped to same-creator
comparisons only.

Read-only: this scans the archive and reports groups, it never writes.
Discarding a card the user decides is inferior is a separate, ordinary
`DELETE /api/v1/characters/{card_id}` -- already a reversible bin, not a new
action this module needs to own.

Computed fresh on every call rather than cached, matching the "manual scan,
then review" shape of the feature: the archive's own text-chunk sweep is
seconds across the whole corpus, and the expensive step (prose comparison)
only ever runs on pairs `aspect.cards.dupes` has already gated cheaply. See
that module for the heuristic itself.
"""

from __future__ import annotations

import hashlib
from urllib.parse import quote

from fastapi import APIRouter

from aspect.api.schemas import (
    DuplicateGroupOut,
    DuplicateMemberOut,
    DuplicatePairOut,
    DuplicatesOut,
)
from aspect.api.v1 import _shared
from aspect.archive import catalog
from aspect.cards import dupes, edit

router = APIRouter()


def _member_out(record: catalog.CardSummary) -> DuplicateMemberOut:
    quoted = quote(record.filename, safe="")
    return DuplicateMemberOut(
        id=record.filename,
        name=record.name,
        page_name=record.page_name,
        tags=list(record.tags),
        description_chars=record.description_chars,
        character_version=record.character_version,
        create_date=record.create_date,
        thumb_url=f"{_shared.PREFIX}/characters/{quoted}/thumb",
        png_url=f"{_shared.PREFIX}/characters/{quoted}/png",
    )


def _group_id(filenames: list[str]) -> str:
    joined = "|".join(sorted(filenames))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


@router.get(
    "/duplicates",
    response_model=DuplicatesOut,
    summary="Candidate duplicate groups, scoped to same-creator cards only",
)
def get_duplicates() -> DuplicatesOut:
    idx = _shared.index()
    records = {record.filename: record for record in idx.cards()}

    signals: list[dupes.CardSignals] = []
    for record in records.values():
        thumb = _shared.thumbnail_store.avatar(record.filename)
        avatar_hash: int | None = None
        if thumb is not None:
            try:
                avatar_hash = dupes.avatar_hash(thumb.path.read_bytes())
            except (OSError, ValueError):
                avatar_hash = None
        signals.append(
            dupes.CardSignals(
                filename=record.filename,
                name=record.name,
                creator=record.creator,
                avatar_hash=avatar_hash,
            )
        )

    text_cache: dict[str, tuple[str, str, str]] = {}

    def read_text(filename: str) -> tuple[str, str, str]:
        if filename in text_cache:
            return text_cache[filename]
        try:
            _, data = edit.read_card(idx.root / filename)
        except edit.WriteError:
            result = ("", "", "")
        else:
            result = (
                str(data.get("description") or ""),
                str(data.get("first_mes") or ""),
                str(data.get("creator_notes") or ""),
            )
        text_cache[filename] = result
        return result

    component_groups = dupes.find_duplicate_groups(signals, read_text)

    out: list[DuplicateGroupOut] = []
    for pairs in component_groups:
        member_filenames = sorted({p.a for p in pairs} | {p.b for p in pairs})
        creator = records[member_filenames[0]].creator
        out.append(
            DuplicateGroupOut(
                group_id=_group_id(member_filenames),
                creator=creator,
                members=[_member_out(records[filename]) for filename in member_filenames],
                pairs=[
                    DuplicatePairOut(
                        a=pair.a,
                        b=pair.b,
                        avatar_distance=pair.avatar_distance,
                        name_score=round(pair.name_score, 3),
                        text_score=round(pair.text_score, 3),
                        strength=pair.strength,
                        reasons=list(pair.reasons),
                    )
                    for pair in pairs
                ],
            )
        )

    # Bigger / more-corroborated groups first -- an 8-card cluster is worth a
    # look before a 2-card weak name-only match.
    out.sort(key=lambda group: (-len(group.members), group.group_id))
    scanned = sum(len(cards) for cards in dupes.group_by_creator(signals).values())
    return DuplicatesOut(groups=out, scanned=scanned)
