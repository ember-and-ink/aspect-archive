"""One-off: remove the audio and video the archive used to download.

The media pipeline accepted audio and video from the day it was ported, because
the userscript it was ported from did. Nothing downstream can use either: the
archive's output is V3 PNG cards and gallery folders for SillyTavern, which has
no surface that plays a gallery mp3, and whose expression feature wants static
png/webp sprites that a webm cannot drive. The corpus at the time of writing
held 87 audio files (570MB -- one character alone carrying 143MB of mp3, two
others a 46MB and a 43MB uncompressed wav) and 16 video files (28MB), for zero
usable assets.

Acquisition is now blocked at the source (`writer.UNSUPPORTED_EXT_RE`, plus the
sniffed-type check in `finish_item` and the discovery filter in
`30-media-localization-feature.js`), so this script only has to clear what
landed before that. It is deliberately a one-off migration, not a maintenance
pass -- once the corpus is clean it can be deleted, since nothing can put audio
or video back.

Three things per file, in the order that keeps state consistent if interrupted:

  1. drop its entry from the gallery's `.media.json` `files` map -- otherwise
     the manifest still claims a URL became a local file that is gone, and
     `/media/status` counts it toward a character's downloaded total
  2. delete any cached thumbnail (Pillow could never render these, so there
     should be none -- globbed defensively rather than assumed)
  3. delete the file

Dry run by default; pass --apply to actually delete.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aspect.archive import thumbs  # noqa: E402
from aspect.config import settings  # noqa: E402
from aspect.media import manifest as media_manifest, writer as media_writer  # noqa: E402

AUDIO_EXT = {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".m4b", ".flac", ".aac", ".mid", ".midi"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".flv"}


def kind_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    parser.add_argument("--galleries", type=Path, default=settings.galleries_dir)
    parser.add_argument("--verbose", action="store_true", help="list every file, not just per-folder totals")
    args = parser.parse_args()

    root: Path = args.galleries
    if not root.is_dir():
        print(f"no such galleries dir: {root}", file=sys.stderr)
        return 2

    # Same defaults the server uses (`api/v1/_shared.py`), so this prunes the
    # cache the app actually reads.
    store = thumbs.ThumbnailStore()

    # Sanity check that the policy this script enforces is the one the writer
    # enforces -- if they ever drift, deleting the wrong things is silent.
    for ext in AUDIO_EXT | VIDEO_EXT:
        assert media_writer.unsupported_url_reason(f"https://x/a{ext}"), f"writer would still accept {ext}"

    victims: dict[Path, list[Path]] = defaultdict(list)
    for path in sorted(root.rglob("*")):
        if path.is_file() and kind_of(path):
            victims[path.parent].append(path)

    if not victims:
        print("nothing to do: no audio or video in the archive")
        return 0

    counts: dict[str, int] = defaultdict(int)
    freed = 0
    thumbs_removed = 0
    manifest_entries_dropped = 0

    for folder in sorted(victims):
        files = victims[folder]
        manifest = media_manifest.load_manifest(folder)
        names = {p.name for p in files}
        stale_urls = [url for url, entry in manifest["files"].items() if entry.get("file") in names]

        folder_bytes = 0
        for path in files:
            kind = kind_of(path) or "?"
            size = path.stat().st_size
            counts[kind] += 1
            folder_bytes += size
            freed += size
            if args.verbose:
                print(f"  {'DEL' if args.apply else 'would delete'} [{kind}] {size / 1e6:7.1f}MB {path.name}")

            for thumb in store.gallery_dir.joinpath(folder.name).glob(f"{path.name}_*.jpg"):
                thumbs_removed += 1
                if args.apply:
                    thumb.unlink(missing_ok=True)

            if args.apply:
                path.unlink(missing_ok=True)

        manifest_entries_dropped += len(stale_urls)
        if stale_urls and args.apply:
            for url in stale_urls:
                del manifest["files"][url]
            media_manifest.save_manifest(folder, manifest)

        print(
            f"{folder.name}: {len(files)} file(s), {folder_bytes / 1e6:.1f}MB"
            f"{f', {len(stale_urls)} manifest entr(y/ies)' if stale_urls else ''}"
        )

    verb = "removed" if args.apply else "would remove"
    print(
        f"\n{verb} {counts['audio']} audio + {counts['video']} video "
        f"across {len(victims)} folder(s), freeing {freed / 1e6:.0f}MB"
    )
    print(f"{verb} {thumbs_removed} cached thumbnail(s), {manifest_entries_dropped} manifest entr(y/ies)")
    if not args.apply:
        print("\ndry run -- nothing was deleted. re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
