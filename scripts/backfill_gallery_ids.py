"""Give every card in the archive a `gallery_id`.

`extensions.gallery_id` is the handle SillyTavern-CharacterLibrary keys a
character's image gallery on (see aspect/cards/gallery.py). Every card written from now
on gets one at write time -- PngWriter stamps it -- but cards built before that
existed, and imports whose source carried no id, don't have one. This is the
catch-up pass: scan the cards folder, mint an id for each card missing one, and patch it
into the PNG in place.

    uv run python scripts/backfill_gallery_ids.py            # report only
    uv run python scripts/backfill_gallery_ids.py --apply    # write the ids
    make gallery-ids ARGS=--apply

Surgical, like check_cards.py --repair: the new id is re-embedded into the *same*
PNG byte stream (only the tEXt chunks are rewritten), so avatar pixels and the
pngquant compression are preserved exactly -- verified per card by comparing the
non-text chunks before and after, which aborts the write if they ever differ.
Cards that already carry an id are never touched: that id is the link to a
gallery folder that may already hold images. Safe to re-run -- a second pass
finds nothing to do.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aspect.cards import gallery, pngtools
from aspect.config import settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cards-dir", type=Path, default=settings.archive_dir)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the ids in place (default is a read-only report)",
    )
    parser.add_argument("--limit", type=int, default=50, help="max cards to list (0 = all)")
    args = parser.parse_args()

    if not args.cards_dir.is_dir():
        print(f"cards dir not found: {args.cards_dir}")
        return 1

    cards = sorted(args.cards_dir.glob("**/*.png"))
    stamped: list[tuple[str, str]] = []  # (path, id or "-" in report mode)
    present = unreadable = aborted = 0

    for path in cards:
        raw = path.read_bytes()
        parsed = pngtools.read_envelope(raw)
        if parsed is None:
            print(f"  skip  {path.relative_to(args.cards_dir)}: no readable card")
            unreadable += 1
            continue
        envelope, data = parsed

        if gallery.read_id(data) is not None:
            present += 1
            continue

        rel = str(path.relative_to(args.cards_dir))
        if not args.apply:
            stamped.append((rel, "-"))
            continue

        gid = gallery.ensure_id(data)
        new_bytes = pngtools.embed_card(raw, envelope, data)
        if pngtools.non_text_chunks(raw) != pngtools.non_text_chunks(new_bytes):
            print(f"  ABORT {rel}: pixel chunks changed, not writing")
            aborted += 1
            continue
        path.write_bytes(new_bytes)
        stamped.append((rel, gid))

    verb = "stamped" if args.apply else "missing gallery_id"
    print(f"=== SCANNED {len(cards)} cards ===\n")
    print(f"### {verb.upper()}: {len(stamped)} card(s)")
    listed = stamped if args.limit == 0 else stamped[: args.limit]
    for rel, gid in listed:
        print(f"  {rel}" + (f"  ->  {gid}" if args.apply else ""))
    if args.limit and len(stamped) > args.limit:
        print(f"  ... and {len(stamped) - args.limit} more")

    print(f"\nalready had one: {present}, unreadable: {unreadable}, aborted: {aborted}")
    if stamped and not args.apply:
        print("\n>>> read-only report. Re-run with --apply to write these ids.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
