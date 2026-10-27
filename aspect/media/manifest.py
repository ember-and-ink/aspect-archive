"""State for server-side media downloads (Phase 3C-1) -- docs/PHASE_3C_PLAN.md §3.

Two stores, deliberately separate:

**The per-gallery manifest** (`<gallery>/.media.json`) is what makes a
character's media state travel with its folder and survive a rename --
folders resolve by gallery id (`gallery.resolve_folder`), so the manifest
does too. A dotfile, so it is already excluded from every listing and scan
(`v1.py:list_gallery_files`, `gallery.py:_scan`). It records which source URL
became which local file (`files`), which URLs are permanently gone for *this*
gallery specifically (`dead`), a short history of runs, and -- see "The source
ledger" below -- the disposition of every media *source* the card carries
(`sources`), which is what lets a run decide it has nothing to do without
touching the network.

**The global dead-URL ledger** (`data/state/dead_urls.json`) is the
cross-character win: the same broken catbox link appears on dozens of cards,
so a URL confirmed dead once should never be re-fetched from any of them.
Ported from `web/modules/media-dedup.js`'s ledger half (`_ledger`,
`recordFailure`, `recordSuccess`, `isDead`, `deadReason`, the eviction rule)
-- same entry shape, same thresholds, same "delete on success" behaviour.
Unlike the JS version this one is never poisoned by our own missing `/proxy`
route (docs/PHASE_3C_PLAN.md §1): the writer that fills it in only ever sees
real fetch outcomes. Per §6/§9 the two old localStorage blobs are discarded,
not migrated, when this lands.

Both stores are written atomically via `edit.write_atomic` -- a reader
mid-write (the browser polling `GET .../media` while a run is in flight)
must never see a truncated JSON file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from aspect.cards import edit
from aspect.config import settings

MANIFEST_NAME = ".media.json"
_MANIFEST_VERSION = 1

# Verbatim MAX_TRANSIENT_ATTEMPTS / MAX_LEDGER_ENTRIES (media-dedup.js:42,46).
MAX_TRANSIENT_ATTEMPTS = 4
MAX_LEDGER_ENTRIES = 5000
_LEDGER_VERSION = 1


# --------------------------------------------------------------------------
# Per-gallery manifest
# --------------------------------------------------------------------------


def manifest_path(gallery_dir: Path) -> Path:
    return gallery_dir / MANIFEST_NAME


def load_manifest(gallery_dir: Path) -> dict[str, Any]:
    """The manifest for one gallery folder, or a fresh skeleton if there is
    none yet or it can't be parsed -- a corrupt manifest must not block new
    downloads, only lose its own history."""
    path = manifest_path(gallery_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return _empty_manifest()
    try:
        data = json.loads(raw)
    except ValueError:
        return _empty_manifest()
    if not isinstance(data, dict):
        return _empty_manifest()
    data.setdefault("version", _MANIFEST_VERSION)
    data.setdefault("files", {})
    data.setdefault("dead", {})
    data.setdefault("runs", [])
    if not isinstance(data["files"], dict):
        data["files"] = {}
    if not isinstance(data["dead"], dict):
        data["dead"] = {}
    if not isinstance(data["runs"], list):
        data["runs"] = []
    # `sources` is deliberately *not* defaulted here: its absence is meaningful
    # (a manifest written before the ledger existed) and `sources_satisfied`
    # needs to tell that apart from an empty one. See below.
    if "sources" in data and not isinstance(data["sources"], dict):
        del data["sources"]
    return data


def empty_manifest() -> dict[str, Any]:
    """A fresh, unsaved manifest -- for a card that has no gallery folder yet
    (never had media downloaded) rather than one whose `.media.json` is
    merely missing or corrupt."""
    return _empty_manifest()


def _empty_manifest() -> dict[str, Any]:
    return {"version": _MANIFEST_VERSION, "updated": None, "files": {}, "dead": {}, "runs": []}


def save_manifest(gallery_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["updated"] = _now_iso()
    payload = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    edit.write_atomic(manifest_path(gallery_dir), payload)


def record_saved(
    manifest: dict[str, Any], url: str, file_name: str, sha256: str, *, size: int | None = None
) -> None:
    entry: dict[str, Any] = {"file": file_name, "sha256": sha256, "at": _now_iso()}
    if size is not None:
        entry["size"] = size
    manifest["files"][url] = entry
    manifest["dead"].pop(url, None)


def record_dead(manifest: dict[str, Any], url: str, reason: str, *, attempts: int = 1) -> None:
    manifest["dead"][url] = {"reason": reason, "attempts": attempts, "at": _now_iso()}


# --------------------------------------------------------------------------
# The source ledger -- what was seen, and what we could do about it
# --------------------------------------------------------------------------
#
# `files` and `dead` answer "which image URLs became which local file, and
# which are gone". Neither can express three things that turn out to decide
# whether a card is finished:
#
#   - a gallery *root* we resolved. `files` holds a MEGA folder's decrypted
#     children (`mega://handle/key`), never `https://mega.nz/folder/X#Y`; the
#     same for a Civitai post, a catbox album, a Chub project.
#   - a URL we saw and had no downloader for. This is the whole reason cards
#     with Civitai links sat un-downloaded for months without ever looking
#     broken: nothing recorded that the URL existed.
#   - a URL we saw and deliberately skipped (an mp3, under the images-only
#     policy). Not an error, not a file -- a decision.
#
# So `sources` records the disposition of every source a card carries, keyed
# by URL (or `chub:<project id>` for the one source that isn't a link anyone
# wrote). `h` is the handler id *at the time of the run*, `st` the outcome.
# Recording the handler is what makes the ledger self-healing: when a URL
# recorded `unhandled` gains a handler, that card -- and only that card --
# re-arms on the next Localize all. No version stamp, no archive-wide rescan,
# and the same mechanism covers every extractor added after this one.

SOURCE_DONE = "done"
SOURCE_UNHANDLED = "unhandled"
SOURCE_IGNORED = "ignored"
SOURCE_FAILED = "failed"


def record_source(
    manifest: dict[str, Any],
    key: str,
    handler: str | None,
    status: str,
    *,
    count: int | None = None,
    reason: str | None = None,
) -> None:
    """Note what became of one source. `count` is how many images a gallery
    root resolved to -- informational, and the only way to see afterwards that
    a post which resolved to nothing was actually reached."""
    sources = manifest.setdefault("sources", {})
    entry: dict[str, Any] = {"h": handler, "st": status, "at": _now_iso()}
    if count is not None:
        entry["n"] = count
    if reason:
        entry["r"] = str(reason)[:200]
    sources[key] = entry


def effective_sources(manifest: dict[str, Any], refs: "Iterable[Any]") -> dict[str, Any]:
    """The ledger as the skip check should see it: what `files`/`dead` already
    prove, overlaid with what runs have explicitly recorded.

    A direct image URL needs no ledger entry of its own -- `files` says it was
    saved, `dead` says it is gone, and every skip path in
    `writer.download_item` writes one or the other. Deriving those keeps the
    manifest from carrying the same fact twice, and means a manifest written
    long before this ledger existed already answers for the great majority of
    its sources.

    What cannot be derived is exactly what `record_source` stores: gallery
    roots (`files` holds their children, never them), URLs with no handler, and
    URLs skipped by the images-only policy before any fetch. So a pre-ledger
    card re-runs iff it has a gallery source -- once -- and everything else
    migrates for free.
    """
    files = manifest["files"]
    dead = manifest["dead"]
    view: dict[str, Any] = {}
    for ref in refs:
        if ref.handler not in ("embedded", "lorebook"):
            continue
        if ref.key in files:
            view[ref.key] = {"h": ref.handler, "st": SOURCE_DONE}
        elif ref.key in dead:
            view[ref.key] = {"h": ref.handler, "st": SOURCE_FAILED}

    stored = manifest.get("sources")
    if isinstance(stored, dict):
        view.update(stored)
    return view


def sources_satisfied(manifest: dict[str, Any], refs: "Iterable[Any]") -> bool:
    """Whether every source this card carries has already been dealt with, by
    code no older than what is running now.

    A card re-runs when any ref is unrecorded (a URL new to the card, a gallery
    root on a pre-ledger manifest, or a fetch that failed transiently and so
    recorded nothing), when a ref recorded `unhandled` now has a handler, or
    when the handler that dealt with it has been replaced by a different one.
    Everything else -- downloaded, permanently gone, deliberately ignored --
    counts as satisfied.

    The middle clause is the one that matters: it is what makes adding
    `media/civitai.py` re-arm precisely the cards carrying Civitai links, on
    the next ordinary Localize all, with no rescan of the archive and nothing
    for anyone to remember to press.
    """
    refs = list(refs)
    view = effective_sources(manifest, refs)
    for ref in refs:
        entry = view.get(ref.key)
        if not isinstance(entry, dict):
            return False
        if entry.get("st") == SOURCE_UNHANDLED and ref.handler is not None:
            return False
        if entry.get("h") != ref.handler:
            return False
    return True


def append_run(manifest: dict[str, Any], run: dict[str, Any]) -> None:
    manifest["runs"].append(run)
    # Bounded the same way the ledger is -- a long-lived gallery shouldn't grow
    # its manifest without limit; only the recent history is ever read.
    if len(manifest["runs"]) > 200:
        manifest["runs"] = manifest["runs"][-200:]


# --------------------------------------------------------------------------
# Global dead-URL ledger
# --------------------------------------------------------------------------


def load_dead_ledger() -> dict[str, Any]:
    """`{url: {n, f, l, p, s, e}}`, same short-key entry shape as the JS
    ledger it replaces (media-dedup.js "Entry shape" comment): attempt count,
    first-seen, last-attempt, permanent flag, last HTTP status, last error
    text."""
    try:
        raw = settings.dead_urls_file.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict) or data.get("version") != _LEDGER_VERSION:
        return {}
    urls = data.get("urls")
    return urls if isinstance(urls, dict) else {}


def save_dead_ledger(ledger: dict[str, Any]) -> None:
    _evict_if_needed(ledger)
    payload = json.dumps({"version": _LEDGER_VERSION, "urls": ledger}, ensure_ascii=False).encode("utf-8")
    edit.write_atomic(settings.dead_urls_file, payload)


def _evict_if_needed(ledger: dict[str, Any]) -> None:
    """Port of `evictIfNeeded` (media-dedup.js:374-379): drop the
    least-recently-touched entries once the ledger grows past the cap."""
    if len(ledger) <= MAX_LEDGER_ENTRIES:
        return
    ordered = sorted(ledger.items(), key=lambda kv: kv[1].get("l", 0))
    drop = len(ledger) - MAX_LEDGER_ENTRIES
    for url, _ in ordered[:drop]:
        del ledger[url]


def is_dead(ledger: dict[str, Any], url: str) -> bool:
    """Port of `isDead` (media-dedup.js:413-417)."""
    entry = ledger.get(url)
    if not entry:
        return False
    return bool(entry.get("p") == 1 or entry.get("n", 0) >= MAX_TRANSIENT_ATTEMPTS)


def dead_reason(ledger: dict[str, Any], url: str) -> str:
    """Port of `deadReason` (media-dedup.js:420-425)."""
    entry = ledger.get(url)
    if not entry:
        return ""
    if entry.get("p") == 1:
        status = entry.get("s")
        return f"HTTP {status}" if status else (entry.get("e") or "permanently unavailable")
    return f"failed {entry.get('n', 0)} times ({entry.get('e') or 'unknown'})"


def record_failure(
    ledger: dict[str, Any], url: str, *, permanent: bool, status: int | None, message: str
) -> dict[str, Any]:
    """Port of `recordFailure` (media-dedup.js:433-448). Returns
    `{dead, permanent, attempts}` -- `dead` is what the caller checks to
    decide whether this run's classification of the URL just became final."""
    now = time.time()
    entry = ledger.get(url) or {"n": 0, "f": now}
    entry["n"] = entry.get("n", 0) + 1
    entry["l"] = now
    entry["s"] = status
    entry["e"] = str(message or "")[:200]
    if permanent:
        entry["p"] = 1
    ledger[url] = entry

    is_permanent = entry.get("p") == 1
    return {
        "dead": is_permanent or entry["n"] >= MAX_TRANSIENT_ATTEMPTS,
        "permanent": is_permanent,
        "attempts": entry["n"],
    }


def record_success(ledger: dict[str, Any], url: str) -> None:
    """Port of `recordSuccess` (media-dedup.js:451-454): a URL that works
    again drops out of the ledger rather than accumulating a clean history."""
    ledger.pop(url, None)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Kept as an alias for the module's own internal callers written before this
# was made public.
_now_iso = now_iso
