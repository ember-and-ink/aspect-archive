"""Convert existing gallery stills to WebP -- the one-time corpus sweep half
of docs/PHASE_3C_PLAN.md §4 (the other half, WebP-at-intake, is already live
in `media_writer.finish_item`).

New downloads land as WebP already; this is for the ~1,995 jpg/jpeg/png files
that predate that and are still sitting on disk at whatever format their CDN
happened to serve. For each one:

  1. Re-encode via `media_writer.normalize_to_webp` -- the exact function the
     download route uses, so a file converted here is byte-for-byte what it
     would have been if downloaded today.
  2. Write the `.webp` sibling, retire the original to `data/.trash/`
     (`edit.to_trash`, same bin every other delete in this app uses).
  3. Forget the stale gallery thumb -- it was rendered from the old bytes and
     is now keyed to a filename nothing points at.
  4. If the gallery's `.media.json` names the old file for some source URL,
     repoint that entry at the new one rather than leaving it dangling.

Animated gif/webp, svg, audio and video pass through `normalize_to_webp`
unchanged and are simply not candidates -- see the plan's §6 note on why
animated conversion is a separate, separately-verified pass.

    uv run python scripts/normalize_gallery_media.py              # dry run
    uv run python scripts/normalize_gallery_media.py --apply       # convert
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from aspect.cards import edit
from aspect.archive import thumbs
from aspect.media import manifest as media_manifest, writer as media_writer
from aspect.config import settings

_CANDIDATE_EXTS = frozenset((".jpg", ".jpeg", ".png"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--galleries-dir", type=Path, default=settings.galleries_dir)
    parser.add_argument("--thumbs-dir", type=Path, default=settings.thumbs_dir)
    parser.add_argument("--apply", action="store_true", help="convert; without it nothing is written")
    parser.add_argument("--limit", type=int, default=20, help="max conversions to list (0 = all)")
    args = parser.parse_args()

    if not args.galleries_dir.is_dir():
        parser.error(f"galleries dir does not exist: {args.galleries_dir}")

    store = thumbs.ThumbnailStore(args.thumbs_dir, settings.archive_dir)

    listed = 0
    converted = manifest_updated = failed = skipped_not_still = 0
    for folder in sorted(p for p in args.galleries_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        manifest = media_manifest.load_manifest(folder)
        file_to_url = {entry.get("file"): url for url, entry in manifest["files"].items()}
        touched = False

        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() not in _CANDIDATE_EXTS:
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                print(f"  FAILED reading {path}: {exc}")
                failed += 1
                continue
            sniff = media_writer.sniff_media(data, None)
            if not sniff.valid or not sniff.media_type:
                continue
            new_data, new_type = media_writer.normalize_to_webp(data, sniff.media_type)
            if new_type == sniff.media_type:
                # Not still-image content normalize_to_webp will touch, or the
                # re-encode itself failed and it logged why.
                skipped_not_still += 1
                continue

            new_path = path.with_suffix(".webp")
            if listed < args.limit or args.limit == 0:
                print(f"{path.relative_to(args.galleries_dir)} -> {new_path.name}")
            listed += 1

            if not args.apply:
                continue
            if new_path.exists():
                print(f"  SKIP: {new_path.name} already exists in {folder.name}")
                failed += 1
                continue

            try:
                edit.write_atomic(new_path, new_data)
                edit.to_trash(path)
            except edit.WriteError as exc:
                print(f"  FAILED converting {path}: {exc}")
                failed += 1
                continue

            store.forget_gallery(folder.name, path.name)
            converted += 1

            url = file_to_url.get(path.name)
            if url:
                digest = hashlib.sha256(new_data).hexdigest()
                media_manifest.record_saved(manifest, url, new_path.name, digest, size=len(new_data))
                touched = True
                manifest_updated += 1

        if touched:
            media_manifest.save_manifest(folder, manifest)

    print(f"\n{listed} candidate(s) found" + ("" if args.limit == 0 else f" ({min(listed, args.limit)} shown)"))
    if not args.apply:
        print("read-only; pass --apply to write")
        return 0

    print(f"converted {converted}, manifest entries repointed {manifest_updated}" + (f", failed {failed}" if failed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
