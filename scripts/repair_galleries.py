"""Clean up orphaned folders in SillyTavern's user/images.

CharacterLibrary computes a gallery folder name live as
`sanitize(character.name) + "_" + gallery_id` (index.js:1098), so renaming a
card orphans its gallery: the folder is still on disk but nothing reaches it.
The name-repair pass renamed a lot of cards, and galleries were then partly
re-downloaded under the new names -- leaving both copies behind.

Every orphan is classified by the gallery_id in its folder name:

  stale  -- that gallery_id still belongs to a card on disk. The folder holds
            real images. Files not already present in the card's current folder
            are moved there; the folder is then removed.
  dead   -- no card carries that gallery_id (card deleted). Removed.
  bare   -- no gallery_id suffix at all: a gallery from before
            uniqueGalleryFolders was turned on. Removed only when empty.

Duplicate detection is by filename identity, not content hash: the webp
compression pass rewrote every file's bytes, so identical images no longer
hash alike. See `identity()`.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import unicodedata as ud
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path.home() / "workspaces" / "aspect-archive"))
from aspect.cards.pngtools import read_envelope  # noqa: E402

ST = Path.home() / "workspaces" / "SillyTavern" / "data" / "default-user"
CHARS = ST / "characters"
IMAGES = ST / "user" / "images"
# Retired folders land here rather than being deleted, so a bad call is a
# rename away from being undone. Same filesystem, so it costs nothing.
QUARANTINE = IMAGES / "_orphan_quarantine"

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
_FOLDER = re.compile(r"^(.*)_([A-Za-z0-9]{12})$")
_PREFIX = re.compile(
    r"^(?:localized_media_\d+|extgallery_\d+|(?:chub|datacat)gallery_[0-9a-f]+)_"
)
_IGNORE = {".DS_Store"}


def sanitize(name: str) -> str:
    """CharacterLibrary's sanitizeFolderName, verbatim."""
    return _UNSAFE.sub("_", name or "").strip()


def fskey(name: str) -> str:
    """A folder name as the *filesystem* compares it.

    APFS resolves paths case- and normalization-insensitively while Python's
    `==` does not, so a card named "Amélie" (NFC) and its folder stored NFD
    look unequal here but are the same directory to open(). Comparing raw
    strings therefore reports a live gallery as an orphan, and -- because the
    computed target then resolves back to that very folder -- every file
    matches itself as a duplicate and the whole gallery gets retired. Same trap
    for "kate" vs "Kate"."""
    return ud.normalize("NFC", name).casefold()


def identity(p: Path) -> str:
    """What makes two gallery files the same image.

    Filenames are `<source>gallery_<hash>_<id>` or `localized_media_<ms>_<id>`;
    every prefix is minted per download, so only the remainder survives a
    re-fetch. Splitting on the last underscore instead would grab a compressor
    suffix (`_e18`, `_840`) and collide across unrelated images.

    The extension stays part of the identity, so a `.gif` is never discarded in
    favour of a `.webp` that shares its id -- the compression pass flattened
    animations, and this only costs a handful of duplicate stills."""
    return ud.normalize("NFC", _PREFIX.sub("", p.stem, count=1) + p.suffix.lower())


def contents(d: Path) -> list[Path]:
    return sorted(f for f in d.rglob("*") if f.is_file() and f.name not in _IGNORE)


def cards_by_gallery_id() -> dict[str, str]:
    """gallery_id -> the folder name CharacterLibrary computes for that card."""
    out: dict[str, str] = {}
    for p in sorted(CHARS.glob("*.png")):
        env = read_envelope(p.read_bytes())
        if not env:
            continue
        envelope, data = env
        ext = data.get("extensions")
        gid = (ext or {}).get("gallery_id") if isinstance(ext, dict) else None
        name = sanitize(envelope.get("name") or data.get("name") or "")
        if gid and name:
            out[gid] = f"{name}_{gid}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument(
        "--purge",
        action="store_true",
        help="delete emptied folders outright instead of moving them to "
        f"{QUARANTINE.name}/ for review",
    )
    args = ap.parse_args()

    def discard(d: Path) -> None:
        """Retire a folder we are done with -- to quarantine, or for real."""
        if not args.apply:
            return
        if args.purge:
            shutil.rmtree(d)
            return
        QUARANTINE.mkdir(exist_ok=True)
        dst, n = QUARANTINE / d.name, 1
        while dst.exists():
            dst, n = QUARANTINE / f"{d.name}~{n}", n + 1
        d.rename(dst)

    by_gid = cards_by_gallery_id()
    live = {fskey(v) for v in by_gid.values()}

    stale: dict[str, list[Path]] = defaultdict(list)
    dead: list[Path] = []
    bare_kept: list[Path] = []

    for d in sorted(IMAGES.iterdir()):
        if not d.is_dir() or fskey(d.name) in live or d == QUARANTINE:
            continue
        m = _FOLDER.match(d.name)
        target = by_gid.get(m.group(2)) if m else None
        if target and fskey(target) == fskey(d.name):
            # Only the string form differs (NFC/NFD, case) -- same directory.
            continue
        if target:
            stale[target].append(d)
        elif m or not contents(d):
            dead.append(d)
        else:
            bare_kept.append(d)

    moved = skipped = removed = 0
    log: list[dict] = []

    for target_name, sources in sorted(stale.items()):
        target = IMAGES / target_name
        seen = {identity(f) for f in contents(target)} if target.exists() else set()
        for src in sorted(sources, key=lambda p: p.name):
            if target.exists() and src.samefile(target):
                log.append({"op": "skip-samefile", "src": src.name})
                continue
            files = contents(src)
            # A folder whose target does not exist yet is just misnamed: rename
            # it rather than moving every file individually.
            if files and not target.exists():
                log.append({"op": "rename", "src": src.name, "dst": target_name,
                            "files": len(files)})
                if args.apply:
                    src.rename(target)
                seen = {identity(f) for f in files}
                moved += len(files)
                removed += 1
                continue
            for f in files:
                fid = identity(f)
                if fid in seen:
                    skipped += 1
                    continue
                dst = target / f.relative_to(src)
                stem, suffix, n = dst.stem, dst.suffix, 1
                while dst.exists():
                    dst = dst.with_name(f"{stem}~{n}{suffix}")
                    n += 1
                log.append({"op": "move", "src": f"{src.name}/{f.relative_to(src)}",
                            "dst": f"{target_name}/{dst.relative_to(target)}"})
                if args.apply:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(dst))
                seen.add(fid)
                moved += 1
            log.append({"op": "retire-stale", "src": src.name, "files": len(files)})
            discard(src)
            removed += 1

    for d in dead:
        n = len(contents(d))
        log.append({"op": "retire-dead", "src": d.name, "files": n})
        discard(d)
        removed += 1

    if args.apply and not args.purge:
        print(f"  retired folders moved to {QUARANTINE}")
    print(f"[{'APPLIED' if args.apply else 'DRY RUN'}] "
          f"folders removed={removed}  files kept (moved/renamed)={moved}  "
          f"duplicates discarded={skipped}")
    if bare_kept:
        print(f"  left alone -- no gallery_id in the name but not empty: "
              f"{[p.name for p in bare_kept]}")
    Path(__file__).with_name(
        f"log_{'applied' if args.apply else 'dryrun'}.json"
    ).write_text(json.dumps(log, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
