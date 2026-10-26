"""Bring the avatar thumbnail cache in step with the catalog.

The cache came across at cutover from SillyTavern's `thumbnails/avatar` and
already covers 99.6% of the archive, so this is a tidy-up pass, not a build:

  missing   -- an indexed card with no thumb. Rendered here so the first browse
               of a fresh archive is not also the thing that generates them.
               (The API generates on miss too, so this is an optimization of
               first-paint, never a prerequisite.)
  stale     -- a thumb whose card is gone: renamed by `make names`, or deleted.
               840 of these at cutover, the residue of the archive's whole life.
  miscased  -- a thumb whose name differs from its card's only by case. macOS
               resolves it anyway, so it looks fine here and would silently miss
               in a Linux container -- costing a needless regeneration. Renamed
               to match the card while we can still see the pair.

    uv run python scripts/sync_thumbs.py              # read-only report
    uv run python scripts/sync_thumbs.py --apply      # generate, prune, rename

Stale thumbs are retired to `<thumbs>/_stale/` rather than deleted, so a wrong
call is a `mv` away from being undone. Same filesystem, so it is free. The whole
cache is disposable regardless -- deleting it costs one regeneration pass and no
data -- which is why this script is allowed to be blunt.

`--galleries` does the same job for the gallery thumb cache instead of the
avatar one -- docs/PHASE_3C_PLAN.md §5:

    uv run python scripts/sync_thumbs.py --galleries              # report
    uv run python scripts/sync_thumbs.py --galleries --apply      # act

  missing  -- a thumbable image (see `thumbs.THUMBABLE_EXTS`) in a live gallery
              folder with no `_<size>.webp` thumb yet.
  orphaned -- a thumb whose source image left the gallery by some route other
              than `DELETE .../files/{filename}` (which already forgets its own
              thumb on the way out) -- `ThumbnailStore.prune_gallery`.
  dropped  -- a whole thumb-cache folder whose gallery folder no longer exists
              at all, e.g. after a manual `rm -r` of a gallery.
  superseded -- pre-WebP `.jpg` thumbs, fitted inside a 384 box instead of
              cover-cropped to fill the tile. Nothing requests them any more and
              nothing else collects them, so they need `--drop-superseded`.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from aspect.archive import catalog, thumbs
from aspect.config import settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--archive-dir", type=Path, default=settings.archive_dir)
    parser.add_argument("--thumbs-dir", type=Path, default=settings.thumbs_dir)
    parser.add_argument("--galleries-dir", type=Path, default=settings.galleries_dir)
    parser.add_argument(
        "--galleries",
        action="store_true",
        help="sync the gallery thumb cache instead of the avatar one",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually generate, rename and retire; without it nothing is written",
    )
    parser.add_argument(
        "--drop-superseded",
        action="store_true",
        help="with --galleries: delete pre-WebP `.jpg` thumbs, which no longer match "
        "either the format or the geometry the cache is now keyed on",
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="with --apply: prune and delete, but leave missing thumbs to be rendered "
        "on first view rather than pre-rendering the lot here",
    )
    parser.add_argument(
        "--delete-stale",
        action="store_true",
        help="delete stale thumbs outright instead of retiring them to _stale/",
    )
    parser.add_argument("--limit", type=int, default=20, help="max names to list per section (0 = all)")
    args = parser.parse_args()

    if args.galleries:
        return _sync_galleries(args)

    if not args.archive_dir.is_dir():
        parser.error(f"archive dir does not exist: {args.archive_dir}")

    index = catalog.ArchiveIndex(args.archive_dir)
    index.refresh()
    store = thumbs.ThumbnailStore(args.thumbs_dir, args.archive_dir)
    filenames = [r.filename for r in index.all()]

    missing = store.missing(filenames)
    stale = store.stale(filenames)

    # Split the miscased out of `stale` before pruning: by exact name they look
    # orphaned, but they are a usable thumb for a card that is still here.
    by_fold = {name.casefold(): name for name in filenames}
    miscased: list[tuple[Path, str]] = []
    for path in list(stale):
        card = by_fold.get(path.name.casefold())
        if card is not None:
            miscased.append((path, card))
            stale.remove(path)
    # A miscased thumb is a hit, not a miss -- don't render one we already have.
    renamed_targets = {card for _, card in miscased}
    missing = [name for name in missing if name not in renamed_targets]

    print(f"archive: {len(filenames)} cards in {args.archive_dir}")
    print(f"thumbs:  {args.thumbs_dir / 'avatar'}")
    _report("missing (to generate)", missing, args.limit)
    _report("miscased (to rename)", [f"{p.name} -> {card}" for p, card in miscased], args.limit)
    _report("stale (to retire)", [p.name for p in stale], args.limit)

    if not args.apply:
        print("\nread-only; pass --apply to write")
        return 0

    for path, card in miscased:
        # Two-step through a temporary name: on a case-insensitive filesystem a
        # direct rename between names differing only in case is a no-op or an
        # error depending on the platform.
        interim = path.with_name(path.name + ".casefix")
        path.replace(interim)
        interim.replace(path.with_name(card))
    print(f"renamed  {len(miscased)}")

    if args.no_generate:
        print(f"skipped  {len(missing)} missing thumb(s), left for first view to render")
    else:
        generated = failed = 0
        for name in missing:
            if store.generate_avatar(name) is None:
                failed += 1
            else:
                generated += 1
        print(f"generated {generated}" + (f", failed {failed}" if failed else ""))

    if args.delete_stale:
        for path in stale:
            path.unlink(missing_ok=True)
        print(f"deleted  {len(stale)} stale")
    else:
        retired = args.thumbs_dir / "_stale"
        retired.mkdir(parents=True, exist_ok=True)
        for path in stale:
            shutil.move(str(path), str(retired / path.name))
        print(f"retired  {len(stale)} stale -> {retired}")
    return 0


def _sync_galleries(args: argparse.Namespace) -> int:
    if not args.galleries_dir.is_dir():
        print(f"no galleries dir at {args.galleries_dir}, nothing to do")
        return 0

    store = thumbs.ThumbnailStore(args.thumbs_dir, args.archive_dir)
    live_folders = {e.name for e in os.scandir(args.galleries_dir) if e.is_dir() and not e.name.startswith(".")}
    try:
        cached_folders = {e.name for e in os.scandir(store.gallery_dir) if e.is_dir()}
    except OSError:
        cached_folders = set()
    dropped_folders = sorted(cached_folders - live_folders)

    missing: list[str] = []
    orphaned = 0
    per_folder_files: dict[str, set[str]] = {}
    for folder in sorted(live_folders):
        directory = args.galleries_dir / folder
        try:
            files = {e.name for e in os.scandir(directory) if e.is_file()}
        except OSError:
            continue
        per_folder_files[folder] = files
        for name in sorted(files):
            if Path(name).suffix.lower() not in thumbs.THUMBABLE_EXTS:
                continue
            if not store.gallery_path(folder, name).is_file():
                missing.append(f"{folder}/{name}")

    superseded, superseded_bytes = _superseded_thumbs(store.gallery_dir)

    print(f"galleries: {len(live_folders)} folders in {args.galleries_dir}")
    print(f"thumbs:    {store.gallery_dir}")
    _report("missing (to generate)", missing, args.limit)
    _report("dropped (cache folder, no live gallery)", dropped_folders, args.limit)
    if superseded:
        verb = "to delete" if args.drop_superseded else "pass --drop-superseded to delete"
        print(
            f"\nsuperseded (pre-WebP .jpg): {len(superseded)} file(s), "
            f"{superseded_bytes / 1048576:.0f} MB -- {verb}"
        )

    if not args.apply:
        print("\norphaned (to prune): computed per folder during --apply")
        print("\nread-only; pass --apply to write")
        return 0

    if args.no_generate:
        print(f"skipped  {len(missing)} missing thumb(s), left for first view to render")
    else:
        generated = failed = 0
        for entry in missing:
            folder, _, name = entry.partition("/")
            source = args.galleries_dir / folder / name
            if store.generate_gallery(source, folder, name) is None:
                failed += 1
            else:
                generated += 1
        print(f"generated {generated}" + (f", failed {failed}" if failed else ""))

    for folder, files in per_folder_files.items():
        orphaned += store.prune_gallery(folder, files)
    print(f"pruned   {orphaned} orphaned thumb(s)")

    for folder in dropped_folders:
        shutil.rmtree(store.gallery_dir / folder, ignore_errors=True)
    print(f"dropped  {len(dropped_folders)} cache folder(s) with no live gallery")

    if args.drop_superseded:
        dropped = 0
        for path in superseded:
            try:
                path.unlink()
                dropped += 1
            except OSError:
                continue
        print(f"deleted  {dropped} superseded .jpg thumb(s), {superseded_bytes / 1048576:.0f} MB")
    return 0


def _superseded_thumbs(gallery_dir: Path) -> tuple[list[Path], int]:
    """Gallery thumbs left over from the JPEG era, and what they weigh.

    Identified by extension alone, which is exact here: every thumb written
    before the change was `.jpg` and every one written since is `.webp`. They are
    not merely a different encoding either -- they were *fitted* inside a box
    rather than cover-cropped to fill it, so nothing will ever ask for one again
    and `prune_gallery` won't collect them (it only drops thumbs whose source
    image is gone, and these sources are still here).
    """
    found: list[Path] = []
    total = 0
    if not gallery_dir.is_dir():
        return found, total
    for path in gallery_dir.glob("*/*.jpg"):
        if not path.is_file():
            continue
        found.append(path)
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return found, total


def _report(label: str, names: list[str], limit: int) -> None:
    print(f"\n{label}: {len(names)}")
    shown = names if limit == 0 else names[:limit]
    for name in shown:
        print(f"  {name}")
    if len(names) > len(shown):
        print(f"  ... and {len(names) - len(shown)} more")


if __name__ == "__main__":
    raise SystemExit(main())
