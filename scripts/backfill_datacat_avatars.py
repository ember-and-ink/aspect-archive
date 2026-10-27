"""One-off repair: backfill the original-avatar markdown link into
creator_notes for datacat imports that predate `--fetch-datacat-images` (see
scripts/import_cards.py). A card qualifies when:

  1. extensions.jai.sourceKind == "datacat_import" -- this is DEFINITELY a
     datacat import. Anything else (janitor_core, saucepan_core,
     jannyai_import, or no `jai` extension at all -- e.g. a Chub import) is
     left completely untouched.
  2. its creator_notes do NOT already start with an `![name](url)` link to
     the original source CDN (ella.janitorai.com, or cdn.saucepan.ai for a
     saucepan-sourced character) -- i.e. it was never stamped.

For each qualifying card, extensions.jai.id (the JanitorAI character id
stamped at import time -- the same value `--fetch-datacat-images` would have
passed to the resolver) is looked up through datacat.run's own API (see
aspect/datacat_client.py) and, if found, prepended to creator_notes exactly the
way CardBuilder.build does for a fresh import -- `![{name}]({avatar_url})\n\n`
-- then re-embedded in place. Re-embedding goes through pngtools.embed_card,
the same mechanism scripts/check_cards.py --repair uses, so only the tEXt
chunks change -- pixels are verified untouched before every write.

A card whose avatar datacat can no longer resolve (gone from its index, no
original-CDN link on record) is reported and left alone -- nothing to
backfill.

Read-only report by default; pass --repair to actually rewrite cards. This
is a one-time cleanup script (existing imports only -- new imports use
`import_cards.py --fetch-datacat-images` directly) and is not expected to be
run again once the archive is clean.

    uv run python scripts/backfill_datacat_avatars.py
    uv run python scripts/backfill_datacat_avatars.py --repair
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from aspect.cards import pngtools
from aspect.config import settings
from aspect.sources.datacat_client import DatacatImageResolver

# A card is "already stamped" when creator_notes opens with a markdown image
# link to one of the untouched source CDNs -- the exact shape CardBuilder
# produces (see aspect/cards/builder.py's `if avatar_url:` block). Datacat's own
# re-hosted media.datacat.run links don't count -- those are pre-existing
# in-body images, not the stamp this script is checking for.
_STAMPED_RE = re.compile(r"^!\[[^\]]*\]\(https://(?:ella\.janitorai\.com|cdn\.saucepan\.ai)/")


def _is_datacat_import(data: dict) -> bool:
    jai = (data.get("extensions") or {}).get("jai")
    return isinstance(jai, dict) and jai.get("sourceKind") == "datacat_import"


def _already_stamped(notes: str) -> bool:
    return bool(_STAMPED_RE.match(notes.lstrip()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cards-dir", type=Path, default=settings.archive_dir)
    parser.add_argument("--repair", action="store_true", help="rewrite fixable cards in place (pixels preserved)")
    parser.add_argument("--limit", type=int, default=50, help="max cards to list per section (0 = all)")
    args = parser.parse_args()

    if not args.cards_dir.is_dir():
        print(f"cards dir not found: {args.cards_dir}")
        return 1

    cards = sorted(args.cards_dir.glob("**/*.png"))
    resolver = DatacatImageResolver()

    not_datacat = already_ok = unreadable = 0
    no_id: list[str] = []
    unresolved: list[str] = []
    fixed: list[tuple[str, str]] = []

    try:
        for path in cards:
            raw = path.read_bytes()
            parsed = pngtools.read_envelope(raw)
            if parsed is None:
                unreadable += 1
                continue
            envelope, data = parsed

            if not _is_datacat_import(data):
                not_datacat += 1
                continue

            notes = data.get("creator_notes")
            notes = notes if isinstance(notes, str) else ""
            if _already_stamped(notes):
                already_ok += 1
                continue

            rel = str(path.relative_to(args.cards_dir))
            cid = ((data.get("extensions") or {}).get("jai") or {}).get("id")
            if not cid:
                print(f"  skip  {rel}: no extensions.jai.id to resolve")
                no_id.append(rel)
                continue

            avatar_url = resolver.resolve(cid)
            if not avatar_url:
                print(f"  skip  {rel}: datacat has no original avatar on record (id {cid})")
                unresolved.append(rel)
                continue

            name = data.get("name") or ""
            new_notes = f"![{name}]({avatar_url})\n\n{notes}" if notes else f"![{name}]({avatar_url})"

            if args.repair:
                data["creator_notes"] = new_notes
                before = pngtools.non_text_chunks(raw)
                new_bytes = pngtools.embed_card(raw, envelope, data)
                after = pngtools.non_text_chunks(new_bytes)
                if before != after:  # must never happen: repair touches only tEXt
                    print(f"  ABORT {rel}: pixel chunks changed, not writing")
                    continue
                path.write_bytes(new_bytes)
                print(f"  fix   {rel}  ->  {avatar_url}")
            else:
                print(f"  would-fix  {rel}  ->  {avatar_url}")
            fixed.append((rel, avatar_url))
    finally:
        resolver.close()

    _report(cards, unreadable, not_datacat, already_ok, no_id, unresolved, fixed, args.limit)
    if args.repair:
        print(f"\n>>> REPAIRED {len(fixed)} card(s) in place (pixels preserved).")
    else:
        print(f"\n>>> DRY RUN -- {len(fixed)} card(s) would be repaired. Pass --repair to write.")
    return 0


def _report(cards, unreadable, not_datacat, already_ok, no_id, unresolved, fixed, limit):
    def cap(items):
        return items if limit == 0 else items[:limit]

    print(f"\n=== SCANNED {len(cards)} cards ({unreadable} unreadable) ===")
    print(f"    not a datacat_import: {not_datacat}")
    print(f"    already stamped:      {already_ok}")

    print(f"\n### NO extensions.jai.id (can't resolve): {len(no_id)} card(s)")
    for p in cap(no_id):
        print(f"  {p}")
    if limit and len(no_id) > limit:
        print(f"  ... and {len(no_id) - limit} more")

    print(f"\n### UNRESOLVED (datacat has no original avatar on record): {len(unresolved)} card(s)")
    for p in cap(unresolved):
        print(f"  {p}")
    if limit and len(unresolved) > limit:
        print(f"  ... and {len(unresolved) - limit} more")

    print(f"\n### FIXABLE: {len(fixed)} card(s)")
    for p, url in cap(fixed):
        print(f"  {p}  ->  {url}")
    if limit and len(fixed) > limit:
        print(f"  ... and {len(fixed) - limit} more")


if __name__ == "__main__":
    raise SystemExit(main())
