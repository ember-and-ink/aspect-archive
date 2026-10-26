"""Drop the dead keys the old frontend left in `data/settings.json`.

The settings blob is one opaque JSON document the server does not schema-check
(`aspect/state/ui_settings.py`), so every key the vendored CharacterLibrary
frontend ever wrote is still sitting in it -- 117 of them, including credentials
for six providers that were cut before the rewrite, a theme customizer, a mobile
mode and a duplicate-scanner's thresholds. None of it is read by anything any
more, and leaving it there makes the file unreadable for the handful of keys
that *are* live.

    uv run python scripts/prune_settings.py            # read-only report
    uv run python scripts/prune_settings.py --apply    # rewrite the file

WHAT SURVIVES
`ui2`, the browser client's own namespace, plus the root keys it deliberately
reads flat -- the provider credentials and `httpProxyUrl`, which are not
preferences this app invented but real data the old UI and the server already
share (see `frontend/src/hooks/use-settings.ts` and `aspect/runtime/net.py`).
That list is the whole schema; anything else is by definition a leftover.

THE ONE MIGRATION
`tagDictionaryDelta` is real user work -- the tag consolidation overrides -- and
it moved namespace in the rewrite: the old tag manager wrote it at the root, the
new Tags page reads `ui2.tagDictionaryDelta`. So it is moved rather than
dropped, and only when `ui2` does not already hold one (a delta edited in the
new UI always wins; the root copy is then the stale one).

This is a one-off for the Stage 7 cut-over (docs/UI_REWRITE_PLAN.md §5). It is
idempotent -- a second run finds nothing to do -- and `--apply` writes a
timestamped backup beside the file first, because the same document holds the
only copy of the Chub and DataCat tokens.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aspect.config import settings  # noqa: E402
from aspect.state import ui_settings  # noqa: E402

# Everything still read from the blob's root, and by whom.
LIVE_ROOT_KEYS = {
    "httpProxyUrl": "aspect/runtime/net.py",
    "chubToken": "Settings -> Providers",
    "chubRememberToken": "Settings -> Providers",
    "chubNsfw": "Discover",
    "datacatToken": "Settings -> Providers",
    "datacatNsfw": "Discover",
    "datacatFollowedCreators": "Discover -> Following",
    "providerExcludeTags": "Discover",
    "ui2": "the client's own namespace",
}


def plan(blob: dict) -> tuple[dict, list[str], str | None]:
    """The blob as it should be, the keys being dropped, and any migration."""
    kept = {k: v for k, v in blob.items() if k in LIVE_ROOT_KEYS}
    dropped = sorted(k for k in blob if k not in LIVE_ROOT_KEYS)

    migrated = None
    delta = blob.get("tagDictionaryDelta")
    ui2 = dict(kept.get("ui2") or {})
    if delta is not None and ui2.get("tagDictionaryDelta") is None:
        ui2["tagDictionaryDelta"] = delta
        kept["ui2"] = ui2
        migrated = "tagDictionaryDelta -> ui2.tagDictionaryDelta"

    return kept, dropped, migrated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the pruned file")
    parser.add_argument(
        "--path",
        type=Path,
        default=settings.settings_file,
        help="the settings file (default: data/settings.json)",
    )
    args = parser.parse_args()

    if not args.path.is_file():
        print(f"no settings file at {args.path} -- nothing to prune")
        return 0

    store = ui_settings.SettingsStore(args.path)
    blob = store.read()
    kept, dropped, migrated = plan(blob)

    print(f"{args.path}: {len(blob)} keys -> {len(kept)}")
    if migrated:
        print(f"  migrate  {migrated}")
    for key in sorted(kept):
        print(f"  keep     {key}  ({LIVE_ROOT_KEYS[key]})")
    for key in dropped:
        print(f"  drop     {key}")

    if not dropped and not migrated:
        print("nothing to do")
        return 0
    if not args.apply:
        print("\nread-only; re-run with --apply to write")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = args.path.with_suffix(f".json.bak-{stamp}")
    shutil.copy2(args.path, backup)
    store.write(kept)
    print(f"\nwrote {args.path} ({len(json.dumps(kept))} bytes); backup at {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
