"""When Discover first saw each Following card, persisted at `data/discover_seen.json`.

WHY THIS EXISTS
The question browsing Following actually asks is *what is new since I last
looked?* The ignore list this replaced tried to answer it by subtraction --
mark everything you don't want, and what is left is new -- and that cannot
keep up with how much gets published. This answers it directly instead: every
id a Following feed returns is stamped with the moment it first came back, and
"Recent only" shows the ones stamped inside the grace window.

The snapshot is of what the feed *returned*, not of everything each creator has
published. A Following load is a fixed-size query (each creator's newest page,
plus Chub's timeline), so nothing here pages to the end of anything; a card
that only ever sat past the edge of that query was never on screen to be new.

WHAT THIS MODULE KNOWS
That ids are strings, providers are buckets of them, and each id maps to one
epoch-milliseconds stamp. Nothing about what an id *means* -- Chub's are short
integers, DataCat's are uuids -- and the bucket names are not validated here
either: the route's `Literal` is where the known providers are enumerated.

The stamp is the server's clock, not the browser's, so two tabs (or a laptop
with a drifting clock) agree on when a card was first seen.

FIRST-SEEN IS NEVER REWRITTEN
Recording an id already present is a no-op; there is no update and no removal.
A card's stamp moving forward would make it "new" again, which is the one wrong
answer this store exists to avoid. The file therefore only grows, bounded by
how many distinct cards your follows publish, which is far below the ceilings.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("jai_proxy.state.seen")

# A ceiling on what will be read back, guarding a damaged or runaway file. A
# DataCat entry (36-char uuid + stamp) is ~60 bytes of JSON, so this is several
# hundred thousand cards.
MAX_BYTES = 32 * 1024 * 1024

# Per-provider ceiling, so a client bug that records in a loop fails loudly at a
# known size instead of growing the file until it stops loading.
MAX_IDS_PER_PROVIDER = 250_000

# A single provider id's length. Chub's are ~6 digits and DataCat's are 36-char
# uuids; this is slack, not a guess at a format.
MAX_ID_CHARS = 128

# Every record is a read-modify-write of the whole file. Route handlers are sync
# and run on FastAPI's threadpool, so two tabs refreshing at once would
# otherwise race and one feed's new stamps would be lost -- and come back as
# "new" with a later stamp on the next load.
_lock = threading.Lock()


class SeenError(Exception):
    """The stored stamps could not be read, or the given ids are unusable."""


def normalize(ids: list[str]) -> list[str]:
    """Trim, drop blanks, drop duplicates, keep first-seen order.

    Raises on an id too long to be one.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in ids:
        value = raw.strip()
        if not value:
            continue
        if len(value) > MAX_ID_CHARS:
            raise SeenError(
                f"id is {len(value)} characters, over the {MAX_ID_CHARS}-character ceiling"
            )
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def now_ms() -> int:
    return int(time.time() * 1000)


class SeenStore:
    """Read/record access to one JSON object of `{provider: {id: epoch_ms}}`.

    Reads are not cached: clearing a provider's bucket by hand -- to start the
    snapshot over -- is a reasonable thing to do that a cache would hide until
    a restart.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, dict[str, int]]:
        """Every bucket. `{}` when nothing has been seen yet.

        A missing file is normal and reads as empty. A file that exists but does
        not parse raises instead: degrading to `{}` would make every card in the
        next feed "new", and the next record would write that over the history.

        Rows whose stamp is not a number are dropped rather than raising -- the
        rest of the bucket is still a usable history.
        """
        if not self.path.is_file():
            return {}
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise SeenError(f"could not read {self.path}: {exc}") from exc
        if len(raw) > MAX_BYTES:
            raise SeenError(f"{self.path} is {len(raw)} bytes, over the {MAX_BYTES}-byte ceiling")
        try:
            blob = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SeenError(f"{self.path} is not valid JSON: {exc}") from exc
        if not isinstance(blob, dict):
            raise SeenError(f"{self.path} holds a {type(blob).__name__}, expected a JSON object")
        return {
            provider: {
                key: int(stamp)
                for key, stamp in bucket.items()
                if isinstance(key, str)
                and key.strip()
                and isinstance(stamp, (int, float))
                and not isinstance(stamp, bool)
            }
            for provider, bucket in blob.items()
            if isinstance(provider, str) and isinstance(bucket, dict)
        }

    def record(
        self, provider: str, ids: list[str], *, now: int | None = None
    ) -> tuple[int, dict[str, int]]:
        """Stamp every id not already in the bucket with `now`.

        Returns `(newly stamped, {id: first seen})` for exactly the given ids --
        not the whole bucket, which the client has no use for and which grows
        without bound. Ids already present keep their original stamp.
        """
        wanted = normalize(ids)
        stamp = now_ms() if now is None else now
        with _lock:
            blob = self.read()
            bucket = blob.get(provider, {})
            added = [value for value in wanted if value not in bucket]
            if len(bucket) + len(added) > MAX_IDS_PER_PROVIDER:
                raise SeenError(
                    f"{provider} would hold {len(bucket) + len(added)} ids, over the "
                    f"{MAX_IDS_PER_PROVIDER}-id ceiling"
                )
            if added:
                for value in added:
                    bucket[value] = stamp
                blob[provider] = bucket
                self._write(blob)
                logger.info("seen: +%d on %s (%d total)", len(added), provider, len(bucket))
        return len(added), {value: bucket[value] for value in wanted}

    def _write(self, blob: dict[str, Any]) -> None:
        """Replace the file atomically -- temp file, fsync, `os.replace`."""
        try:
            text = json.dumps(blob, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise SeenError(f"seen stamps are not JSON-serialisable: {exc}") from exc
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_BYTES:
            raise SeenError(
                f"seen stamps are {len(encoded)} bytes, over the {MAX_BYTES}-byte ceiling"
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".seen-", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise SeenError(f"could not write {self.path}: {exc}") from exc
        logger.debug("seen stamps written to %s (%d bytes)", self.path, len(encoded))
