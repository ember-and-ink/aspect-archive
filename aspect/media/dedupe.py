"""Trash exact-duplicate gallery files left behind by media re-downloads.

Media occasionally gets re-fetched for a card whose gallery already holds it --
a re-run of a broken batch, a re-import, a rescan after a slight card change --
and the new download lands under a new `extgallery_<ts>_<name>.webp` /
`localized_media_<ts>_<name>.webp` filename (the timestamp in the name is a
batch position, not a content id; see `aspect/media/names.py`). The old file
stays on disk, byte-identical to the new one, just no longer named in the
gallery's `.media.json` (`aspect/media/manifest.py`) because that manifest was
repointed at the new file. Nothing else ever cleans these up, and it recurs
any time a card is rescanned, so this is exposed as a live server action
(`POST /api/v1/media/dedupe`, Settings -> Media) rather than a one-off script.

A file is only trashed when both are true:

  1. its name is not claimed by any current entry in the gallery's
     `.media.json` `files` map (i.e. nothing currently points at it), and
  2. its sha256 matches the sha256 of a file that *is* currently claimed.

That second check is the safety net -- it is what makes this "obvious
duplicate cleanup" rather than image similarity matching. A same-size,
similarly-named, or same-prefix file that isn't a byte-for-byte match of a
live file is left alone (`unresolved`), on the assumption it is a manually
added image the manifest never knew about rather than a stale re-download.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from aspect.archive import thumbs
from aspect.cards import edit
from aspect.media import manifest as media_manifest


@dataclass
class DedupeResult:
    folders_touched: int = 0
    files_trashed: int = 0
    bytes_freed: int = 0
    unresolved: int = 0
    details: list[str] = field(default_factory=list)
    unresolved_details: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dedupe_galleries(
    root: Path, *, apply: bool, thumb_store: thumbs.ThumbnailStore | None = None
) -> DedupeResult:
    """Scan every gallery folder under `root` and trash (when `apply`) exact
    duplicates per the rule above. Always safe to call with `apply=False` --
    nothing is read but the manifests and file bytes."""
    store = thumb_store or thumbs.ThumbnailStore()
    result = DedupeResult()

    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        manifest = media_manifest.load_manifest(folder)
        referenced_files = {entry.get("file") for entry in manifest["files"].values()}
        referenced_sha = {entry.get("sha256") for entry in manifest["files"].values() if entry.get("sha256")}
        if not referenced_sha:
            continue

        candidates = [
            path
            for path in sorted(folder.iterdir())
            if path.is_file() and not path.name.startswith(".") and path.name not in referenced_files
        ]
        if not candidates:
            continue

        folder_trashed = 0
        for path in candidates:
            try:
                digest = _sha256(path)
            except OSError:
                continue
            if digest not in referenced_sha:
                result.unresolved += 1
                result.unresolved_details.append(f"{folder.name}/{path.name}")
                continue

            size = path.stat().st_size
            result.details.append(f"{folder.name}/{path.name} ({size / 1e6:.1f}MB)")
            result.bytes_freed += size
            result.files_trashed += 1
            folder_trashed += 1
            if apply:
                store.forget_gallery(folder.name, path.name)
                edit.to_trash(path)

        if folder_trashed:
            result.folders_touched += 1

    return result
