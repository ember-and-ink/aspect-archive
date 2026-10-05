"""The lorebook index: every lorebook the archive's cards embed, deduplicated.

Derived, like `proxy.archive.catalog` and for the same reason -- the cards on
disk are the source of truth, and this is a cache of what they say. Nothing here
is persisted and nothing is written back to a card.

Three levels, built from `proxy.cards.lorebook_split`:

* A **part** is one source lorebook as one card carries it.
* A **lorebook** is every part that is the same book: parts sharing a provider
  id (a JanitorAI script id, a Chub project path), or with no id but identical
  entries. Its distinct entry-sets are its **revisions** -- the same book as
  different cards captured it over time.
* An **entry line** is one entry of a lorebook across its revisions, matched by
  title. A line with more than one version is an entry that changed upstream,
  which is what "upgrading" cards means.

Lorebook ids are derived from the provider id where there is one and from the
content otherwise, so they are stable across restarts as long as the cards are.

The one input that is not a card is `proxy.state.lorebook_decisions`: merges the
user declared (two lorebooks, or two entries, that are really one) and the
version chosen for an entry that has several. They are applied here, on top of
what the cards say, and a merge shifts the ids of what it joins -- which is why
decisions are stored against content, not against these ids.

Cost: only cards the catalog already knows carry lore are read, and each is
re-read only when its (mtime, size) changes, so the build after the first is a
dict walk.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from proxy.archive import catalog
from proxy.cards import lorebook_split, pngtools
from proxy.state.lorebook_decisions import Decisions, Merge

logger = logging.getLogger("jai_proxy.archive.lorebooks")


@dataclass(frozen=True, slots=True)
class EntryVersion:
    """One distinct entry text, as first seen. Shared by every card and every
    lorebook that carries it."""

    hash: str
    title: str
    keys: tuple[str, ...]
    secondary_keys: tuple[str, ...]
    content: str
    constant: bool


@dataclass(frozen=True, slots=True)
class CardPart:
    """A `BookPart` reduced to what the index keeps: hashes, not entries."""

    filename: str
    position: int
    title: str
    kind: str
    refs: tuple[str, ...]
    hashes: tuple[str, ...]
    # Per entry, the key its line is matched on within the lorebook.
    line_keys: tuple[str, ...]
    fingerprint: str


@dataclass(slots=True)
class Revision:
    fingerprint: str
    hashes: tuple[str, ...]
    cards: list[str] = field(default_factory=list)
    # The latest date any card carrying this revision was created -- the only
    # ordering available, since entries themselves carry no timestamp.
    newest: str = ""


@dataclass(slots=True)
class LineVersion:
    hash: str
    cards: list[str] = field(default_factory=list)
    newest: str = ""


@dataclass(slots=True)
class EntryLine:
    id: str
    lorebook_id: str
    title: str
    versions: list[LineVersion] = field(default_factory=list)
    # The version every card should carry, when the user has picked one.
    chosen: str = ""
    # Entry merges that put versions on this line which titles alone did not.
    merges: list[Merge] = field(default_factory=list)

    @property
    def card_count(self) -> int:
        return len({c for v in self.versions for c in v.cards})

    @property
    def resolved(self) -> bool:
        """Nothing left to decide: one version, or one picked."""
        return len(self.versions) < 2 or bool(self.chosen)

    @property
    def pending_cards(self) -> set[str]:
        """Cards a sync would rewrite: those holding a version other than the
        chosen one."""
        if not self.chosen:
            return set()
        return {c for v in self.versions if v.hash != self.chosen for c in v.cards}


@dataclass(slots=True)
class Similar:
    lorebook_id: str
    shared: int
    # "subset" (this book is contained in the other), "superset", or "overlap".
    relation: str


@dataclass(slots=True)
class Lorebook:
    id: str
    name: str
    kinds: tuple[str, ...]
    refs: tuple[str, ...]
    creators: tuple[str, ...]
    cards: tuple[str, ...]
    revisions: list[Revision]
    lines: list[EntryLine]
    entry_hashes: frozenset[str]
    chars: int
    # What a merge decision names this lorebook by: its provider id, else its
    # content. Unlike `id` it does not move when the lorebook is merged.
    anchor: str = ""
    merges: list[Merge] = field(default_factory=list)
    similar: list[Similar] = field(default_factory=list)

    @property
    def changed_lines(self) -> int:
        return sum(1 for line in self.lines if len(line.versions) > 1)

    @property
    def unresolved_lines(self) -> int:
        return sum(1 for line in self.lines if not line.resolved)

    @property
    def pending_cards(self) -> set[str]:
        return {c for line in self.lines for c in line.pending_cards}


@dataclass(frozen=True, slots=True)
class LorebookStats:
    cards: int = 0
    lorebooks: int = 0
    shared_lorebooks: int = 0
    entries_embedded: int = 0
    entries_unique: int = 0
    changed_lines: int = 0
    unresolved_lines: int = 0
    pending_cards: int = 0
    seconds: float = 0.0


def _line_key(entry: dict[str, Any], prefix: str, seen: Counter[str]) -> str:
    """What matches an entry to its earlier and later versions: its title,
    falling back to its trigger keys. Suffixed with an ordinal because titles
    repeat within one book -- two "Notes" entries are two entries, not two
    versions of one."""
    base = lorebook_split.norm_text(lorebook_split.entry_title(entry, prefix))
    if not base:
        keys = entry.get("keys") if isinstance(entry.get("keys"), list) else []
        base = "|".join(sorted(lorebook_split.norm_text(k) for k in keys if isinstance(k, str)))
    if not base:
        base = lorebook_split.entry_hash(entry)
    seen[base] += 1
    return f"{base}\x00{seen[base]}"


class _Groups:
    """Union-find over part indices."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        self._parent[self.find(a)] = self.find(b)


class LorebookIndex:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # filename -> ((mtime, size), parts). The per-card cache a rebuild reuses.
        self._card_parts: dict[str, tuple[tuple[float, int], tuple[CardPart, ...]]] = {}
        self._versions: dict[str, EntryVersion] = {}
        self._signature: tuple[frozenset[tuple[str, float, int]], Any] | None = None
        self._root: Any = None
        self._lorebooks: dict[str, Lorebook] = {}
        self._lines: dict[str, EntryLine] = {}
        self._by_card: dict[str, tuple[tuple[CardPart, str], ...]] = {}
        # Identity key (a provider ref, or `fp:<fingerprint>`) -> lorebook id.
        self._by_key: dict[str, str] = {}
        # (filename, part position) -> per entry of that part, its line's id.
        self._part_lines: dict[tuple[str, int], tuple[str, ...]] = {}
        self.stats = LorebookStats()

    # --- reading -------------------------------------------------------------

    def lorebooks(self) -> tuple[Lorebook, ...]:
        return tuple(self._lorebooks.values())

    def get(self, lorebook_id: str) -> Lorebook | None:
        return self._lorebooks.get(lorebook_id)

    def lines(self) -> tuple[EntryLine, ...]:
        return tuple(self._lines.values())

    def line(self, line_id: str) -> EntryLine | None:
        return self._lines.get(line_id)

    def version(self, entry_hash: str) -> EntryVersion | None:
        return self._versions.get(entry_hash)

    def for_card(self, filename: str) -> tuple[tuple[CardPart, str], ...]:
        """A card's parts in entry order, each with the id of its lorebook."""
        return self._by_card.get(filename, ())

    def by_key(self, *keys: str) -> Lorebook | None:
        """The lorebook known by any of `keys` -- provider refs, a stored
        anchor, or `fp:<fingerprint>` for a book identified by its content.
        How a decision, which cannot hold an id, names a lorebook."""
        for key in keys:
            found = self._by_key.get(key)
            if found is not None:
                return self._lorebooks.get(found)
        return None

    def part_lines(self, part: CardPart) -> tuple[EntryLine | None, ...]:
        """The line each entry of `part` belongs to, aligned with
        `part.hashes`. By position rather than by hash: the same text can sit
        on two lines of one lorebook (an entry retitled without being
        reworded), and only the card's own copy says which it is."""
        ids = self._part_lines.get((part.filename, part.position), ())
        return tuple(self._lines.get(line_id) for line_id in ids)

    # --- building ------------------------------------------------------------

    def refresh(
        self, idx: catalog.ArchiveIndex, decisions: Decisions | None = None, decisions_key: Any = None
    ) -> None:
        """Bring the index in step with the archive and the stored decisions.
        A no-op unless a card that carries lore was added, removed or rewritten
        since the last call, or `decisions_key` changed."""
        decisions = decisions or Decisions()
        records = [r for r in idx.cards() if r.lore_entry_count > 0]
        signature = (frozenset((r.filename, r.mtime, r.size) for r in records), decisions_key)
        if signature == self._signature and idx.root == self._root:
            return
        with self._lock:
            if signature == self._signature:
                return
            if idx.root != self._root:
                # A different directory (a test's, a repointed archive): the
                # per-card cache is keyed by filename and means nothing there.
                self._card_parts, self._versions, self._root = {}, {}, idx.root
            started = time.perf_counter()
            card_parts: dict[str, tuple[tuple[float, int], tuple[CardPart, ...]]] = {}
            read = 0
            for record in records:
                key = (record.mtime, record.size)
                cached = self._card_parts.get(record.filename)
                if cached is not None and cached[0] == key:
                    card_parts[record.filename] = cached
                    continue
                card_parts[record.filename] = (key, self._read_parts(idx, record))
                read += 1
            self._card_parts = card_parts
            self._build({r.filename: r for r in records}, decisions)
            self._signature = signature
            seconds = time.perf_counter() - started
            self.stats = LorebookStats(
                cards=sum(1 for _, parts in card_parts.values() if parts),
                lorebooks=len(self._lorebooks),
                shared_lorebooks=sum(1 for b in self._lorebooks.values() if len(b.cards) > 1),
                entries_embedded=sum(len(p.hashes) for _, parts in card_parts.values() for p in parts),
                entries_unique=len({h for b in self._lorebooks.values() for h in b.entry_hashes}),
                changed_lines=sum(b.changed_lines for b in self._lorebooks.values()),
                unresolved_lines=sum(b.unresolved_lines for b in self._lorebooks.values()),
                pending_cards=len({c for b in self._lorebooks.values() for c in b.pending_cards}),
                seconds=seconds,
            )
            logger.info(
                "lorebooks: %d books over %d cards (%d cards read) in %.2fs",
                len(self._lorebooks),
                self.stats.cards,
                read,
                seconds,
            )

    def _read_parts(self, idx: catalog.ArchiveIndex, record: catalog.CardSummary) -> tuple[CardPart, ...]:
        try:
            envelope = pngtools.read_envelope((idx.root / record.filename).read_bytes())
        except (OSError, ValueError, TypeError):
            return ()
        if envelope is None or not isinstance(envelope[1], dict):
            return ()
        data = envelope[1]
        parts: list[CardPart] = []
        for position, part in enumerate(lorebook_split.split_book(data.get("character_book"), record.extensions)):
            seen: Counter[str] = Counter()
            hashes: list[str] = []
            line_keys: list[str] = []
            for entry in part.entries:
                digest = lorebook_split.entry_hash(entry)
                hashes.append(digest)
                line_keys.append(_line_key(entry, part.comment_prefix, seen))
                if digest not in self._versions:
                    keys = entry.get("keys") if isinstance(entry.get("keys"), list) else []
                    secondary = (
                        entry.get("secondary_keys") if isinstance(entry.get("secondary_keys"), list) else []
                    )
                    content = entry.get("content")
                    self._versions[digest] = EntryVersion(
                        hash=digest,
                        title=lorebook_split.entry_title(entry, part.comment_prefix),
                        keys=tuple(k for k in keys if isinstance(k, str)),
                        secondary_keys=tuple(k for k in secondary if isinstance(k, str)),
                        content=content if isinstance(content, str) else "",
                        constant=entry.get("constant") is True,
                    )
            parts.append(
                CardPart(
                    filename=record.filename,
                    position=position,
                    title=part.title,
                    kind=part.kind,
                    refs=part.refs,
                    hashes=tuple(hashes),
                    line_keys=tuple(line_keys),
                    fingerprint=lorebook_split.fingerprint(hashes),
                )
            )
        return tuple(parts)

    def _build(self, records: dict[str, catalog.CardSummary], decisions: Decisions) -> None:
        parts = [p for _, card in self._card_parts.values() for p in card]

        # Two parts are the same lorebook when they share a provider id, or
        # carry identical entries. The content key is what joins an id-less
        # copy (a re-adopted PNG, a saucepan card) to the book it came from.
        groups = _Groups(len(parts))
        first_with: dict[str, int] = {}
        for i, part in enumerate(parts):
            for key in (*part.refs, f"fp:{part.fingerprint}"):
                if key in first_with:
                    groups.union(i, first_with[key])
                else:
                    first_with[key] = i
        # Then the merges the user declared, each naming two lorebooks by a key
        # one of their parts carries. A merge whose keys match nothing any more
        # (the cards changed, or went) joins nothing.
        for merge in decisions.lorebook_merges:
            if merge.a in first_with and merge.b in first_with:
                groups.union(first_with[merge.a], first_with[merge.b])
        members: dict[int, list[CardPart]] = {}
        for i, part in enumerate(parts):
            members.setdefault(groups.find(i), []).append(part)

        def when(filename: str) -> str:
            record = records[filename]
            return record.create_date or record.linked_at

        lorebooks: dict[str, Lorebook] = {}
        lines: dict[str, EntryLine] = {}
        by_card: dict[str, list[tuple[CardPart, str]]] = {}
        by_key: dict[str, str] = {}
        part_lines: dict[tuple[str, int], tuple[str, ...]] = {}
        for group in members.values():
            # A synced part of a book with no provider id carries the content
            # identity it had before the sync as a ref (`fp:...`); those are
            # identity, but not something to show as a source.
            refs = tuple(sorted({r for p in group for r in p.refs if not r.startswith("fp:")}))
            # The provider id when there is one (it survives the book's content
            # changing); otherwise the content itself, which for an id-less
            # group is a single fingerprint by construction.
            # A merged id-less group holds several; the smallest keeps the id
            # independent of card order. A part that carries a stamped
            # fingerprint counts by that one, not by what it holds now, so
            # syncing a lorebook does not move its id.
            if refs:
                anchor = refs[0]
            else:
                anchor = min(k for p in group for k in (p.refs or (f"fp:{p.fingerprint}",)))
            lorebook_id = lorebook_split.digest(anchor)[:12]
            keys = {r for p in group for r in p.refs} | {f"fp:{p.fingerprint}" for p in group}
            for key in keys:
                by_key[key] = lorebook_id

            revisions: dict[str, Revision] = {}
            line_versions: dict[str, dict[str, LineVersion]] = {}
            for part in sorted(group, key=lambda p: p.filename):
                date = when(part.filename)
                revision = revisions.setdefault(part.fingerprint, Revision(part.fingerprint, part.hashes))
                if part.filename not in revision.cards:
                    revision.cards.append(part.filename)
                    revision.newest = max(revision.newest, date)
                for digest, line_key in zip(part.hashes, part.line_keys):
                    version = line_versions.setdefault(line_key, {}).setdefault(digest, LineVersion(digest))
                    if part.filename not in version.cards:
                        version.cards.append(part.filename)
                        version.newest = max(version.newest, date)
                by_card.setdefault(part.filename, []).append((part, lorebook_id))

            line_versions, line_merges, folded = self._merge_lines(line_versions, decisions.entry_merges)

            book_lines: list[EntryLine] = []
            for line_key, versions in line_versions.items():
                # Newest first: with no timestamp on an entry, the version on the
                # most recently created card is the best available guess at
                # "current". A guess -- the UI presents it as one.
                ordered = sorted(versions.values(), key=lambda v: (v.newest, len(v.cards)), reverse=True)
                picked = [v.hash for v in ordered if v.hash in decisions.chosen]
                chosen = max(picked, key=lambda h: decisions.chosen[h]) if picked else ""
                line = EntryLine(
                    id=lorebook_split.digest(f"{lorebook_id}\x00{line_key}")[:12],
                    lorebook_id=lorebook_id,
                    title=self._versions[chosen or ordered[0].hash].title,
                    versions=ordered,
                    chosen=chosen,
                    merges=line_merges.get(line_key, []),
                )
                book_lines.append(line)
                lines[line.id] = line
            line_ids = {line_key: line.id for line_key, line in zip(line_versions, book_lines)}
            for part in group:
                part_lines[(part.filename, part.position)] = tuple(
                    line_ids[folded.get(line_key, line_key)] for line_key in part.line_keys
                )

            cards = tuple(sorted({p.filename for p in group}))
            titles = Counter(p.title for p in group if p.title)
            if titles:
                name = titles.most_common(1)[0][0]
            elif len(cards) == 1:
                name = f"{records[cards[0]].name} lorebook".strip()
            else:
                name = "Untitled lorebook"
            entry_hashes = frozenset(h for p in group for h in p.hashes)
            lorebooks[lorebook_id] = Lorebook(
                id=lorebook_id,
                name=name,
                kinds=tuple(sorted({p.kind for p in group})),
                refs=refs,
                creators=tuple(c for c, _ in Counter(records[f].creator for f in cards if records[f].creator).most_common()),
                cards=cards,
                revisions=sorted(revisions.values(), key=lambda r: (r.newest, len(r.cards)), reverse=True),
                lines=book_lines,
                entry_hashes=entry_hashes,
                chars=sum(len(self._versions[h].content) for h in entry_hashes),
                anchor=anchor,
                merges=[m for m in decisions.lorebook_merges if m.a in keys and m.b in keys],
            )

        self._relate(lorebooks)
        self._lorebooks = dict(sorted(lorebooks.items(), key=lambda kv: (-len(kv[1].cards), kv[1].name.casefold())))
        self._lines = lines
        self._by_key = by_key
        self._part_lines = part_lines
        self._by_card = {
            filename: tuple(sorted(pairs, key=lambda pair: pair[0].position)) for filename, pairs in by_card.items()
        }
        # Versions no card carries any more would otherwise accumulate for the
        # life of the process.
        live = {h for book in lorebooks.values() for h in book.entry_hashes}
        self._versions = {h: v for h, v in self._versions.items() if h in live}

    @staticmethod
    def _merge_lines(
        line_versions: dict[str, dict[str, LineVersion]], merges: list[Merge]
    ) -> tuple[dict[str, dict[str, LineVersion]], dict[str, list[Merge]], dict[str, str]]:
        """Fold together the lines of one lorebook that an entry merge names.
        A merge names two version hashes; the lines holding them become one.
        The third result maps each folded line's key to the key it now has."""
        holder: dict[str, str] = {}
        for line_key, versions in line_versions.items():
            for digest in versions:
                holder.setdefault(digest, line_key)
        applied = [m for m in merges if m.a in holder and m.b in holder and holder[m.a] != holder[m.b]]
        if not applied:
            return line_versions, {}, {}

        root: dict[str, str] = {}

        def find(key: str) -> str:
            while root.get(key, key) != key:
                key = root[key]
            return key

        for merge in applied:
            a, b = find(holder[merge.a]), find(holder[merge.b])
            if a != b:
                root[max(a, b)] = min(a, b)

        merged: dict[str, dict[str, LineVersion]] = {}
        for line_key, versions in line_versions.items():
            target = merged.setdefault(find(line_key), {})
            for digest, version in versions.items():
                kept = target.setdefault(digest, LineVersion(digest))
                kept.cards.extend(c for c in version.cards if c not in kept.cards)
                kept.newest = max(kept.newest, version.newest)
        line_merges: dict[str, list[Merge]] = {}
        for merge in applied:
            line_merges.setdefault(find(holder[merge.a]), []).append(merge)
        return merged, line_merges, {line_key: find(line_key) for line_key in line_versions}

    @staticmethod
    def _relate(lorebooks: dict[str, Lorebook]) -> None:
        """Mark lorebooks that share entries without being the same book --
        the candidates for a manual merge (a book re-published under a new id,
        an older copy embedded without one)."""
        holders: dict[str, list[str]] = {}
        for book in lorebooks.values():
            for digest in book.entry_hashes:
                holders.setdefault(digest, []).append(book.id)
        shared: dict[tuple[str, str], int] = Counter()
        for ids in holders.values():
            if len(ids) < 2:
                continue
            for i, a in enumerate(ids):
                for b in ids[i + 1 :]:
                    shared[(a, b) if a < b else (b, a)] += 1
        for (a, b), count in shared.items():
            size_a, size_b = len(lorebooks[a].entry_hashes), len(lorebooks[b].entry_hashes)
            # One shared entry between two large books is a coincidence (a
            # boilerplate "rules" entry); it only counts when it is the whole
            # of the smaller book.
            if count < 2 and count < min(size_a, size_b):
                continue
            for this, other, mine, theirs in ((a, b, size_a, size_b), (b, a, size_b, size_a)):
                relation = "subset" if count == mine else "superset" if count == theirs else "overlap"
                lorebooks[this].similar.append(Similar(other, count, relation))
        for book in lorebooks.values():
            book.similar.sort(key=lambda s: -s.shared)


# The process-wide index, built on first use -- same shape as `catalog.index()`.
_index: LorebookIndex | None = None
_index_lock = threading.Lock()


def index(
    idx: catalog.ArchiveIndex, decisions: Decisions | None = None, decisions_key: Any = None
) -> LorebookIndex:
    """The lorebook index, in step with `idx` and the given decisions."""
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = LorebookIndex()
    _index.refresh(idx, decisions, decisions_key)
    return _index
