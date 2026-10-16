"""The archive index: every card in `data/characters`, held in memory.

Deliberately not a database. A full scan -- decode each PNG's tEXt chunk, base64
decode, JSON parse -- costs 0.72 s warm across the whole 3,839-card archive, so
an in-memory dict rebuilt at startup and kept honest with a stat sweep is enough
indefinitely. SQLite would add a schema, a migration story and a second source of
truth to reconcile, in exchange for saving under a second once per process.

Two consequences of that choice worth stating, because they are what make it
hold up:

* **The filesystem is the source of truth, always.** The index is a cache of it,
  never the authority. A card dropped in by hand, renamed, or deleted outside
  this process shows up on the next refresh with no reindex step, which is also
  how a userscript acquisition lands in the browse grid live.
* **Summaries in memory, full cards from disk.** The index holds only what
  browsing needs (name, creator, tags, counts). A detail view re-reads and
  re-parses the one file it is about -- under a millisecond, and never stale.

Refresh is a stat of every file, comparing (mtime_ns, size) against what was
parsed last time; only changed and new files are re-read. On this archive that
sweep is single-digit milliseconds, so endpoints can call it per request behind a
short debounce rather than trusting a startup snapshot.

Cards that fail to parse are *recorded*, not skipped -- `CardSummary.error` says
why. An archive silently dropping the files it cannot read is the exact failure
an archive exists to prevent, so they surface as a data-quality view instead.
"""

from __future__ import annotations

import logging
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from proxy.cards import dates, pngtools
from proxy.cards.naming import id_fragment
from proxy.config import settings

logger = logging.getLogger("jai_proxy.archive")

# How long a refresh's result is trusted before another stat sweep is allowed.
# The sweep is milliseconds, but a browse page fires a dozen requests at once and
# there is no reason for each to re-stat 3,839 files.
_REFRESH_DEBOUNCE_SECONDS = 2.0


def _text(value: Any) -> str:
    """A string field off a card, defensively: cards come from five importers and
    a `None` or a number where a string belongs must not break a whole scan."""
    return value if isinstance(value, str) else ""


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())


# The fields that go into the prompt sent to a model, and so into "how big is
# this card?". Kept in this order and this membership because it is the set
# CharacterLibrary's own size estimate sums -- a browse grid ordered by size has
# to agree with the number shown on the card.
_PROMPT_FIELDS = ("description", "personality", "scenario", "first_mes", "system_prompt")


def _lore_entry_count(data: dict[str, Any]) -> int:
    book = data.get("character_book")
    if not isinstance(book, dict):
        return 0
    entries = book.get("entries")
    return len(entries) if isinstance(entries, list) else 0


@dataclass(frozen=True, slots=True)
class CardSummary:
    """What browsing a card needs, without its prose.

    `filename` is the identity. Not the card id, not a surrogate key: the exact
    name of the file on disk. It is what the archive is addressed by, what the
    inherited avatar thumbnail cache is keyed on, and what a human sees in a
    directory listing -- so making it the API's id means there is one name for a
    card across the whole system, and a mismatch between the index and the disk
    is visible rather than inferred. `card_id` (the source's own id) and
    `fragment` (its `_<id8>` slice) travel alongside for lookups that key on
    those instead.
    """

    filename: str
    size: int
    mtime: float
    # Identity as the *source* knows it, not as the filesystem does.
    card_id: str = ""
    fragment: str = ""
    gallery_id: str = ""
    # The root original's `_<id8>` fragment, for a fork -- "" for everything
    # else. Flattened (docs/FORKS_AND_EXTRAS_PLAN.md §3): a fork of a fork
    # still carries the *original's* fragment here, never an intermediate
    # fork's. A field rather than a scan over `extensions` because both the
    # browse filter chip and the Related pane's "forks of this card" query
    # need it filterable the same way `has_gallery`/`favorite` already are.
    fork_of: str = ""
    # Card content, flattened for filtering and display.
    name: str = ""
    creator: str = ""
    page_name: str = ""
    tags: tuple[str, ...] = ()
    source_kind: str = ""
    source_url: str = ""
    linked_at: str = ""
    # When the card was created, as the card itself records it: SillyTavern's
    # root-level `create_date`, or -- for a card written before this archive
    # stamped one -- the earliest provider `linkedAt` that field is derived
    # from. See proxy/cards/dates.py for why that is the right source and why
    # `linked_at` above (which is `jai`'s, rewritten by the bulk passes) is not.
    create_date: str = ""
    character_version: str = ""
    greeting_count: int = 0
    lore_entry_count: int = 0
    description_chars: int = 0
    # Characters across the five fields that make up a card's prompt --
    # description, personality, scenario, first_mes, system_prompt. A count, not
    # the text: it answers "how big is this card?" for sorting and for the
    # data-quality view, which is the only thing the prose was wanted for at list
    # level. Divided by four it is the same rough token estimate the frontend
    # would compute if it held the prose itself.
    prompt_chars: int = 0
    has_creator_notes: bool = False
    has_example_dialogue: bool = False
    # Starred by the user, out of `extensions.fav`. A bool on the summary rather
    # than a lookup into `extensions` because the grid filters and paints on it,
    # and the extensions blob is 790 bytes a card that a list request does not
    # otherwise pay for. SillyTavern also mirrors this at the envelope root, so
    # the parse reads both -- but only the extensions copy survives a patch (see
    # `pngtools.embed_card`), which is why writes go there.
    favorite: bool = False
    # The avatar's pixel size, out of the PNG header the scan has already read.
    # For the low-res filter -- see proxy/cards/avatar_refetch.py.
    avatar_width: int = 0
    avatar_height: int = 0
    # The card's whole `data.extensions` block, verbatim. Kept here -- unlike
    # every other piece of prose-adjacent content -- because it is not prose: it
    # is identity. Provider links, gallery_id, version uids and the per-source
    # provenance blocks all live in it, so a client that has the summary but not
    # the extensions cannot tell where a card came from or which gallery is its
    # own. 790 bytes a card, ~3 MB across the archive, and served only when a
    # request asks for it. Excluded from equality: it is derived from the same
    # bytes as everything else, and hashing a dict would not work anyway.
    extensions: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    # Why this card could not be read, when it could not be. Empty for the
    # healthy majority; a card with an error carries only its file fields.
    error: str = ""

    # What a text search matches against, folded once at parse time rather than
    # per query: a query scans all 3,839 records, and case-folding six fields per
    # record per keystroke is the difference between a filter that feels instant
    # and one that does not. Prose is deliberately excluded -- matching
    # descriptions means keeping 40 MB of text resident for a feature nobody has
    # asked for; when it is wanted it belongs in a deep-search path that reads
    # from disk. Derived, so it is not an argument and not part of equality.
    haystack: str = field(init=False, repr=False, compare=False, default="")

    def __post_init__(self) -> None:
        parts = (self.name, self.creator, self.page_name, self.filename, *self.tags)
        object.__setattr__(self, "haystack", " ".join(parts).casefold())

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class RefreshStats:
    """What a refresh actually did, for logging and for the /stats endpoint."""

    scanned: int = 0
    parsed: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    seconds: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.parsed or self.removed)


def summarize_data(data: dict[str, Any], outer: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything a summary derives from a card's `data` object alone -- no file
    involved.

    Split out of `summarize` below so the Discover preview can describe a card
    that only exists in a provider's API response using exactly the counting the
    archive applies to a card on disk (`proxy/api/v1/discover.py`). A greeting
    count or a prompt weight that disagreed between the preview and the card you
    end up with would be worse than showing nothing.

    Returns the CardSummary field names, so a caller can splat it."""
    outer = outer if isinstance(outer, dict) else {}
    extensions = data.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    # Every card this archive holds carries an `extensions.jai` block regardless
    # of which importer wrote it -- it is stamped by CardBuilder, not by the
    # source -- so it is the one reliable place to read provenance from. Cards
    # from elsewhere (hand-dropped, another tool) simply have blank provenance
    # rather than being rejected.
    jai = extensions.get("jai")
    jai = jai if isinstance(jai, dict) else {}
    gallery_id = extensions.get("gallery_id")
    fork = extensions.get("fork")
    fork = fork if isinstance(fork, dict) else {}

    card_id = _text(jai.get("id"))
    return {
        "card_id": card_id,
        # From the id where there is one, so it matches what the filename was
        # built from; short source ids yield a short fragment, same as on disk.
        "fragment": id_fragment(card_id),
        "gallery_id": gallery_id if isinstance(gallery_id, str) else "",
        "fork_of": _text(fork.get("of")),
        "name": _text(data.get("name")),
        "creator": _text(data.get("creator")) or _text(jai.get("creatorName")),
        "page_name": _text(jai.get("pageName")),
        "tags": _string_list(data.get("tags")),
        "source_kind": _text(jai.get("sourceKind")),
        "source_url": _text(jai.get("source_url")),
        "linked_at": _text(jai.get("linkedAt")),
        # Read from the envelope root, not from `data`: `create_date` is one of
        # the few fields SillyTavern keeps outside the card body. Resolved
        # rather than read straight so a card that predates the stamp still
        # sorts and displays -- the derivation is the same one the writers use.
        "create_date": dates.resolve_create_date(outer, data),
        "character_version": _text(data.get("character_version")),
        # The primary greeting counts: a card always has one, and "3 greetings"
        # should mean what a reader would expect it to mean.
        "greeting_count": (1 if _text(data.get("first_mes")) else 0)
        + len(_string_list(data.get("alternate_greetings"))),
        "lore_entry_count": _lore_entry_count(data),
        "description_chars": len(_text(data.get("description"))),
        "prompt_chars": sum(len(_text(data.get(f))) for f in _PROMPT_FIELDS),
        "has_creator_notes": bool(_text(data.get("creator_notes")).strip()),
        "has_example_dialogue": bool(_text(data.get("mes_example")).strip()),
        "favorite": _favorite(outer, extensions),
        "extensions": extensions,
    }


def summarize(path: Path, stat_result: Any | None = None) -> CardSummary:
    """Read one card PNG into a summary. Never raises: a card that cannot be
    parsed comes back as a summary carrying the reason in `error`, because the
    caller is a scan of thousands and one bad file must not end it."""
    st = stat_result or path.stat()
    base = {"filename": path.name, "size": st.st_size, "mtime": st.st_mtime}
    try:
        raw = path.read_bytes()
        envelope = pngtools.read_envelope(raw)
    except (OSError, ValueError, TypeError) as exc:
        return CardSummary(**base, error=f"{type(exc).__name__}: {exc}")
    if envelope is None:
        # `read_envelope` answers one question (is there a card?) with one value,
        # so the reason has to be recovered separately. Only on the failure path,
        # which is why healthy cards are not charged for a second parse.
        return CardSummary(**base, error=_diagnose(raw))

    outer, data = envelope
    if not isinstance(data, dict):
        return CardSummary(**base, error="card `data` is not an object")

    width, height = pngtools.image_size(raw)
    return CardSummary(**base, **summarize_data(data, outer), avatar_width=width, avatar_height=height)


def _favorite(outer: dict[str, Any], extensions: dict[str, Any]) -> bool:
    """Whether this card is starred, from either place the flag is written.

    SillyTavern keeps `fav` at the envelope root *and* under
    `data.extensions`, and cards that passed through it can carry either or
    both -- and either as the string `"true"`, which is what its own settings
    round-trip produces. Reading is therefore tolerant of all four shapes.
    Writing is not: only the extensions copy survives `pngtools.embed_card`,
    which rebuilds the envelope from the spec header plus `data`.
    """
    for value in (extensions.get("fav"), outer.get("fav")):
        if value is True or value == "true":
            return True
    return False


def _diagnose(raw: bytes) -> str:
    """Why these bytes carry no readable card, in terms a human can act on.

    Three distinct problems -- a file that is not a PNG, a PNG that was never a
    card, and a card whose payload is corrupt -- want three different responses
    (delete it, ignore it, re-export it). Collapsing them into "unreadable" makes
    the data-quality view a list to squint at instead of a list to work through.
    """
    try:
        chunks = pngtools.read_text_chunks(raw)
    except (ValueError, TypeError) as exc:
        return str(exc)  # "not a PNG stream"
    if not (chunks.get("ccv3") or chunks.get("chara")):
        return "no character card embedded"
    return "card payload is not decodable (bad base64, or not a JSON object)"


def _fold(filename: str) -> str:
    """The key a tolerant filename lookup uses.

    Two normalizations, both earned the hard way on this archive. NFC because
    APFS is Unicode-normalization-insensitive: a name typed `é` and a name stored
    `e` + U+0301 are the same file to the filesystem but different strings to
    Python, and a URL round-trip can hand back either form. Casefold because the
    same filesystem is case-insensitive, so `Abbie_x.png` opens the file stored
    as `abbie_x.png` -- the index must resolve what the disk would resolve.
    """
    return unicodedata.normalize("NFC", filename).casefold()


class ArchiveIndex:
    """Every card in one directory, kept in step with the directory.

    Read paths take a snapshot of the record mapping rather than holding a lock
    for the duration of a query: records are frozen, the mapping is replaced
    wholesale on refresh, so a request either sees the state before a refresh or
    the state after it and never a half-updated index.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        debounce_seconds: float = _REFRESH_DEBOUNCE_SECONDS,
    ) -> None:
        self.root = root or settings.archive_dir
        self._debounce = debounce_seconds
        self._records: dict[str, CardSummary] = {}
        self._folded: dict[str, str] = {}
        self._by_fragment: dict[str, tuple[CardSummary, ...]] = {}
        self._stat_keys: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._last_refresh = 0.0
        self.last_stats = RefreshStats()

    # --- reading -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[CardSummary]:
        return iter(tuple(self._records.values()))

    def all(self) -> tuple[CardSummary, ...]:
        """Every record, healthy or not, in directory order."""
        return tuple(self._records.values())

    def cards(self) -> tuple[CardSummary, ...]:
        """Only the cards that parsed. What browse endpoints list."""
        return tuple(r for r in self._records.values() if r.ok)

    def broken(self) -> tuple[CardSummary, ...]:
        """The cards that did not parse -- the data-quality view."""
        return tuple(r for r in self._records.values() if not r.ok)

    def get(self, filename: str) -> CardSummary | None:
        """A record by filename, exact match first, then normalization-tolerant.
        See `_fold` for why the fallback exists."""
        record = self._records.get(filename)
        if record is not None:
            return record
        resolved = self._folded.get(_fold(filename))
        return self._records.get(resolved) if resolved else None

    def path_of(self, filename: str) -> Path | None:
        """The on-disk path for a record, or None when nothing matches. Resolved
        through the index rather than by joining the argument onto the root, so a
        traversal attempt (`../../etc/passwd`) cannot name a file: it either
        matches an indexed filename or it does not exist."""
        record = self.get(filename)
        return self.root / record.filename if record else None

    def by_fragment(self, fragment: str) -> tuple[CardSummary, ...]:
        """Records carrying this `_<id8>` fragment. The archive's duplicate and
        already-saved checks key on the fragment alone (never the name -- a
        rename breaks a name-pinned lookup), so a lookup by it belongs here.
        A dict lookup, built once per rescan -- not a scan of every record,
        which is what a caller checking many fragments (Discover's "have"
        guard, potentially hundreds of ids a page) would otherwise pay for
        each one."""
        return self._by_fragment.get(fragment, ()) if fragment else ()

    def fragments(self) -> frozenset[str]:
        """Every `_<id8>` fragment currently on disk.

        The cheap half of "do I have this": no filesystem walk (unlike
        `PngWriter.existing`, which globs per id), just the keys of the dict
        `by_fragment` already maintains. Meant to be fetched once and matched
        against locally, the way Discover's have-guard does."""
        return frozenset(self._by_fragment)

    # --- refreshing ----------------------------------------------------------

    def refresh(self, *, force: bool = False) -> RefreshStats:
        """Bring the index in step with the directory: stat everything, re-read
        what changed, drop what is gone. Cheap enough to call per request, and
        debounced so a page's worth of parallel requests sweeps once."""
        now = time.monotonic()
        if not force and now - self._last_refresh < self._debounce:
            return self.last_stats
        with self._lock:
            # Another thread may have refreshed while this one waited.
            if not force and self._last_refresh > now:
                return self.last_stats
            stats = self._rescan()
            self._last_refresh = time.monotonic()
            self.last_stats = stats
        if stats.changed:
            logger.info(
                "archive: %d cards (%d parsed, %d removed, %d unreadable) in %.2fs",
                len(self._records),
                stats.parsed,
                stats.removed,
                stats.failed,
                stats.seconds,
            )
        return stats

    def _rescan(self) -> RefreshStats:
        started = time.perf_counter()
        stats = RefreshStats()
        records: dict[str, CardSummary] = {}
        folded: dict[str, str] = {}
        stat_keys: dict[str, tuple[int, int]] = {}

        for path, st in self._iter_card_files():
            filename = path.name
            stats.scanned += 1
            key = (st.st_mtime_ns, st.st_size)
            cached = self._records.get(filename)
            # A card is re-read only when its bytes could have changed. Size
            # alongside mtime because a same-second in-place rewrite of a
            # different length is the realistic edit here (a card repaired by
            # check_cards.py), and it changes the size every time.
            if cached is not None and self._stat_keys.get(filename) == key:
                records[filename] = cached
                stats.unchanged += 1
            else:
                record = summarize(path, st)
                records[filename] = record
                stats.parsed += 1
            if not records[filename].ok:
                stats.failed += 1
            stat_keys[filename] = key
            folded[_fold(filename)] = filename

        by_fragment: dict[str, list[CardSummary]] = {}
        for record in records.values():
            if record.fragment:
                by_fragment.setdefault(record.fragment, []).append(record)

        stats.removed = len(set(self._records) - set(records))
        self._records = records
        self._folded = folded
        self._by_fragment = {k: tuple(v) for k, v in by_fragment.items()}
        self._stat_keys = stat_keys
        stats.seconds = time.perf_counter() - started
        return stats

    def _iter_card_files(self) -> Iterable[tuple[Path, Any]]:
        """Every card PNG under the archive root, with its stat, sorted by name
        so the index has a stable order and `nested` layouts (a `<creator>/`
        level) are picked up as well as flat ones. `scandir` via `Path.rglob`
        would re-stat; we need the stat anyway, so take it once here."""
        try:
            entries = sorted(self.root.rglob("*.png"), key=lambda p: p.name)
        except OSError as exc:
            logger.warning("archive: cannot list %s: %s", self.root, exc)
            return
        for path in entries:
            try:
                st = path.stat()
            except OSError:
                continue  # vanished mid-scan; the next refresh settles it
            if path.is_file():
                yield path, st


# The process-wide index. One directory, one index, built lazily on first use so
# importing this module (in a test, in a script) does not scan 3 GB.
_index: ArchiveIndex | None = None
_index_lock = threading.Lock()


def index() -> ArchiveIndex:
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = ArchiveIndex()
                _index.refresh(force=True)
    return _index
