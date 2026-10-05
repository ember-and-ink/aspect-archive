"""`/api/v1/lorebooks` -- lorebooks as entities.

The reads are derived from the cards by `proxy.archive.lorebooks`: which
distinct lorebooks the archive holds, which cards carry each, where copies have
drifted apart.

Most writes record *decisions* -- these two lorebooks are one, these two entries
are one, this is the version to keep, this lorebook belongs on that card -- in
`proxy.state.lorebook_decisions`. None of them touches a card: a decision
changes what these routes report, and is undone by its matching DELETE.

Two routes do write cards, and only when called: `POST /lorebooks/sync` carries
the decisions out (`proxy.archive.lorebook_sync`), saving each card's original
lorebook first, and `POST /characters/{id}/lorebook-original/restore` puts one
back.
"""

from __future__ import annotations

import difflib
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query

from proxy.api.schemas import (
    CardLorebookOut,
    LorebookChooseIn,
    LorebookChooseNewestOut,
    LorebookEntryCandidateOut,
    LorebookMergeIn,
    LorebookMergeOut,
    LorebookOriginalOut,
    LorebookRefOut,
    LorebookSyncCardOut,
    LorebookSyncFailureOut,
    LorebookSyncIn,
    LorebookSyncOut,
    LorebookSyncPlanOut,
    LorebookCardOut,
    LorebookDetailOut,
    LorebookEntriesOut,
    LorebookEntryDetailOut,
    LorebookEntryOut,
    LorebookEntryVersionOut,
    LorebookOut,
    LorebookRevisionOut,
    LorebookSimilarOut,
    LorebooksOut,
    LorebookStatsOut,
)
from proxy.api.v1 import _shared
from proxy.archive import catalog, lorebook_sync, lorebooks
from proxy.cards import edit, lorebook_split
from proxy.config import settings
from proxy.state import lorebook_decisions, lorebook_snapshots

router = APIRouter()

_PREVIEW_CHARS = 220
# How alike two entries' text must be to be offered as the same entry, and how
# much of each is compared. A bounded heuristic: the user confirms every merge.
_CANDIDATE_RATIO = 0.6
_COMPARE_CHARS = 2000


# The last decisions read, with the file key they were read at, so a browse
# page's worth of requests parses the file once rather than once each.
_decisions_cache: tuple[Any, lorebook_decisions.Decisions] | None = None


def _store() -> lorebook_decisions.DecisionsStore:
    """Built per call so a test that repoints the setting takes effect."""
    return lorebook_decisions.DecisionsStore(settings.lorebook_decisions_file)


def _indexes() -> tuple[catalog.ArchiveIndex, lorebooks.LorebookIndex]:
    global _decisions_cache
    idx = _shared.index()
    store = _store()
    key = (str(store.path), store.key())
    if _decisions_cache is None or _decisions_cache[0] != key:
        try:
            _decisions_cache = (key, store.read())
        except lorebook_decisions.DecisionsError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return idx, lorebooks.index(idx, _decisions_cache[1], key)


def _waiting(idx: catalog.ArchiveIndex, lore: lorebooks.LorebookIndex) -> lorebook_sync.Pending:
    """The attach/detach decisions as they stand against `idx`. Call after
    `_indexes()`, which is what loads the decisions."""
    decisions = _decisions_cache[1] if _decisions_cache else lorebook_decisions.Decisions()
    return lorebook_sync.pending(idx, lore, decisions)


def _snapshots() -> lorebook_snapshots.SnapshotStore:
    return lorebook_snapshots.SnapshotStore(settings.lorebook_snapshots_dir)


def _write(action: Any) -> Any:
    """Run one store mutation, turning a failed write into a 500 that says why."""
    try:
        return action()
    except (lorebook_decisions.DecisionsError, lorebook_snapshots.SnapshotError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _merge_out(merge: lorebook_decisions.Merge) -> LorebookMergeOut:
    return LorebookMergeOut(id=merge.id, a_name=merge.a_name, b_name=merge.b_name, at=merge.at)


def _card_out(idx: catalog.ArchiveIndex, filename: str) -> LorebookCardOut:
    record = idx.get(filename)
    quoted = quote(filename, safe="")
    return LorebookCardOut(
        id=filename,
        name=record.name if record else filename,
        creator=record.creator if record else "",
        create_date=record.create_date if record else "",
        thumb_url=f"{_shared.PREFIX}/characters/{quoted}/thumb",
    )


def _book_fields(book: lorebooks.Lorebook, waiting: lorebook_sync.Pending) -> dict:
    return {
        "id": book.id,
        "name": book.name,
        "kinds": list(book.kinds),
        "refs": list(book.refs),
        "creators": list(book.creators),
        "card_count": len(book.cards),
        "entry_count": len(book.lines),
        "revision_count": len(book.revisions),
        "changed_entries": book.changed_lines,
        "similar_count": len(book.similar),
        "unresolved_entries": book.unresolved_lines,
        "pending_cards": len(book.pending_cards | waiting.cards_of(book.id)),
        "chars": book.chars,
    }


def _preview(content: str) -> str:
    return " ".join(content[: _PREVIEW_CHARS * 2].split())[:_PREVIEW_CHARS]


def _entry_out(lore: lorebooks.LorebookIndex, line: lorebooks.EntryLine, book_name: str) -> LorebookEntryOut:
    # The chosen version when there is one -- it is what the entry *is* now.
    newest = lore.version(line.chosen or line.versions[0].hash)
    content = newest.content if newest else ""
    return LorebookEntryOut(
        id=line.id,
        lorebook_id=line.lorebook_id,
        lorebook_name=book_name,
        title=line.title,
        keys=list(newest.keys) if newest else [],
        constant=newest.constant if newest else False,
        preview=_preview(content),
        chars=len(content),
        version_count=len(line.versions),
        card_count=line.card_count,
        resolved=line.resolved,
        pending_cards=len(line.pending_cards),
    )


def _candidates(
    lore: lorebooks.LorebookIndex, book: lorebooks.Lorebook, line: lorebooks.EntryLine
) -> list[LorebookEntryCandidateOut]:
    """Entries of the same lorebook that may be `line` under another title.

    Two entries on the same card are two entries by definition, so only lines
    that never share a card are compared -- which is also what keeps this cheap:
    in a lorebook with one revision every pair shares its cards and nothing is
    compared at all."""
    mine = lore.version(line.versions[0].hash)
    if mine is None or not mine.content:
        return []
    cards = {c for v in line.versions for c in v.cards}
    text = lorebook_split.norm_text(mine.content)[:_COMPARE_CHARS]
    matcher = difflib.SequenceMatcher(None, "", text, autojunk=False)
    scored: list[tuple[float, lorebooks.EntryLine, str]] = []
    for other in book.lines:
        if other.id == line.id or any(c in cards for v in other.versions for c in v.cards):
            continue
        theirs = lore.version(other.versions[0].hash)
        if theirs is None or not theirs.content:
            continue
        matcher.set_seq1(lorebook_split.norm_text(theirs.content)[:_COMPARE_CHARS])
        if matcher.real_quick_ratio() < _CANDIDATE_RATIO or matcher.quick_ratio() < _CANDIDATE_RATIO:
            continue
        ratio = matcher.ratio()
        if ratio >= _CANDIDATE_RATIO:
            scored.append((ratio, other, theirs.content))
    scored.sort(key=lambda item: -item[0])
    return [
        LorebookEntryCandidateOut(
            id=other.id,
            title=other.title,
            preview=_preview(content),
            similarity=round(ratio, 3),
            version_count=len(other.versions),
            card_count=other.card_count,
        )
        for ratio, other, content in scored[:5]
    ]


@router.get("/lorebooks", response_model=LorebooksOut, summary="Every distinct lorebook in the archive")
def list_lorebooks() -> LorebooksOut:
    idx, lore = _indexes()
    waiting = _waiting(idx, lore)
    stats = lore.stats
    pending_cards = len({c for book in lore.lorebooks() for c in book.pending_cards} | set(waiting.by_card))
    return LorebooksOut(
        lorebooks=[LorebookOut(**_book_fields(book, waiting)) for book in lore.lorebooks()],
        stats=LorebookStatsOut(
            cards=stats.cards,
            lorebooks=stats.lorebooks,
            shared_lorebooks=stats.shared_lorebooks,
            entries_embedded=stats.entries_embedded,
            entries_unique=stats.entries_unique,
            changed_entries=stats.changed_lines,
            unresolved_entries=stats.unresolved_lines,
            pending_cards=pending_cards,
        ),
    )


def _plan_out(
    idx: catalog.ArchiveIndex, lore: lorebooks.LorebookIndex, plans: list[lorebook_sync.CardPlan]
) -> LorebookSyncPlanOut:
    def names(ids: tuple[str, ...]) -> list[str]:
        return [book.name for book_id in ids if (book := lore.get(book_id))]

    cards: list[LorebookSyncCardOut] = []
    for card in plans:
        record = idx.get(card.filename)
        cards.append(
            LorebookSyncCardOut(
                card=_card_out(idx, card.filename),
                upgrades=card.upgrades,
                duplicates=card.duplicates,
                attach=names(card.attach),
                detach=names(card.detach),
                has_original=bool(record and lorebook_snapshots.pointer(record.extensions)),
            )
        )
    return LorebookSyncPlanOut(cards=cards)


# Registered before `/lorebooks/{lorebook_id}` so "sync" is not taken for an id.
@router.get(
    "/lorebooks/sync",
    response_model=LorebookSyncPlanOut,
    summary="What a sync would do: every card it would rewrite, and how",
)
def sync_plan(lorebook: str = Query("", description="Only this lorebook's changes.")) -> LorebookSyncPlanOut:
    idx, lore = _indexes()
    if lorebook:
        _require_book(lore, lorebook)
    return _plan_out(idx, lore, lorebook_sync.plan(lore, _waiting(idx, lore), lorebook_id=lorebook))


@router.post(
    "/lorebooks/sync",
    response_model=LorebookSyncOut,
    summary="Write the stored decisions onto the cards",
)
def sync(body: LorebookSyncIn) -> LorebookSyncOut:
    """Rewrite each pending card's embedded lorebook in place: chosen versions
    replace the ones it holds, lorebooks waiting to be attached are appended,
    those waiting to be detached are removed. A card holding both a stale
    version of an entry and the chosen one keeps only the chosen one.

    The first time a card is rewritten its lorebook is saved as it was
    (`proxy.state.lorebook_snapshots`), and `.../lorebook-original/restore`
    puts it back. Nothing but the card's embedded book changes -- its pixels
    are byte-identical afterwards, like any other field edit."""
    idx, lore = _indexes()
    if body.lorebook:
        _require_book(lore, body.lorebook)
    waiting = _waiting(idx, lore)
    cards = {_shared.require(idx, card_id).filename for card_id in body.cards} if body.cards else None
    plans = lorebook_sync.plan(lore, waiting, lorebook_id=body.lorebook, cards=cards)
    results = _write(lambda: lorebook_sync.apply(idx, lore, plans, _snapshots()))

    # An attachment is a decision until it is carried out; after that the card
    # says it. Ones that had stopped meaning anything go with them.
    written = {r.filename for r in results if not r.error}
    carried_out = {
        attachment.id
        for filename in written
        for attachment, book_id in waiting.by_card.get(filename, ())
        if not body.lorebook or book_id == body.lorebook
    }
    _write(lambda: _store().drop_attachments(carried_out | {a.id for a in waiting.moot}))
    catalog.index().refresh(force=True)

    return LorebookSyncOut(
        synced=len(written),
        upgraded=sum(r.upgraded for r in results),
        removed=sum(r.removed for r in results),
        attached=sum(r.attached for r in results),
        detached=sum(r.detached for r in results),
        originals_saved=sum(1 for r in results if r.snapshot_taken),
        failed=[
            LorebookSyncFailureOut(id=r.filename, name=_card_out(idx, r.filename).name, error=r.error)
            for r in results
            if r.error
        ],
    )


# Registered before `/lorebooks/{lorebook_id}` so "entries" is not taken for an id.
@router.get(
    "/lorebooks/entries",
    response_model=LorebookEntriesOut,
    summary="Entries across every lorebook, one row per entry however many versions it has",
)
def list_entries(
    q: str = Query("", description="Matches title, keys and text, case-insensitively."),
    lorebook: str = Query("", description="Only this lorebook's entries."),
    changed: bool = Query(False, description="Only entries with more than one version."),
    unresolved: bool = Query(False, description="Only changed entries with no version chosen yet."),
    sort: Literal["versions", "cards", "title"] = "versions",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> LorebookEntriesOut:
    _idx, lore = _indexes()
    needle = q.strip().casefold()
    names = {book.id: book.name for book in lore.lorebooks()}

    matched: list[lorebooks.EntryLine] = []
    for line in lore.lines():
        if lorebook and line.lorebook_id != lorebook:
            continue
        if changed and len(line.versions) < 2:
            continue
        if unresolved and line.resolved:
            continue
        if needle:
            version = lore.version(line.versions[0].hash)
            haystack = " ".join((line.title, *(version.keys if version else ()), version.content if version else ""))
            if needle not in haystack.casefold():
                continue
        matched.append(line)

    if sort == "title":
        matched.sort(key=lambda line: line.title.casefold())
    elif sort == "cards":
        matched.sort(key=lambda line: (-line.card_count, line.title.casefold()))
    else:
        matched.sort(key=lambda line: (-len(line.versions), -line.card_count, line.title.casefold()))

    return LorebookEntriesOut(
        entries=[_entry_out(lore, line, names.get(line.lorebook_id, "")) for line in matched[offset : offset + limit]],
        total=len(matched),
    )


@router.get(
    "/lorebooks/entries/{entry_id}",
    response_model=LorebookEntryDetailOut,
    summary="One entry: every version of its text, and which cards hold each",
)
def get_entry(entry_id: str) -> LorebookEntryDetailOut:
    idx, lore = _indexes()
    line = lore.line(entry_id)
    if line is None:
        raise HTTPException(status_code=404, detail=f"no lorebook entry {entry_id!r}")
    book = lore.get(line.lorebook_id)
    versions: list[LorebookEntryVersionOut] = []
    for held in line.versions:
        version = lore.version(held.hash)
        if version is None:
            continue
        versions.append(
            LorebookEntryVersionOut(
                hash=version.hash,
                title=version.title,
                keys=list(version.keys),
                secondary_keys=list(version.secondary_keys),
                constant=version.constant,
                content=version.content,
                newest=held.newest,
                cards=[_card_out(idx, filename) for filename in held.cards],
            )
        )
    return LorebookEntryDetailOut(
        id=line.id,
        lorebook_id=line.lorebook_id,
        lorebook_name=book.name if book else "",
        title=line.title,
        versions=versions,
        chosen=line.chosen,
        merges=[_merge_out(m) for m in line.merges],
        candidates=_candidates(lore, book, line) if book else [],
    )


def _require_line(lore: lorebooks.LorebookIndex, entry_id: str) -> lorebooks.EntryLine:
    line = lore.line(entry_id)
    if line is None:
        raise HTTPException(status_code=404, detail=f"no lorebook entry {entry_id!r}")
    return line


def _require_book(lore: lorebooks.LorebookIndex, lorebook_id: str) -> lorebooks.Lorebook:
    book = lore.get(lorebook_id)
    if book is None:
        raise HTTPException(status_code=404, detail=f"no lorebook {lorebook_id!r}")
    return book


@router.put(
    "/lorebooks/entries/{entry_id}/chosen",
    response_model=LorebookRefOut,
    summary="Choose the version of an entry every card should carry",
)
def choose_version(entry_id: str, body: LorebookChooseIn) -> LorebookRefOut:
    _idx, lore = _indexes()
    line = _require_line(lore, entry_id)
    hashes = [v.hash for v in line.versions]
    if body.hash not in hashes:
        raise HTTPException(status_code=422, detail="that version does not belong to this entry")
    _write(lambda: _store().choose([body.hash], clear=hashes))
    return LorebookRefOut(id=line.id)


@router.delete(
    "/lorebooks/entries/{entry_id}/chosen",
    response_model=LorebookRefOut,
    summary="Clear an entry's chosen version",
)
def clear_version(entry_id: str) -> LorebookRefOut:
    _idx, lore = _indexes()
    line = _require_line(lore, entry_id)
    _write(lambda: _store().choose([], clear=[v.hash for v in line.versions]))
    return LorebookRefOut(id=line.id)


@router.post(
    "/lorebooks/entries/{entry_id}/merge",
    response_model=LorebookRefOut,
    summary="Declare two entries of one lorebook to be versions of the same entry",
)
def merge_entries(entry_id: str, body: LorebookMergeIn) -> LorebookRefOut:
    _idx, lore = _indexes()
    line, other = _require_line(lore, entry_id), _require_line(lore, body.into)
    if line.id == other.id:
        raise HTTPException(status_code=422, detail="an entry cannot be merged with itself")
    if line.lorebook_id != other.lorebook_id:
        raise HTTPException(status_code=422, detail="entries of different lorebooks cannot be merged; merge the lorebooks first")
    anchor = line.versions[0].hash
    _write(lambda: _store().merge_entries(anchor, other.versions[0].hash))
    _idx, lore = _indexes()
    merged = next((l for l in lore.lines() if any(v.hash == anchor for v in l.versions) and l.lorebook_id == line.lorebook_id), None)
    return LorebookRefOut(id=merged.id if merged else line.id)


@router.delete(
    "/lorebooks/entries/merges/{merge_id}",
    status_code=204,
    summary="Undo an entry merge",
)
def unmerge_entries(merge_id: str) -> None:
    if not _write(lambda: _store().unmerge("entry", merge_id)):
        raise HTTPException(status_code=404, detail=f"no entry merge {merge_id!r}")


@router.post(
    "/lorebooks/{lorebook_id}/merge",
    response_model=LorebookRefOut,
    summary="Declare two lorebooks to be the same book",
)
def merge_lorebooks(lorebook_id: str, body: LorebookMergeIn) -> LorebookRefOut:
    _idx, lore = _indexes()
    book, other = _require_book(lore, lorebook_id), _require_book(lore, body.into)
    if book.id == other.id:
        raise HTTPException(status_code=422, detail="a lorebook cannot be merged with itself")
    merge = _write(lambda: _store().merge_lorebooks(other.anchor, book.anchor, other.name, book.name))
    _idx, lore = _indexes()
    merged = next((b for b in lore.lorebooks() if any(m.id == merge.id for m in b.merges)), None)
    return LorebookRefOut(id=merged.id if merged else other.id)


@router.delete(
    "/lorebooks/merges/{merge_id}",
    status_code=204,
    summary="Undo a lorebook merge",
)
def unmerge_lorebooks(merge_id: str) -> None:
    if not _write(lambda: _store().unmerge("lorebook", merge_id)):
        raise HTTPException(status_code=404, detail=f"no lorebook merge {merge_id!r}")


@router.post(
    "/lorebooks/{lorebook_id}/choose-newest",
    response_model=LorebookChooseNewestOut,
    summary="Choose the newest version of every entry that has none chosen",
)
def choose_newest(lorebook_id: str) -> LorebookChooseNewestOut:
    """"Newest" is the version on the most recently created card -- the same
    guess the pages label as one. Entries already decided are left alone."""
    _idx, lore = _indexes()
    book = _require_book(lore, lorebook_id)
    hashes = [line.versions[0].hash for line in book.lines if not line.resolved]
    if hashes:
        _write(lambda: _store().choose(hashes))
    return LorebookChooseNewestOut(chosen=len(hashes))


@router.get(
    "/lorebooks/{lorebook_id}",
    response_model=LorebookDetailOut,
    summary="One lorebook: its revisions, entries, and the lorebooks it overlaps",
)
def get_lorebook(lorebook_id: str) -> LorebookDetailOut:
    idx, lore = _indexes()
    book = lore.get(lorebook_id)
    if book is None:
        raise HTTPException(status_code=404, detail=f"no lorebook {lorebook_id!r}")
    waiting = _waiting(idx, lore)

    similar: list[LorebookSimilarOut] = []
    for other in book.similar:
        target = lore.get(other.lorebook_id)
        if target is None:
            continue
        similar.append(
            LorebookSimilarOut(
                id=target.id,
                name=target.name,
                card_count=len(target.cards),
                entry_count=len(target.lines),
                shared=other.shared,
                relation=other.relation,  # type: ignore[arg-type]
            )
        )

    return LorebookDetailOut(
        **_book_fields(book, waiting),
        revisions=[
            LorebookRevisionOut(
                fingerprint=revision.fingerprint,
                entry_count=len(revision.hashes),
                newest=revision.newest,
                cards=[_card_out(idx, filename) for filename in revision.cards],
            )
            for revision in book.revisions
        ],
        # Undecided entries first, then the other changed ones -- they are
        # what a reader opens a multi-revision book to find.
        entries=[
            _entry_out(lore, line, book.name)
            for line in sorted(book.lines, key=lambda line: (line.resolved, -len(line.versions)))
        ],
        similar=similar,
        merges=[_merge_out(m) for m in book.merges],
        attaching=[_card_out(idx, filename) for filename in waiting.attach.get(book.id, ())],
        detaching=[_card_out(idx, filename) for filename in waiting.detach.get(book.id, ())],
    )


@router.get(
    "/characters/{card_id}/lorebooks",
    response_model=list[CardLorebookOut],
    summary="The lorebooks a card's embedded book was assembled from, in entry order",
)
def card_lorebooks(card_id: str) -> list[CardLorebookOut]:
    idx, lore = _indexes()
    record = _shared.require(idx, card_id)
    waiting = _waiting(idx, lore)
    detaching = {book_id for a, book_id in waiting.by_card.get(record.filename, ()) if a.op == "detach"}
    out: list[CardLorebookOut] = []
    for part, lorebook_id in lore.for_card(record.filename):
        book = lore.get(lorebook_id)
        if book is None:
            continue
        behind = sum(
            1
            for digest, line in zip(part.hashes, lore.part_lines(part))
            if line is not None and line.chosen and line.chosen != digest
        )
        out.append(
            CardLorebookOut(
                lorebook_id=lorebook_id,
                name=book.name,
                kind=part.kind,
                entry_count=len(part.hashes),
                card_count=len(book.cards),
                revision=part.fingerprint,
                is_newest=book.revisions[0].fingerprint == part.fingerprint,
                revision_count=len(book.revisions),
                state="detaching" if lorebook_id in detaching else "attached",
                pending_upgrades=0 if lorebook_id in detaching else behind,
            )
        )
    for attachment, lorebook_id in waiting.by_card.get(record.filename, ()):
        book = lore.get(lorebook_id)
        if attachment.op != "attach" or book is None:
            continue
        out.append(
            CardLorebookOut(
                lorebook_id=lorebook_id,
                name=book.name,
                kind=book.kinds[0] if book.kinds else "embedded",
                entry_count=len(book.lines),
                card_count=len(book.cards),
                revision="",
                is_newest=True,
                revision_count=len(book.revisions),
                state="attaching",
            )
        )
    return out


def _set_attached(card_id: str, lorebook_id: str, want: bool) -> list[CardLorebookOut]:
    """Record that a card should (or should not) carry a lorebook. Asking for
    what the card already is withdraws any decision to the contrary -- which is
    how a pending attach or detach is undone."""
    idx, lore = _indexes()
    record = _shared.require(idx, card_id)
    book = _require_book(lore, lorebook_id)
    waiting = _waiting(idx, lore)
    store = _store()
    # Whatever anchor an earlier decision about this pair was stored under.
    earlier = {a.id for a, book_id in waiting.by_card.get(record.filename, ()) if book_id == book.id}
    _write(lambda: store.drop_attachments(earlier))
    if (book.id in lorebook_sync.carried(lore, record.filename)) != want:
        _write(
            lambda: store.set_attachment(
                record.filename,
                book.anchor,
                "attach" if want else "detach",
                card_id=record.card_id,
                card_name=record.name,
                lorebook_name=book.name,
            )
        )
    return card_lorebooks(record.filename)


@router.put(
    "/characters/{card_id}/lorebooks/{lorebook_id}",
    response_model=list[CardLorebookOut],
    summary="Attach a lorebook to a card at the next sync",
)
def attach_lorebook(card_id: str, lorebook_id: str) -> list[CardLorebookOut]:
    return _set_attached(card_id, lorebook_id, True)


@router.delete(
    "/characters/{card_id}/lorebooks/{lorebook_id}",
    response_model=list[CardLorebookOut],
    summary="Detach a lorebook from a card at the next sync",
)
def detach_lorebook(card_id: str, lorebook_id: str) -> list[CardLorebookOut]:
    return _set_attached(card_id, lorebook_id, False)


def _entry_count(book: Any) -> int:
    return sum(len(lorebook_split.usable(s.entries)) for s in lorebook_split.segments(book))


@router.get(
    "/characters/{card_id}/lorebook-original",
    response_model=LorebookOriginalOut,
    summary="The lorebook a card had before its first sync, if a sync has rewritten it",
)
def lorebook_original(card_id: str) -> LorebookOriginalOut:
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    token = lorebook_snapshots.pointer(record.extensions)
    snapshot = _write(lambda: _snapshots().get(token))
    if snapshot is None:
        return LorebookOriginalOut(exists=False)
    try:
        _outer, data = edit.read_card(idx.root / record.filename)
    except edit.WriteError as exc:
        raise _shared.write_error(exc) from exc
    current = data.get("character_book")
    return LorebookOriginalOut(
        exists=True,
        taken_at=snapshot.taken_at,
        entry_count=_entry_count(snapshot.book),
        differs=(current if isinstance(current, dict) else None) != snapshot.book,
    )


@router.post(
    "/characters/{card_id}/lorebook-original/restore",
    response_model=LorebookOriginalOut,
    summary="Put back the lorebook a card had before its first sync",
)
def restore_lorebook_original(card_id: str) -> LorebookOriginalOut:
    """Undo every sync of this card at once. Decisions are left alone, so a
    card restored while a chosen version still stands is simply pending again."""
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    try:
        restored = _write(lambda: lorebook_sync.restore(idx, record.filename, _snapshots()))
    except edit.WriteError as exc:
        raise _shared.write_error(exc) from exc
    if not restored:
        raise HTTPException(status_code=404, detail="this card's lorebook has never been synced")
    catalog.index().refresh(force=True)
    return lorebook_original(record.filename)
