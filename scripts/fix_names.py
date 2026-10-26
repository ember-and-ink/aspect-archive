"""Find (and interactively repair) card names that are not actually names.

SillyTavern injects `data.name` as the character's identity -- literally
"You are <name>" -- so `Naomi  your broke roommate started an Onlyfans` tells
the model that is its name, and `Narrator` hides whoever the card is really
about. Neither defect is visible until the roleplay is already going wrong.

This scans the archive (or ./import), classifies each name via
aspect.text.name_repair.diagnose, and shows what it would change:

    uv run python scripts/fix_names.py                  # read-only report
    uv run python scripts/fix_names.py --dir import     # scan a staging folder
    uv run python scripts/fix_names.py --verdict generic
    uv run python scripts/fix_names.py --interactive    # confirm each rename
    uv run python scripts/fix_names.py --json out.json
    uv run python scripts/fix_names.py --stats          # how good were the rules?
    uv run python scripts/fix_names.py --rejudge        # how good are they now?
    uv run python scripts/fix_names.py --prune          # forget deleted cards

Every interactive decision -- including skips and "actually that name was
fine" -- is appended to logs/name_repair.jsonl with the diagnosis that produced
it, so the rules can be scored and improved against real judgements instead of
guesses. `--stats` replays what the rules said at the time; `--rejudge` re-runs
the *current* rules over the same cards, which is the loop to tune against.
`--no-log` opts out.

Nothing is ever renamed without a human saying so. The name engine offers
exactly the right answer about 80% of the time (see aspect/name_repair), which is
good enough to propose and nowhere near good enough to apply blindly -- so there
is deliberately no `--apply-all`. `--interactive` walks the findings one at a time
showing the alternates and a slice of the description, and accepts a typed name
for the cases the rules get wrong.

A rename rewrites `data.name` in place via pngtools.embed_card (tEXt chunks
only, so pngquant-compressed pixels survive byte-for-byte) and renames the file
to match the `<name>_<id8>.png` layout, keeping the id fragment so the card
still reads as acquired.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from aspect.cards import pngtools
from aspect.cards.naming import id_fragment, safe_filename
from aspect.config import settings
from aspect.text.name_repair import GENERIC, JUNK, OK, TITLE, Candidate, Diagnosis, diagnose

_VERDICT_ORDER = (GENERIC, TITLE, JUNK)
_ID_FRAGMENT = re.compile(r"_([A-Za-z0-9]{6,8})$")

# Every interactive decision is appended here as one JSON object per line: the
# diagnosis as the rules saw it (verdict, suggestion, ranked candidates with
# their scoring features) alongside what the human actually chose. That pairing
# is the only ground truth we get for whether the rules are any good -- it is
# lost the moment the session ends otherwise -- so `--stats` can replay it and
# report top-1 accuracy, false-positive rate, and which cases needed a name
# typed by hand (the rules found nothing usable at all).
_LOG_DEFAULT = Path(__file__).resolve().parent.parent / "logs" / "name_repair.jsonl"

# What the human did with a finding.
_ACCEPT = "accept"  # took the top suggestion as-is
_PICK = "pick"  # chose a lower-ranked candidate
_TYPED = "typed"  # typed a name the rules did not offer
_FINE = "fine"  # flagged, but the existing name was already correct
_SKIP = "skip"  # undecided / moved on
_FAILED = "failed"  # a rename was chosen but could not be written
_UNDO = "undo"  # a previous rename was put back


def _iter_cards(cards_dir: Path):
    for path in sorted(cards_dir.glob("*.png")):
        raw = path.read_bytes()
        parsed = pngtools.read_envelope(raw)
        if parsed is None:
            continue
        envelope, data = parsed
        yield path, raw, envelope, data


def _card_id(path: Path, data: dict) -> str:
    """Stable identity for a card, so the same card is recognised across copies.

    A staging copy in ./import and its imported twin in the archive are the same
    card and must not count as two judgements in `--stats`.
    """
    ext = data.get("extensions") or {}
    for key in ("gallery_id",):
        if ext.get(key):
            return str(ext[key])
    for source in ("jai", "datacat"):
        cid = (ext.get(source) or {}).get("id")
        if cid:
            return f"{source}:{cid}"
    m = _ID_FRAGMENT.search(path.stem)
    return m.group(1) if m else path.stem


def _already_imported(cards_dir: Path, findings: list) -> list:
    """Findings whose card already lives in the archive.

    `make import` skips a card whose id is already there and leaves the staged
    PNG in place, so ./import keeps accumulating copies of cards that were
    imported long ago. Renaming those copies edits a dead file -- the archive,
    which is what SillyTavern actually reads, is untouched. This has already
    cost one full pass of duplicated work, so it is now called out up front.
    """
    if cards_dir.resolve() == Path(settings.archive_dir).resolve():
        return []
    archive = Path(settings.archive_dir)
    if not archive.is_dir():
        return []
    known = set()
    for name in (p.stem for p in archive.glob("*.png")):
        m = _ID_FRAGMENT.search(name)
        if m:
            known.add(m.group(1))
    stale = []
    for finding in findings:
        m = _ID_FRAGMENT.search(finding[0].stem)
        if m and m.group(1) in known:
            stale.append(finding)
    return stale


def _describe(data: dict, width: int = 220) -> str:
    """A slice of the definition, for a human deciding what the card really is."""
    text = (data.get("description") or "").strip()
    # Skip past the guardrail/OOC preamble so many JanitorAI cards open with.
    text = re.sub(r"^\s*(?:\[[^\]]*\]\s*)+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text[:width] + ("..." if len(text) > width else "")


def _fragment(path: Path, data: dict) -> str:
    """The `_<id8>` disambiguator this card's filename should carry.

    The card's own `extensions.jai.id` is authoritative and comes first: it is
    the exact value `/existing` and the import both key on, so deriving the
    fragment from it is what keeps a renamed card matchable. The filename regex
    is only a fallback for a card with no stamp at all, and it is the reason
    this is not read off the filename any more -- it demands 6-8 characters,
    while a Chub id can be as short as three (`Aria_1911.png`), so a rename
    keyed on it silently DROPPED the fragment and cut the card loose from every
    id-based check we have.
    """
    stamped = ((data.get("extensions") or {}).get("jai") or {}).get("id") or ""
    fragment = id_fragment(str(stamped))
    if fragment:
        return fragment
    m = _ID_FRAGMENT.search(path.stem)
    return m.group(1) if m else ""


def _new_path(path: Path, new_name: str, data: dict) -> Path:
    """`<name>_<id8>.png`, preserving the card's id fragment."""
    fragment = _fragment(path, data)
    suffix = f"_{fragment}" if fragment else ""
    return path.with_name(f"{safe_filename(new_name)}{suffix}.png")


def _apply(path: Path, raw: bytes, envelope: dict, data: dict, new_name: str) -> str | None:
    """Rewrite data.name in place and rename the file. Returns an error, or None."""
    data["name"] = new_name
    before = pngtools.non_text_chunks(raw)
    new_bytes = pngtools.embed_card(raw, envelope, data)
    if pngtools.non_text_chunks(new_bytes) != before:
        return "pixel chunks changed, refusing to write"
    path.write_bytes(new_bytes)

    target = _new_path(path, new_name, data)
    if target != path:
        # `kate` -> `Kate` is a case-only rename, and on a case-insensitive
        # filesystem (macOS/APFS by default) the target "exists" because it IS
        # this file. Only a genuinely different card is a collision.
        if target.exists() and not target.samefile(path):
            return f"wrote name, but {target.name} already exists -- file not renamed"
        path.rename(target)
    return None


def _print_finding(index: int, total: int, path: Path, dg: Diagnosis, data: dict) -> None:
    print(f"\n[{index}/{total}] {path.name}")
    print(f"    verdict   : {dg.verdict.upper()}  ({dg.reason})")
    print(f"    name      : {dg.raw!r}")
    print(f"    suggestion: {dg.suggestion!r}")
    if dg.dropped_segments:
        print(f"    dropping  : {dg.dropped_segments}")
    if dg.candidates:
        alts = ", ".join(f"{i + 1}) {c.display}" for i, c in enumerate(dg.candidates))
        print(f"    from body : {alts}")
    if dg.ensemble:
        print("    NOTE      : looks like a genuine narrator/ensemble card -- probably leave it")
    print(f"    definition: {_describe(data)}")


def _implausible(name: str, data: dict) -> str | None:
    """Why a typed name looks like a slipped keystroke rather than a name.

    Real damage this prevents: `yy` and `u` were typed at this prompt and
    written straight to cards, which then had to be deleted and restored by
    hand. Short names are legitimate (Io, Vi, Ru, V), so this asks rather than
    refuses -- but a name the definition has never heard of is worth a beat.
    """
    name = name.strip()
    if len(name) < 2:
        return "a single character"
    if len(set(name.lower())) == 1:
        return "the same character repeated"
    if len(name) <= 3:
        body = " ".join(str(data.get(k) or "") for k in ("description", "personality", "scenario"))
        if name.lower() not in body.lower():
            return "nowhere in the definition"
    return None


def _prompt(dg: Diagnosis, data: dict) -> tuple[str, str | None]:
    """Ask what to do. Returns (action, chosen name or None)."""
    options = [c.display for c in dg.candidates]
    hint = "    [enter]=skip  y=accept  1-9=pick  n=name is fine  <text>=type a name  q=quit > "
    while True:
        try:
            reply = input(hint).strip()
        except EOFError:
            return _SKIP, None
        if reply == "":
            return _SKIP, None
        if reply.lower() == "q":
            raise KeyboardInterrupt
        # A deliberate "this was a false positive" is worth far more to the
        # rules than the same keystroke count of skipping, so it gets its own key.
        if reply.lower() == "n":
            return _FINE, None
        if reply.lower() == "y":
            if dg.suggestion:
                return _ACCEPT, dg.suggestion
            print("    no suggestion to accept")
            continue
        if reply.isdigit():
            i = int(reply) - 1
            if 0 <= i < len(options):
                return (_ACCEPT if options[i] == dg.suggestion else _PICK), options[i]
            print("    no such option")
            continue
        why = _implausible(reply, data)
        if why:
            print(f"    !! {reply!r} looks like a slip -- {why}.")
            try:
                if input("    use it as the name anyway? [y/N] > ").strip().lower() != "y":
                    continue
            except EOFError:
                return _SKIP, None
        return _TYPED, reply


def _candidate_row(c: Candidate) -> dict:
    """A candidate plus the features that ranked it, for offline rule tuning."""
    return {
        "display": c.display,
        "score": round(c.score, 4),
        "count": c.count,
        "lowercase": c.lowercase,
        "possessive": c.possessive,
        "first_pos": round(c.first_pos, 4),
        "surname": c.surname,
    }


def _rank_of(name: str | None, dg: Diagnosis) -> int | None:
    """1-based rank of `name` among the candidates, or None if the rules missed it."""
    if not name:
        return None
    lowered = name.casefold()
    for i, c in enumerate(dg.candidates, 1):
        if c.display.casefold() == lowered:
            return i
    return None


def _log(
    log_path: Path,
    session: str,
    path: Path,
    dg: Diagnosis,
    data: dict,
    action: str,
    chosen: str | None,
    new_path: Path | None = None,
    error: str | None = None,
) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": session,
        "card_id": _card_id(path, data),
        "dir": str(path.parent),
        "file": path.name,
        "new_file": new_path.name if new_path else None,
        "verdict": dg.verdict,
        "reason": dg.reason,
        "ensemble": dg.ensemble,
        "name": dg.raw,
        "suggestion": dg.suggestion,
        "kept": dg.kept_segments,
        "dropped": dg.dropped_segments,
        "candidates": [_candidate_row(c) for c in dg.candidates],
        "action": action,
        "chosen": chosen,
        # Did the top guess win, and if not, how far down was the right answer?
        "top1_hit": bool(chosen) and chosen == dg.suggestion,
        "chosen_rank": _rank_of(chosen, dg),
        "error": error,
        # Kept so the log can be read on its own later, after files are renamed.
        "excerpt": _describe(data, width=300),
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _undo(log_path: Path, count: int) -> int:
    """Put the last `count` renames back the way they were, per the log.

    The log already holds the old name and the old filename for every rename,
    so a mistyped name should cost one command -- not deleting the card and
    copying it back by hand.
    """
    if not log_path.is_file():
        print(f"no decision log at {log_path}", file=sys.stderr)
        return 2
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def ref(r: dict) -> str:
        return f"{r['ts']}|{r['file']}"

    spent = {r["undid"] for r in rows if r["action"] == _UNDO and r.get("undid")}
    applied = [
        r
        for r in rows
        if r.get("chosen") and not r.get("error") and r["action"] in (_ACCEPT, _PICK, _TYPED)
        and ref(r) not in spent
    ]
    targets = applied[-count:]
    if not targets:
        print("nothing left to undo")
        return 0

    print(f"about to revert {len(targets)} rename(s), newest first:")
    for r in reversed(targets):
        print(f"  {r['chosen']!r} -> back to {r['name']!r}   ({r['new_file']})")
    try:
        if input("\nproceed? [y/N] > ").strip().lower() != "y":
            print("nothing changed")
            return 1
    except EOFError:
        return 1

    session = uuid.uuid4().hex[:8]
    reverted = 0
    for r in reversed(targets):
        folder = Path(r["dir"])
        path = folder / (r["new_file"] or r["file"])
        if not path.is_file():  # renamed again since; find it by its id fragment
            m = _ID_FRAGMENT.search(Path(r["file"]).stem)
            hits = sorted(folder.glob(f"*_{m.group(1)}.png")) if m else []
            if len(hits) != 1:
                print(f"  !! cannot locate {path.name} -- skipped")
                continue
            path = hits[0]
        raw = path.read_bytes()
        parsed = pngtools.read_envelope(raw)
        if parsed is None:
            print(f"  !! {path.name} has no embedded card -- skipped")
            continue
        envelope, data = parsed
        err = _apply(path, raw, envelope, data, r["name"])
        if err:
            print(f"  !! {path.name}: {err}")
            continue
        reverted += 1
        print(f"  {path.name} -> {r['name']!r}")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "session": session,
                "card_id": r.get("card_id"),
                "dir": r["dir"],
                "file": path.name,
                "new_file": _new_path(path, r["name"], data).name,
                "verdict": r["verdict"],
                "action": _UNDO,
                "undid": ref(r),
                "name": r["chosen"],
                "chosen": r["name"],
                "suggestion": r["suggestion"],
                "top1_hit": False,
                "chosen_rank": None,
                "candidates": [],
                "error": None,
            }, ensure_ascii=False) + "\n")
    print(f"\n>>> reverted {reverted} rename(s).")
    return 0


def _prune(log_path: Path, cards_dir: Path) -> int:
    """Drop decisions about cards that no longer exist.

    A card deleted as junk -- a generic name over a definition with nothing in
    it -- was never winnable, so scoring the rules against it is meaningless and
    makes the false-positive rate look worse than it is. Cards are matched by id
    fragment, which survives every rename, so this only ever catches a genuine
    deletion. Pruned records are moved to a sidecar rather than destroyed, so
    the history of what was judged is still there if it is ever wanted.
    """
    if not log_path.is_file():
        print(f"no decision log at {log_path}", file=sys.stderr)
        return 2
    if not cards_dir.is_dir():
        print(f"not a directory: {cards_dir}", file=sys.stderr)
        return 2

    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    live = {m.group(1) for m in (_ID_FRAGMENT.search(p.stem) for p in cards_dir.glob("*.png")) if m}

    keep, drop = [], []
    for line in lines:
        r = json.loads(line)
        m = _ID_FRAGMENT.search(Path(r["file"]).stem)
        (drop if m and m.group(1) not in live else keep).append((line, r))

    if not drop:
        print(f"nothing to prune -- every card in {log_path.name} is still in {cards_dir}")
        return 0

    by_card: dict[str, list[dict]] = {}
    for _, r in drop:
        by_card.setdefault(_ID_FRAGMENT.search(Path(r["file"]).stem).group(1), []).append(r)
    print(f"{len(drop)} record(s) across {len(by_card)} deleted card(s):")
    for cid, group in by_card.items():
        r = group[-1]
        print(f"   [{r['verdict']:7s}] {r['name']!r}  (last action: {r['action']})")
    try:
        if input(f"\nmove these out of {log_path.name}? [y/N] > ").strip().lower() != "y":
            print("nothing changed")
            return 1
    except EOFError:
        return 1

    sidecar = log_path.with_suffix(".pruned.jsonl")
    with sidecar.open("a", encoding="utf-8") as fh:
        for line, _ in drop:
            fh.write(line + "\n")
    log_path.write_text("".join(line + "\n" for line, _ in keep), encoding="utf-8")
    print(f"\n>>> {len(keep)} record(s) left in {log_path.name}; {len(drop)} moved to {sidecar.name}")
    return 0


def _stats(log_path: Path) -> int:
    """Replay the decision log: how often were the rules right?"""
    if not log_path.is_file():
        print(f"no decision log at {log_path}", file=sys.stderr)
        return 2
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        print(f"{log_path} is empty")
        return 0

    # A reverted rename was a mistake, not a judgement -- drop both it and the
    # undo record, so a slipped keystroke does not read as a rules failure.
    spent = {r["undid"] for r in rows if r.get("action") == _UNDO and r.get("undid")}
    rows = [r for r in rows if r.get("action") != _UNDO and f"{r['ts']}|{r['file']}" not in spent]
    if not rows:
        print(f"{log_path} has no decisions left after undos")
        return 0

    sessions = len({r.get("session") for r in rows})
    print(f"{len(rows)} decision(s) across {sessions} session(s) in {log_path}")

    # The same card can be judged more than once -- a staging copy and then its
    # archive twin, or a second pass over the same folder. Score the latest
    # judgement only, or a card revisited twice gets double the weight.
    def key(r: dict) -> str:
        if r.get("card_id"):
            return r["card_id"]
        m = _ID_FRAGMENT.search(Path(r["file"]).stem)  # records written before card_id
        return m.group(1) if m else f"{r['dir']}/{r['file']}"

    latest: dict[str, dict] = {}
    for r in rows:
        latest[key(r)] = r
    scored = list(latest.values())
    if len(scored) != len(rows):
        print(f"({len(rows) - len(scored)} re-judged card(s) collapsed to their latest decision)")
    print()
    rows = scored

    for verdict in (None, *_VERDICT_ORDER):
        subset = rows if verdict is None else [r for r in rows if r["verdict"] == verdict]
        if not subset:
            continue
        label = "ALL" if verdict is None else verdict.upper()
        actions = Counter(r["action"] for r in subset)
        # Only decisions that judged the suggestion tell us about accuracy;
        # a skip is "I didn't look hard enough", not a verdict on the rules.
        judged = [r for r in subset if r["action"] in (_ACCEPT, _PICK, _TYPED, _FINE)]
        hits = sum(1 for r in judged if r["top1_hit"])
        # "Was the right answer anywhere on screen?" -- either it was the
        # suggestion (junk repairs are string surgery, so they carry no rank)
        # or it was one of the ranked candidates from the body.
        top5 = sum(1 for r in judged if r["top1_hit"] or (r["chosen_rank"] or 99) <= 5)
        missed = [r for r in judged if r["action"] == _TYPED and not r["chosen_rank"]]
        print(f"{label:8s} n={len(subset):4d}  " + "  ".join(f"{k}={v}" for k, v in sorted(actions.items())))
        if judged:
            print(f"         top-1 {hits}/{len(judged)} ({hits / len(judged):.1%})"
                  f"   in top-5 {top5}/{len(judged)} ({top5 / len(judged):.1%})"
                  f"   rules missed entirely: {len(missed)}"
                  f"   false positives: {actions.get(_FINE, 0)}")
        print()

    worst = [r for r in rows if r["action"] in (_TYPED, _FINE)]
    if worst:
        print(f"--- {len(worst)} case(s) the rules got wrong ---")
        for r in worst[:30]:
            got = r["suggestion"]
            want = r["chosen"] or "(name was already fine)"
            print(f"  [{r['verdict']:7s}] {r['name']!r}\n      rules: {got!r}  ->  you: {want!r}")
        if len(worst) > 30:
            print(f"  ... and {len(worst) - 30} more")
    return 0


def _rejudge(log_path: Path, cards_dir: Path) -> int:
    """Score the rules as they are *now* against the logged human answers.

    `--stats` replays what the rules said at decision time, which is frozen
    history: a rule fixed afterwards still reads as a miss there. This instead
    re-runs `diagnose` over each logged card's body with its pre-rename name put
    back, and asks the only question that matters when tuning -- would the rules
    give Matt's answer today?

    Three numbers per verdict, because a rule change can win one and lose
    another: top-1 (exact match, casing included), false positives it now leaves
    alone, and -- the one that vetoes a change -- true findings it has gone
    quiet on. Renames are followed by id fragment; a card that has since been
    deleted is not scored, for the same reason `--prune` exists.
    """
    if not log_path.is_file():
        print(f"no decision log at {log_path}", file=sys.stderr)
        return 2
    if not cards_dir.is_dir():
        print(f"not a directory: {cards_dir}", file=sys.stderr)
        return 2

    live: dict[str, dict] = {}
    for path, _raw, _envelope, data in _iter_cards(cards_dir):
        m = _ID_FRAGMENT.search(path.stem)
        if m:
            live[m.group(1)] = data

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    spent = {r["undid"] for r in rows if r.get("action") == _UNDO and r.get("undid")}
    rows = [r for r in rows if r.get("action") != _UNDO and f"{r['ts']}|{r['file']}" not in spent]

    seen, judged, gone = set(), [], 0
    for r in rows:
        m = _ID_FRAGMENT.search(Path(r.get("new_file") or r["file"]).stem)
        data = live.get(m.group(1)) if m else None
        if data is None:
            gone += 1
            continue
        if (m.group(1), r["name"]) in seen:
            continue
        seen.add((m.group(1), r["name"]))
        judged.append((r, diagnose(dict(data, name=r["name"]))))

    print(f"re-judged {len(judged)} logged decision(s) against {cards_dir}"
          f"{f' ({gone} skipped -- card deleted)' if gone else ''}\n")

    for verdict in (None, *_VERDICT_ORDER):
        subset = judged if verdict is None else [x for x in judged if x[0]["verdict"] == verdict]
        if not subset:
            continue
        repairs = [(r, dg) for r, dg in subset if r["action"] in (_ACCEPT, _PICK, _TYPED)]
        hits = sum(1 for r, dg in repairs if dg.suggestion == r["chosen"])
        fine = [(r, dg) for r, dg in subset if r["action"] == _FINE]
        clean = sum(1 for _r, dg in fine if dg.verdict == OK)
        lost = [(r, dg) for r, dg in subset if r["action"] != _FINE and dg.verdict == OK]
        label = "ALL" if verdict is None else verdict.upper()
        print(f"{label:8s} n={len(subset):4d}  top-1 {hits}/{len(repairs)}"
              f" ({hits / max(len(repairs), 1):.1%})"
              f"   false positives now clean {clean}/{len(fine)}"
              f"   TRUE FINDINGS LOST {len(lost)}")
        for r, dg in lost:
            print(f"           lost: {r['name']!r} -- wanted {r['chosen']!r}")
    print()

    misses = [(r, dg) for r, dg in judged
              if r["action"] in (_ACCEPT, _PICK, _TYPED) and dg.suggestion != r["chosen"]]
    if misses:
        print(f"--- {len(misses)} case(s) the current rules still get wrong ---")
        for r, dg in misses[:30]:
            print(f"  [{r['verdict']:7s}] {r['name']!r}\n"
                  f"      rules: {dg.suggestion!r}  ->  you: {r['chosen']!r}")
        if len(misses) > 30:
            print(f"  ... and {len(misses) - 30} more")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dir", type=Path, default=settings.archive_dir, help="folder to scan")
    parser.add_argument(
        "--verdict",
        choices=_VERDICT_ORDER,
        action="append",
        help="only report these verdicts (repeatable; default: all problems)",
    )
    parser.add_argument(
        "--include-ensemble",
        action="store_true",
        help="also list cards that look like genuine narrator/ensemble pieces",
    )
    parser.add_argument("--interactive", action="store_true", help="confirm and apply renames")
    parser.add_argument("--json", type=Path, default=None, help="dump findings as JSON")
    parser.add_argument("--limit", type=int, default=40, help="max rows to print (0 = all)")
    parser.add_argument(
        "--log",
        type=Path,
        default=_LOG_DEFAULT,
        help=f"append interactive decisions here as JSONL (default: {_LOG_DEFAULT})",
    )
    parser.add_argument("--no-log", action="store_true", help="do not record decisions")
    parser.add_argument(
        "--undo",
        type=int,
        nargs="?",
        const=1,
        default=None,
        metavar="N",
        help="revert the last N renames recorded in the log (default 1)",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="drop log decisions about cards that have since been deleted",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="summarise the decision log (rule accuracy) instead of scanning cards",
    )
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="score the CURRENT rules against the logged decisions (tuning loop)",
    )
    args = parser.parse_args()

    if args.rejudge:
        return _rejudge(args.log, args.dir)

    if args.stats:
        return _stats(args.log)

    if args.undo is not None:
        return _undo(args.log, max(args.undo, 1))

    if args.prune:
        return _prune(args.log, args.dir)

    if not args.dir.is_dir():
        print(f"not a directory: {args.dir}", file=sys.stderr)
        return 2

    wanted = set(args.verdict) if args.verdict else set(_VERDICT_ORDER)
    findings, scanned, counts = [], 0, dict.fromkeys((OK, *_VERDICT_ORDER), 0)

    hidden = Counter()
    for path, raw, envelope, data in _iter_cards(args.dir):
        scanned += 1
        dg = diagnose(data)
        counts[dg.verdict] = counts.get(dg.verdict, 0) + 1
        if dg.verdict not in wanted:
            continue
        # Being an ensemble card only excuses a *generic* name: `Narrator` is a
        # fair name for a world card, and renaming one to a character it merely
        # portrays is the mistake that produced "Chief". It excuses nothing
        # about a JUNK or TITLE name -- `A Mother's Claim, A Daughter's Hunger`
        # is a scenario title whoever is speaking, and hiding it just because
        # the card runs a cast meant a genuinely broken name went unreviewed.
        if dg.ensemble and dg.verdict == GENERIC and not args.include_ensemble:
            hidden[dg.verdict] += 1
            continue
        findings.append((path, raw, envelope, data, dg))

    print(f"scanned {scanned} card(s) in {args.dir}")
    for verdict in (OK, *_VERDICT_ORDER):
        n = counts.get(verdict, 0)
        print(f"  {verdict.upper():8s} {n:5d}  ({n / max(scanned, 1):5.1%})")
    print(f"\n{len(findings)} card(s) to review")
    if hidden:
        detail = ", ".join(f"{n} {v.upper()}" for v, n in sorted(hidden.items()))
        print(f"({sum(hidden.values())} hidden -- {detail}: genuine narrator/ensemble cards "
              "whose name is fine as it is; --include-ensemble to show)")

    stale = _already_imported(args.dir, findings)
    if stale:
        print(f"\n!! {len(stale)} of these {len(findings)} card(s) are ALREADY in the archive")
        print(f"!! ({settings.archive_dir}).")
        print("!! `make import` skips cards it has already taken and leaves the staged PNG")
        print("!! behind, so renaming here edits a dead copy -- SillyTavern reads the archive.")
        print("!! Scan the archive instead:  make names ARGS=--interactive")
        if args.interactive:
            try:
                if input("\ncontinue anyway? [y/N] > ").strip().lower() != "y":
                    return 1
            except EOFError:
                return 1

    renamed = 0
    session = uuid.uuid4().hex[:8]
    logging = args.interactive and not args.no_log
    try:
        for i, (path, raw, envelope, data, dg) in enumerate(findings, 1):
            if not args.interactive and args.limit and i > args.limit:
                print(f"\n... and {len(findings) - args.limit} more (use --limit 0 to see all)")
                break
            _print_finding(i, len(findings), path, dg, data)
            if not args.interactive:
                continue

            action, chosen = _prompt(dg, data)
            if chosen == dg.raw:  # typed the existing name back at us
                action, chosen = _FINE, None

            err = new_path = None
            if chosen:
                err = _apply(path, raw, envelope, data, chosen)
                if err:
                    print(f"    !! {err}")
                    action = _FAILED
                else:
                    new_path = _new_path(path, chosen)
                    print(f"    renamed -> {chosen!r}")
                    renamed += 1

            if logging:
                _log(args.log, session, path, dg, data, action, chosen, new_path, err)
    except KeyboardInterrupt:
        print("\naborted")

    if args.interactive:
        print(f"\n>>> renamed {renamed} card(s) (pixels preserved).")
        if logging:
            print(f">>> decisions logged to {args.log} (session {session})")
            print(">>> review rule accuracy with: make names ARGS=--stats")

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "scanned": scanned,
                    "counts": counts,
                    "findings": [
                        {
                            "file": p.name,
                            "verdict": d.verdict,
                            "name": d.raw,
                            "suggestion": d.suggestion,
                            "ensemble": d.ensemble,
                            "dropped": d.dropped_segments,
                            "candidates": [c.display for c in d.candidates],
                        }
                        for p, _, _, _, d in findings
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        print(f">>> wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
