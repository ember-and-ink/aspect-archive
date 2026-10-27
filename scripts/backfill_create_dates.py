"""Give every card in the archive a root-level `create_date`.

SillyTavern stamps `create_date` on every card it writes and sorts on it; so
does CharacterLibrary's "Date Created". Our builders never did -- see
aspect/cards/dates.py -- so every card this tool produced sorted as 0 there and
showed "(not available)" in the Info panel's Dates section.

The read path no longer needs this pass: the archive index derives the value at
parse time, so the app is already fixed. What the pass is for is the *cards
themselves*, which travel: a card exported to SillyTavern carries only what is
embedded in its PNG, and an unstamped card is undated the moment it leaves here.

    uv run python scripts/backfill_create_dates.py            # report only
    uv run python scripts/backfill_create_dates.py --apply    # write the dates

Surgical, like backfill_gallery_ids.py: the value is re-embedded into the *same*
PNG byte stream (only the tEXt chunks are rewritten), so avatar pixels and the
pngquant compression survive exactly -- verified per card by comparing the
non-text chunks before and after, which aborts the write if they ever differ.
A card that already carries a `create_date` is never touched: a recorded date
outranks a derived one, and a card that came from SillyTavern brought a real one.
Safe to re-run -- a second pass finds nothing to do.

One-time by intent. Every write funnel stamps the field now, so nothing new can
arrive without one; this exists to catch up the cards written before it did.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aspect.cards import dates, pngtools
from aspect.config import settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cards-dir", type=Path, default=settings.archive_dir)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the dates in place (default is a read-only report)",
    )
    parser.add_argument("--limit", type=int, default=50, help="max cards to list (0 = None)")
    args = parser.parse_args()

    if not args.cards_dir.is_dir():
        print(f"cards dir not found: {args.cards_dir}")
        return 1

    cards = sorted(args.cards_dir.glob("**/*.png"))
    stamped: list[tuple[str, str]] = []
    present = unreadable = aborted = 0
    # A card with no provenance at all -- no `linkedAt` in any extension block.
    # Left alone rather than given today's date: an undated card is a fact, and
    # inventing a creation date would make it permanently unrecoverable.
    undatable: list[str] = []

    for path in cards:
        raw = path.read_bytes()
        parsed = pngtools.read_envelope(raw)
        if parsed is None:
            print(f"  skip  {path.relative_to(args.cards_dir)}: no readable card")
            unreadable += 1
            continue
        envelope, data = parsed
        if not isinstance(data, dict):
            unreadable += 1
            continue

        existing = envelope.get(dates.CREATE_DATE_KEY)
        if isinstance(existing, str) and existing.strip():
            present += 1
            continue

        rel = str(path.relative_to(args.cards_dir))
        derived = dates.earliest_linked_at(data)
        if not derived:
            undatable.append(rel)
            continue

        if not args.apply:
            stamped.append((rel, derived))
            continue

        # `embed_card` does the stamping itself, from the same derivation -- the
        # value is computed here only so the report can show it.
        new_bytes = pngtools.embed_card(raw, envelope, data)
        if pngtools.non_text_chunks(raw) != pngtools.non_text_chunks(new_bytes):
            print(f"  ABORT {rel}: pixel chunks changed, not writing")
            aborted += 1
            continue
        path.write_bytes(new_bytes)
        stamped.append((rel, derived))

    verb = "stamped" if args.apply else "missing create_date"
    print(f"=== SCANNED {len(cards)} cards ===\n")
    print(f"### {verb.upper()}: {len(stamped)} card(s)")
    listed = stamped if args.limit == 0 else stamped[: args.limit]
    for rel, value in listed:
        print(f"  {rel}  ->  {value}")
    if args.limit and len(stamped) > args.limit:
        print(f"  ... and {len(stamped) - args.limit} more")

    if undatable:
        print(f"\n### NO PROVENANCE TO DERIVE FROM: {len(undatable)} card(s), left undated")
        for rel in undatable[: args.limit or len(undatable)]:
            print(f"  {rel}")

    print(f"\nalready had one: {present}, unreadable: {unreadable}, aborted: {aborted}")
    if stamped and not args.apply:
        print("\n>>> read-only report. Re-run with --apply to write these dates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
