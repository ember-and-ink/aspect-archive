"""Re-check (and optionally repair) already-built cards against the CURRENT
macro/formatting rules.

Cards are imported once and kept, but the cleaners keep improving -- new macro
typos get added to aspect.text.macros over time, the creator_notes de-HTML gets
tighter. This asks: given today's rules, does any card in the archive still carry a
macro we'd now fix, an unresolvable macro worth a human eyeball, or a leaked
HTML/CSS artifact in its notes?

It re-runs `MacroSanitizer.sanitize` -- the exact transform CardBuilder and the
mappers apply -- over every prose field of each card's embedded chara_card_v3.
That transform is idempotent (it folds to canonical {{user}}/{{char}}), so a
card that's already clean comes back byte-identical: a `WOULD-CHANGE` line means
the card was built before a rule existed, nothing more. creator_notes formatting
is a READ-ONLY heuristic (leaked tags/CSS/doctype) -- we never re-run html_to_md
on stored notes (they're already markdown; the source HTML is gone).

    uv run python scripts/check_cards.py                 # read-only report
    uv run python scripts/check_cards.py --repair        # rewrite WOULD-CHANGE cards
    uv run python scripts/check_cards.py --json out.json  # machine-readable dump

--repair is surgical: it re-embeds the re-sanitized card into the *same* PNG
byte stream (inject_text_chunks strips + rewrites only the tEXt chunks), so the
avatar pixels / pngquant compression are preserved exactly. It applies only the
idempotent macro sanitize -- never html_to_md, never persona-name reversal --
and only touches cards that actually change.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from aspect.cards import pngtools
from aspect.config import settings
from aspect.text.macros import MacroSanitizer

# The prose fields CardBuilder / sources.chub run through the sanitizer. Superset
# of every source path so one scan covers native, datacat and Chub cards.
_TOP_TEXT_FIELDS = (
    "description",
    "personality",
    "scenario",
    "mes_example",
    "first_mes",
    "system_prompt",
    "post_history_instructions",
)

# creator_notes formatting heuristics -- built notes are markdown, so an HTML
# tag / CSS block / doctype in there is a leak from an older html_to_md.
_TAG_RE = re.compile(
    r"</?(?:div|span|style|script|p|br|b|i|strong|em|h[1-6]|ul|ol|li|a|img|font|"
    r"table|tr|td|center|blockquote)\b",
    re.IGNORECASE,
)
_DOCTYPE_RE = re.compile(r"<!doctype|<!--", re.IGNORECASE)
_CSS_RE = re.compile(
    r"\{[^{}]*(?:color|font|margin|padding|background|border|width|height|display|"
    r"text-align)\s*:",
    re.IGNORECASE,
)


def _string_fields(data: dict):
    """Yield (label, get, set) for each sanitizable prose string in a card, so a
    caller can read it and (for --repair) write the cleaned value straight back
    into the raw dict -- no pydantic round-trip, so Chub's lorebook extras and
    int positions survive."""
    for field in _TOP_TEXT_FIELDS:
        v = data.get(field)
        if isinstance(v, str) and v:
            yield field, v, lambda new, f=field: data.__setitem__(f, new)

    greetings = data.get("alternate_greetings")
    if isinstance(greetings, list):
        for i, g in enumerate(greetings):
            if isinstance(g, str) and g:
                yield f"alternate_greetings[{i}]", g, lambda new, i=i: greetings.__setitem__(i, new)

    book = data.get("character_book")
    if isinstance(book, dict):
        for i, entry in enumerate(book.get("entries") or []):
            if isinstance(entry, dict) and isinstance(entry.get("content"), str) and entry["content"]:
                yield f"lore[{i}].content", entry["content"], lambda new, e=entry: e.__setitem__("content", new)


def _notes_artifacts(notes: str) -> list[str]:
    reasons = []
    if _TAG_RE.search(notes):
        reasons.append("html-tag")
    if _DOCTYPE_RE.search(notes):
        reasons.append("doctype/comment")
    if _CSS_RE.search(notes):
        reasons.append("css-block")
    return reasons


def _diff_snippet(before: str, after: str, ctx: int = 40) -> str:
    n = min(len(before), len(after))
    i = 0
    while i < n and before[i] == after[i]:
        i += 1
    lo = max(0, i - ctx)
    b = before[lo : i + ctx].replace("\n", "\\n")
    a = after[lo : i + ctx].replace("\n", "\\n")
    return f"{b!r} => {a!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cards-dir", type=Path, default=settings.archive_dir)
    parser.add_argument(
        "--repair",
        action="store_true",
        help="rewrite cards whose macros would change, in place (pixels preserved)",
    )
    parser.add_argument("--json", type=Path, default=None, help="also dump full findings as JSON")
    parser.add_argument("--limit", type=int, default=50, help="max cards to list per section (0 = all)")
    args = parser.parse_args()

    if not args.cards_dir.is_dir():
        print(f"cards dir not found: {args.cards_dir}")
        return 1

    sanitizer = MacroSanitizer(user_names=settings.user_names)
    cards = sorted(args.cards_dir.glob("**/*.png"))

    changed = []       # {path, fields:[{field, snippet}]}
    unresolved = []    # {path, macros:{name:count}}
    artifacts = []     # {path, reasons:[...]}
    field_counter: Counter = Counter()
    macro_counter: Counter = Counter()
    unreadable = repaired = 0

    for path in cards:
        raw = path.read_bytes()
        parsed = pngtools.read_envelope(raw)
        if parsed is None:
            unreadable += 1
            continue
        envelope, data = parsed

        card_changes = []
        card_unresolved: Counter = Counter()
        dirty = False
        for label, text, setter in _string_fields(data):
            cleaned, unknown = sanitizer.sanitize(text)
            if cleaned != text:
                card_changes.append({"field": label, "snippet": _diff_snippet(text, cleaned)})
                field_counter[label.split("[")[0]] += 1
                if args.repair:
                    setter(cleaned)
                    dirty = True
            for u in unknown:
                card_unresolved[u] += 1
                macro_counter[u] += 1

        rel = str(path.relative_to(args.cards_dir))
        if card_changes:
            changed.append({"path": rel, "fields": card_changes})
        if card_unresolved:
            unresolved.append({"path": rel, "macros": dict(card_unresolved)})

        notes = data.get("creator_notes")
        if isinstance(notes, str) and notes:
            reasons = _notes_artifacts(notes)
            if reasons:
                artifacts.append({"path": rel, "reasons": reasons})

        if dirty:
            before = pngtools.non_text_chunks(raw)
            new_bytes = pngtools.embed_card(raw, envelope, data)
            after = pngtools.non_text_chunks(new_bytes)
            if before != after:  # must never happen: repair touches only tEXt
                print(f"  ABORT {rel}: pixel chunks changed, not writing")
                continue
            path.write_bytes(new_bytes)
            repaired += 1

    _report(cards, unreadable, changed, unresolved, artifacts, field_counter, macro_counter, args.limit)
    if args.repair:
        print(f"\n>>> REPAIRED {repaired} card(s) in place (pixels preserved).")

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "scanned": len(cards),
                    "unreadable": unreadable,
                    "would_change": changed,
                    "unresolved_macros": unresolved,
                    "notes_artifacts": artifacts,
                },
                indent=2,
            )
        )
        print(f">>> wrote {args.json}")
    return 0


def _report(cards, unreadable, changed, unresolved, artifacts, field_counter, macro_counter, limit):
    def cap(items):
        return items if limit == 0 else items[:limit]

    print(f"=== SCANNED {len(cards)} cards ({unreadable} unreadable) ===\n")

    print(f"### MACRO WOULD-CHANGE: {len(changed)} card(s)   fields={dict(field_counter)}")
    for c in cap(changed):
        print(f"  {c['path']}")
        for f in c["fields"]:
            print(f"      [{f['field']}] {f['snippet']}")
    if limit and len(changed) > limit:
        print(f"  ... and {len(changed) - limit} more")

    print(f"\n### UNRESOLVED MACROS (survive sanitize, need eyeball): {len(unresolved)} card(s)")
    print(f"    macros={dict(macro_counter.most_common())}")
    for u in cap(unresolved):
        print(f"  {u['path']}  ->  {u['macros']}")
    if limit and len(unresolved) > limit:
        print(f"  ... and {len(unresolved) - limit} more")

    print(f"\n### CREATOR_NOTES FORMATTING ARTIFACTS (heuristic, read-only): {len(artifacts)} card(s)")
    for a in cap(artifacts):
        print(f"  {a['path']}  ->  {a['reasons']}")
    if limit and len(artifacts) > limit:
        print(f"  ... and {len(artifacts) - limit} more")


if __name__ == "__main__":
    raise SystemExit(main())
