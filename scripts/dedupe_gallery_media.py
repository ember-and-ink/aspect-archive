"""CLI wrapper around `aspect.media.dedupe` -- see that module for what counts
as a safe duplicate and why. The same logic backs `POST /api/v1/media/dedupe`
(Settings -> Media -> "Clean up duplicates"), which is the one-click way to
run this; this script exists for a dry-run look at the whole archive, or for
pointing at a folder outside it (e.g. a downloaded export to sanity-check
before importing).

    uv run python scripts/dedupe_gallery_media.py              # dry run
    uv run python scripts/dedupe_gallery_media.py --apply      # trash them
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aspect.config import settings  # noqa: E402
from aspect.media import dedupe as media_dedupe  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--galleries-dir", type=Path, default=settings.galleries_dir)
    parser.add_argument("--apply", action="store_true", help="trash matches; without it nothing is written")
    parser.add_argument(
        "--verbose", action="store_true", help="also list unreferenced files that aren't a dupe of a live file"
    )
    args = parser.parse_args()

    root: Path = args.galleries_dir
    if not root.is_dir():
        parser.error(f"galleries dir does not exist: {root}")

    result = media_dedupe.dedupe_galleries(root, apply=args.apply)

    verb = "TRASH" if args.apply else "would trash"
    for line in result.details:
        print(f"{verb} {line}")

    verb = "trashed" if args.apply else "would trash"
    print(
        f"\n{verb} {result.files_trashed} duplicate file(s) across "
        f"{result.folders_touched} folder(s), freeing {result.bytes_freed / 1e6:.0f}MB"
    )
    if result.unresolved:
        print(f"{result.unresolved} unreferenced file(s) were not a dupe of any live file and were left alone")
        if args.verbose:
            for line in result.unresolved_details:
                print(f"  ? {line}")
    if not args.apply:
        print("\ndry run -- nothing was moved. re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
