"""On-disk cache of raw lorebook payloads, keyed by (source, lorebook id).

A lorebook is the unit creators reuse across many characters ("standard"
lorebooks get referenced whole, again and again), and fetching one is the slow
part of an export -- saucepan in particular needs one HTTP round trip PER
CHAPTER. So the first time we see a lorebook we stash its raw fetched payload
here; every later character that references the same lorebook id skips the fetch
entirely. The userscript asks `split()` up front and only pulls the misses.

The stored blob is the platform's raw payload, verbatim and opaque -- this cache
never interprets it. For saucepan that's `{id, list, chapters}` with the
chapters still obfuscated (the per-payload mask travels inside the blob), so a
cache-loaded lorebook is byte-identical to a freshly fetched one and the mapper
is unchanged. `source` namespaces the id space so JanitorAI script ids and
saucepan lorebook uuids can't collide (only saucepan is wired today).

It is purely a speed cache: stale content is corrected by clearing it (the
overlay CLEAR button -> POST /clear-lorebooks), never by an eviction policy.
Files are read lazily by key rather than slurped into memory on boot, since the
cache can grow large.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from aspect.config import ensure_dir, settings

logger = logging.getLogger("aspect.state.lorebook_cache")

_SAFE_RE = re.compile(r"[^a-z0-9._-]+")


def _slug(text: str) -> str:
    return _SAFE_RE.sub("_", (text or "").strip().lower()).strip("_") or "unnamed"


class LorebookCache:
    def __init__(self, cache_dir: Path | None = None) -> None:
        self._dir = cache_dir or settings.lorebook_cache_dir
        # ensure_dir, not mkdir: constructed at `import aspect.deps`, before the
        # server's preflight can report an unusable mount -- see
        # aspect.config.ensure_dir.
        ensure_dir(self._dir)

    def _path(self, source: str, lorebook_id: str) -> Path:
        # A readable prefix (real ids are filesystem-safe uuids) for eyeballing
        # the cache dir, plus a hash of the exact (source, id) so two distinct
        # ids can never slug onto the same file.
        digest = hashlib.sha1(f"{source}\x00{lorebook_id}".encode("utf-8")).hexdigest()[:16]
        return self._dir / f"{_slug(source)}_{_slug(lorebook_id)[:48]}_{digest}.json"

    def has(self, source: str, lorebook_id: str) -> bool:
        return bool(lorebook_id) and self._path(source, lorebook_id).is_file()

    def split(self, source: str, ids: list[str]) -> tuple[list[str], list[str]]:
        """Partition `ids` into (cached, missing), preserving order and dropping
        blanks/duplicates -- exactly the two lists the userscript needs: `cached`
        to reference by id in the build, `missing` to actually fetch."""
        cached: list[str] = []
        missing: list[str] = []
        seen: set[str] = set()
        for raw_id in ids:
            lid = (raw_id or "").strip()
            if not lid or lid in seen:
                continue
            seen.add(lid)
            (cached if self.has(source, lid) else missing).append(lid)
        return cached, missing

    def get(self, source: str, lorebook_id: str) -> dict[str, Any] | None:
        path = self._path(source, lorebook_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("failed to read cached lorebook %s", path)
            return None

    def put(self, source: str, lorebook_id: str, blob: dict[str, Any]) -> None:
        if not lorebook_id:
            return
        path = self._path(source, lorebook_id)
        try:
            path.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.exception("failed to write cached lorebook %s", path)

    def clear(self) -> int:
        removed = 0
        for path in self._dir.glob("*.json"):
            if path.is_file():
                path.unlink()
                removed += 1
        return removed

    @property
    def count(self) -> int:
        return sum(1 for path in self._dir.glob("*.json") if path.is_file())
