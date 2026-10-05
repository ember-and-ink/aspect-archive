"""The lorebook sync: carrying the stored decisions out onto the cards.

Everything up to here is bookkeeping -- `proxy.state.lorebook_decisions` records
which version of an entry to keep and which lorebooks belong on which cards, and
`proxy.archive.lorebooks` reports what that would change. This module is the one
that changes it, and it only runs when asked.

A sync **overwrites the card in place**. That is a deliberate exception to the
archive's never-overwrite rule, which is about re-acquiring a card from its
source: this is an edit, the same kind as changing a field, and the result is
still one whole card with one whole materialized book. What makes it safe to be
that blunt is `proxy.state.lorebook_snapshots` -- the book a card had before its
first sync is kept, and `restore` puts it back.

Three steps, kept apart so each can be checked on its own:

* `pending` resolves the attach/detach decisions against the archive as it is.
* `plan` says, per card, what a sync would do. It reads no card.
* `apply` works out every card's new book first and writes second, so a card
  rewritten early in a run is never the source of an entry for a later one.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from proxy.archive import catalog, lorebooks
from proxy.cards import edit, lorebook_split, lorebook_sync
from proxy.state import lorebook_snapshots
from proxy.state.lorebook_decisions import Attachment, Decisions

logger = logging.getLogger("jai_proxy.archive.lorebook_sync")


@dataclass(slots=True)
class Pending:
    """The attach/detach decisions, resolved. An attachment that no longer
    means anything -- its card or lorebook is gone, or the card already is the
    way it asks -- is `moot`, and a sync clears it away."""

    # lorebook id -> filenames
    attach: dict[str, list[str]] = field(default_factory=dict)
    detach: dict[str, list[str]] = field(default_factory=dict)
    # filename -> the decisions that apply to it
    by_card: dict[str, list[tuple[Attachment, str]]] = field(default_factory=dict)
    moot: list[Attachment] = field(default_factory=list)

    def cards_of(self, lorebook_id: str) -> set[str]:
        return {*self.attach.get(lorebook_id, ()), *self.detach.get(lorebook_id, ())}


def find_card(idx: catalog.ArchiveIndex, filename: str, card_id: str = "") -> catalog.CardSummary | None:
    """The card a decision names: by filename, or -- when the file was renamed
    since -- by its source id, provided exactly one card carries it."""
    record = idx.get(filename)
    if record is not None or not card_id:
        return record
    matches = [r for r in idx.cards() if r.card_id == card_id]
    return matches[0] if len(matches) == 1 else None


def carried(lore: lorebooks.LorebookIndex, filename: str) -> set[str]:
    """Ids of the lorebooks a card holds now."""
    return {lorebook_id for _part, lorebook_id in lore.for_card(filename)}


def pending(idx: catalog.ArchiveIndex, lore: lorebooks.LorebookIndex, decisions: Decisions) -> Pending:
    out = Pending()
    for attachment in decisions.attachments:
        record = find_card(idx, attachment.card, attachment.card_id)
        book = lore.by_key(attachment.lorebook)
        if record is None or book is None:
            out.moot.append(attachment)
            continue
        holds = book.id in carried(lore, record.filename)
        if holds == (attachment.op == "attach"):
            out.moot.append(attachment)
            continue
        target = out.attach if attachment.op == "attach" else out.detach
        target.setdefault(book.id, []).append(record.filename)
        out.by_card.setdefault(record.filename, []).append((attachment, book.id))
    return out


@dataclass(frozen=True, slots=True)
class CardPlan:
    filename: str
    # Lorebooks whose chosen versions this card is behind on; how many entries
    # would take one; and how many would simply go, the card already holding
    # the chosen version beside the stale one.
    upgrade: frozenset[str] = frozenset()
    upgrades: int = 0
    duplicates: int = 0
    attach: tuple[str, ...] = ()
    detach: tuple[str, ...] = ()


def plan(
    lore: lorebooks.LorebookIndex,
    waiting: Pending,
    *,
    lorebook_id: str = "",
    cards: set[str] | None = None,
) -> list[CardPlan]:
    """What a sync would do, one row per card it would rewrite. Scoped to one
    lorebook's changes with `lorebook_id`, and to some cards with `cards`."""
    upgrade: dict[str, set[str]] = {}
    for book in lore.lorebooks():
        if lorebook_id and book.id != lorebook_id:
            continue
        for line in book.lines:
            for filename in line.pending_cards:
                upgrade.setdefault(filename, set()).add(book.id)

    def wanted(ops: dict[str, list[str]], filename: str) -> tuple[str, ...]:
        return tuple(
            book_id
            for book_id, filenames in ops.items()
            if filename in filenames and (not lorebook_id or book_id == lorebook_id)
        )

    filenames = set(upgrade)
    for ops in (waiting.attach, waiting.detach):
        for book_id, held in ops.items():
            if not lorebook_id or book_id == lorebook_id:
                filenames.update(held)
    if cards is not None:
        filenames &= cards

    plans: list[CardPlan] = []
    for filename in sorted(filenames):
        detach = wanted(waiting.detach, filename)
        # Upgrading a lorebook on its way off the card would be work thrown away.
        books = frozenset(upgrade.get(filename, ())) - set(detach)
        counts = _count(lore, filename, books)
        plans.append(
            CardPlan(
                filename=filename,
                upgrade=books,
                upgrades=counts["upgrade"],
                duplicates=counts["duplicate"],
                attach=wanted(waiting.attach, filename),
                detach=detach,
            )
        )
    return [p for p in plans if p.upgrade or p.attach or p.detach]


def _count(lore: lorebooks.LorebookIndex, filename: str, books: frozenset[str]) -> Counter[str]:
    """How many of a card's entries a sync would replace, and how many it would
    drop as duplicates -- the same walk `lorebook_sync.rebuild` makes, over the
    index's hashes instead of the card."""
    counts: Counter[str] = Counter()
    for part, book_id in lore.for_card(filename):
        if book_id not in books:
            continue
        stale = [
            line.chosen if line is not None and line.chosen and line.chosen != digest else ""
            for digest, line in zip(part.hashes, lore.part_lines(part))
        ]
        held = {digest for digest, target in zip(part.hashes, stale) if not target}
        for target in stale:
            if not target:
                continue
            counts["duplicate" if target in held else "upgrade"] += 1
            held.add(target)
    return counts


class _Donors:
    """Whole entry objects, by version hash, read off the cards that hold them.

    The index keeps an entry's text and keys but not the rest of it (ordering,
    probability, the provider's own extras), and an upgrade should bring all of
    that along. So the replacement is copied from a card that already carries
    the wanted version."""

    def __init__(self, idx: catalog.ArchiveIndex) -> None:
        self._idx = idx
        self._read: dict[str, dict[str, dict[str, Any]]] = {}

    def _entries(self, filename: str) -> dict[str, dict[str, Any]]:
        if filename not in self._read:
            found: dict[str, dict[str, Any]] = {}
            try:
                _outer, data = edit.read_card(self._idx.root / filename)
            except edit.WriteError:
                data = {}
            for segment in lorebook_split.segments(data.get("character_book"), data.get("extensions")):
                for entry in lorebook_split.usable(segment.entries):
                    found.setdefault(
                        lorebook_split.entry_hash(entry), lorebook_sync.clean_entry(entry, segment.comment_prefix)
                    )
            self._read[filename] = found
        return self._read[filename]

    def entry(self, line: lorebooks.EntryLine, entry_hash: str) -> dict[str, Any] | None:
        for version in line.versions:
            if version.hash != entry_hash:
                continue
            for filename in version.cards:
                found = self._entries(filename).get(entry_hash)
                if found is not None:
                    return found
        return None

    def lorebook(self, book: lorebooks.Lorebook) -> lorebook_sync.Attachment:
        """`book` as it should land on a card that does not have it: every
        entry, each in its chosen version, or its newest where none is chosen.
        Ordered as the newest revision has them, with entries that revision
        lacks after."""
        rank = {digest: position for position, digest in enumerate(book.revisions[0].hashes)} if book.revisions else {}
        targets = [(line, line.chosen or line.versions[0].hash) for line in book.lines if line.versions]
        targets.sort(key=lambda pair: rank.get(pair[1], len(rank)))
        entries = [entry for line, digest in targets if (entry := self.entry(line, digest)) is not None]
        return lorebook_sync.Attachment(
            title=book.name,
            kind=book.kinds[0] if book.kinds else "embedded",
            # What the attached copy is recognised by afterwards: the book's
            # provider ids, or the anchor of one that has none.
            refs=book.refs or (book.anchor,),
            entries=tuple(entries),
        )


@dataclass(frozen=True, slots=True)
class CardResult:
    filename: str
    upgraded: int = 0
    removed: int = 0
    attached: int = 0
    detached: int = 0
    snapshot_taken: bool = False
    error: str = ""


def _new_book(
    lore: lorebooks.LorebookIndex,
    donors: _Donors,
    card: CardPlan,
    data: dict[str, Any],
) -> tuple[dict[str, Any] | None, lorebook_sync.Outcome]:
    book = data.get("character_book")
    segments = lorebook_split.segments(book, data.get("extensions"))
    # The index's view of this card, part by part. It was built from the same
    # file, so its parts line up with the segments that have usable entries --
    # and it is checked rather than assumed, because what it says decides which
    # entries are overwritten.
    indexed = iter(lore.for_card(card.filename))
    drop: set[int] = set()
    replace: dict[int, dict[int, dict[str, Any]]] = {}
    held: set[str] = set()
    for position, segment in enumerate(segments):
        places = [i for i, entry in enumerate(segment.entries) if lorebook_split.usable((entry,))]
        if not places:
            continue
        part, owner_id = next(indexed, (None, ""))
        hashes = tuple(lorebook_split.entry_hash(segment.entries[i]) for i in places)
        if part is None or part.hashes != hashes:
            raise edit.WriteError(f"{card.filename} changed while the sync was being worked out; run it again")
        if owner_id in card.detach:
            drop.add(position)
            continue
        held.add(owner_id)
        if owner_id not in card.upgrade:
            continue
        for place, digest, line in zip(places, hashes, lore.part_lines(part)):
            if line is None or not line.chosen or line.chosen == digest:
                continue
            entry = donors.entry(line, line.chosen)
            if entry is not None:
                replace.setdefault(position, {})[place] = entry
    attach = [donors.lorebook(target) for book_id in card.attach if book_id not in held and (target := lore.get(book_id))]
    return lorebook_sync.rebuild(book, segments, drop=drop, replace=replace, attach=attach)


def apply(
    idx: catalog.ArchiveIndex,
    lore: lorebooks.LorebookIndex,
    plans: list[CardPlan],
    snapshots: lorebook_snapshots.SnapshotStore,
) -> list[CardResult]:
    """Carry `plans` out. One card failing does not stop the rest; its result
    says why. The caller refreshes the index afterwards."""
    donors = _Donors(idx)
    results: list[CardResult] = []
    ready: list[tuple[CardPlan, dict[str, Any], dict[str, Any] | None, lorebook_sync.Outcome]] = []
    for card in plans:
        try:
            _outer, data = edit.read_card(idx.root / card.filename)
            book, outcome = _new_book(lore, donors, card, data)
        except edit.WriteError as exc:
            results.append(CardResult(card.filename, error=str(exc)))
            continue
        if outcome.changed:
            ready.append((card, data, book, outcome))
        else:
            # Only when the entries to write could not be found on any card
            # that should hold them -- an unreadable card, in practice.
            results.append(CardResult(card.filename, error="the entries to write could not be read off any card"))

    for card, data, book, outcome in ready:
        path = idx.root / card.filename
        extensions = data.get("extensions")
        extensions = dict(extensions) if isinstance(extensions, dict) else {}
        taken = False
        try:
            if snapshots.get(lorebook_snapshots.pointer(extensions)) is None:
                extensions[lorebook_snapshots.POINTER_KEY] = snapshots.take(
                    filename=card.filename, name=str(data.get("name") or ""), book=data.get("character_book")
                )
                taken = True
            data["extensions"] = extensions
            if book is None:
                data.pop("character_book", None)
            else:
                data["character_book"] = book
            edit.patch_card(path, data)
        except (edit.WriteError, lorebook_snapshots.SnapshotError, OSError) as exc:
            results.append(CardResult(card.filename, error=str(exc)))
            continue
        logger.info(
            "lorebook sync: %s (%d upgraded, %d duplicates removed, %d attached, %d detached)",
            card.filename,
            outcome.upgraded,
            outcome.removed,
            outcome.attached,
            outcome.detached,
        )
        results.append(
            CardResult(
                card.filename,
                upgraded=outcome.upgraded,
                removed=outcome.removed,
                attached=outcome.attached,
                detached=outcome.detached,
                snapshot_taken=taken,
            )
        )
    return results


def restore(idx: catalog.ArchiveIndex, filename: str, snapshots: lorebook_snapshots.SnapshotStore) -> bool:
    """Put back the book a card had before its first sync. False when the card
    has no snapshot. The snapshot is kept -- it is still the original."""
    path = idx.root / filename
    _outer, data = edit.read_card(path)
    snapshot = snapshots.get(lorebook_snapshots.pointer(data.get("extensions")))
    if snapshot is None:
        return False
    if snapshot.book is None:
        data.pop("character_book", None)
    else:
        data["character_book"] = snapshot.book
    edit.patch_card(path, data)
    logger.info("lorebook sync: restored the original book on %s", filename)
    return True
