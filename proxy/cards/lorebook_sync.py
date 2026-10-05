"""Rewriting a card's embedded `character_book`: the write half of
`proxy.cards.lorebook_split`.

A sync changes a card's book in three ways, and `rebuild` does all three in one
pass over the book's segments:

* **Upgrade** -- an entry is replaced by another version of the same entry.
* **Detach** -- a whole source lorebook is taken off the card.
* **Attach** -- another lorebook's entries are appended.

Everything it did not mean to change is put back as it was: entries that are not
being replaced are the very same objects, in the same order, with whatever
fields the importer wrote -- a Chub entry carries ones our own models would drop.

Every book it writes is stamped (`extensions.jai_lorebooks`, read back by
`lorebook_split.segments`) with the lorebooks it holds and how many entries
belong to each. That is what keeps a rewritten book recognisable: a lorebook
with no provider id is identified by its content, and an upgrade changes the
content. The stamp carries the identity the part had *before* it was rewritten.

Pure functions over plain dicts, no I/O. What to upgrade, attach and detach is
decided by `proxy.archive.lorebook_sync`, which knows the archive.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from proxy.cards import lorebook_split
from proxy.cards.lorebook_split import STAMP_KEY, Segment


@dataclass(frozen=True, slots=True)
class Attachment:
    """A lorebook to append to a card. `entries` are whole entry objects with
    no `[Title] ` prefix on their comments (see `clean_entry`)."""

    title: str
    kind: str
    refs: tuple[str, ...]
    entries: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class Outcome:
    upgraded: int = 0
    # Entries dropped because the card already held the version they were to
    # become -- it carried the old and the new text of one entry side by side.
    removed: int = 0
    attached: int = 0
    detached: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.upgraded or self.removed or self.attached or self.detached)


def clean_entry(entry: dict[str, Any], comment_prefix: str = "") -> dict[str, Any]:
    """A copy of `entry` fit to be put on another card: its own, and without
    the `[Title] ` prefix its comment carries on a card that holds several
    books."""
    cleaned = copy.deepcopy(entry)
    comment = cleaned.get("comment")
    if comment_prefix and isinstance(comment, str) and comment.startswith(comment_prefix):
        cleaned["comment"] = comment[len(comment_prefix) :]
    return cleaned


def _placed(entry: dict[str, Any], *, entry_id: Any, display_index: Any, comment_prefix: str) -> dict[str, Any]:
    """`entry` (already a clean copy) given a place on this card: the slot's
    own id and display position, and this card's comment prefix. Everything
    else about the entry -- its text, what triggers it, its ordering weight --
    is the version's, not the card's."""
    if entry_id is None:
        entry.pop("id", None)
    else:
        entry["id"] = entry_id
    extensions = entry.get("extensions")
    if isinstance(extensions, dict) or display_index is not None:
        extensions = dict(extensions) if isinstance(extensions, dict) else {}
        if display_index is None:
            extensions.pop("display_index", None)
        else:
            extensions["display_index"] = display_index
        entry["extensions"] = extensions
    if comment_prefix and isinstance(entry.get("comment"), str):
        entry["comment"] = f"{comment_prefix}{entry['comment']}"
    return entry


def _stamp(segment: Segment, entry_count: int, identity: tuple[str, ...]) -> dict[str, Any]:
    return {
        "refs": list(identity),
        "title": segment.title,
        "kind": segment.kind,
        "entry_count": entry_count,
        "prefix": segment.comment_prefix,
    }


def identity(segment: Segment) -> tuple[str, ...]:
    """What a segment is known by once its content may have changed: its
    provider ids, or -- for a book that has none -- the content it had. A book
    that was already stamped keeps the identity it was stamped with."""
    if segment.refs:
        return segment.refs
    hashes = [lorebook_split.entry_hash(e) for e in lorebook_split.usable(segment.entries)]
    return (f"fp:{lorebook_split.fingerprint(hashes)}",)


def rebuild(
    book: Any,
    segments: list[Segment],
    *,
    drop: set[int] | frozenset[int] = frozenset(),
    replace: dict[int, dict[int, dict[str, Any]]] | None = None,
    attach: list[Attachment] | tuple[Attachment, ...] = (),
) -> tuple[dict[str, Any] | None, Outcome]:
    """The book a card should carry, given `segments` (its current book, cut
    by `lorebook_split.segments`) and what to do to them.

    `drop` holds the indices of segments to remove. `replace` maps a segment
    index to `{position in segment.entries: replacement entry}`; a replacement
    is a clean entry (`clean_entry`) and is copied again before it is placed.
    `attach` is appended in order.

    Returns `(None, outcome)` when nothing is left -- a card whose only
    lorebook was detached has no book.
    """
    replace = replace or {}
    entries: list[Any] = []
    stamps: list[dict[str, Any]] = []
    upgraded = removed = detached = 0

    for index, segment in enumerate(segments):
        if index in drop:
            detached += 1
            continue
        swaps = replace.get(index, {})
        # Hashes this segment will hold once rewritten. Two entries of one card
        # can be versions of the same entry -- merged by hand, or a book that
        # had its new entries appended without the old ones being taken out.
        # Both resolve to the one chosen version, and the card should hold it
        # once.
        held = {
            lorebook_split.entry_hash(entry)
            for position, entry in enumerate(segment.entries)
            if position not in swaps and lorebook_split.usable((entry,))
        }
        kept: list[Any] = []
        for position, entry in enumerate(segment.entries):
            new = swaps.get(position) if isinstance(entry, dict) else None
            if new is None:
                kept.append(entry)
                continue
            target = lorebook_split.entry_hash(new)
            if target in held:
                removed += 1
                continue
            held.add(target)
            upgraded += 1
            extensions = entry.get("extensions")
            kept.append(
                _placed(
                    copy.deepcopy(new),
                    entry_id=entry.get("id"),
                    display_index=extensions.get("display_index") if isinstance(extensions, dict) else None,
                    comment_prefix=segment.comment_prefix,
                )
            )
        if not kept:
            continue
        entries.extend(kept)
        stamps.append(_stamp(segment, len(kept), identity(segment)))

    next_id = max((e["id"] for e in entries if isinstance(e, dict) and isinstance(e.get("id"), int)), default=-1) + 1
    attached = 0
    for addition in attach:
        if not addition.entries:
            continue
        attached += 1
        for entry in addition.entries:
            entries.append(
                _placed(copy.deepcopy(entry), entry_id=next_id, display_index=len(entries), comment_prefix="")
            )
            next_id += 1
        stamps.append(
            {
                "refs": list(addition.refs),
                "title": addition.title,
                "kind": addition.kind,
                "entry_count": len(addition.entries),
                "prefix": "",
            }
        )

    outcome = Outcome(upgraded=upgraded, removed=removed, attached=attached, detached=detached)
    if not entries:
        return None, outcome

    if isinstance(book, dict):
        rebuilt = dict(book)
    else:
        # A card that had no book: start one, shaped like the importers' own.
        rebuilt = {"name": attach[0].title if attach else "", "description": ""}
    extensions = rebuilt.get("extensions")
    extensions = dict(extensions) if isinstance(extensions, dict) else {}
    extensions[STAMP_KEY] = stamps
    rebuilt["extensions"] = extensions
    rebuilt["entries"] = entries
    return rebuilt, outcome
