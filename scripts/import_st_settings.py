"""Seed `data/settings.json` from an existing SillyTavern install.

The browser UI's settings -- provider credentials, followed creators, display
preferences -- used to live in SillyTavern's `settings.json`, written there by
the Character Library extension while it ran inside SillyTavern's page. This
lifts that one key out and stores it as the archive's own settings, so the
standalone browser stops depending on a SillyTavern that is no longer running.

    uv run python scripts/import_st_settings.py            # read-only report
    uv run python scripts/import_st_settings.py --apply

WHAT IS COPIED
Only `extension_settings.SillyTavernCharacterGallery` -- the frontend's own
blob, ~4 KB of the 193 KB file. Nothing else in SillyTavern's settings describes
the archive: the rest is chat presets, samplers, personas and UI state for an
application this one deliberately does not implement.

The blob is copied WHOLE rather than filtered down to a hand-picked subset. A
denylist of "keys the archive doesn't need" would have to be re-audited every
time the frontend gains a setting, and the failure mode of getting it wrong is
silently dropping something like a saved token. The frontend merges whatever it
finds over its own defaults and ignores keys it no longer knows, so carrying a
few dead ones costs nothing.

WHAT IS NOT COPIED, ON PURPOSE
`world_info_settings.world_info.charLore` -- the map of *additional* lorebooks
per character. Lorebook management is deferred, and the archive having no
charLore store is what makes the bundle exporter omit `auxWorlds` from its
manifest instead of writing an empty array. An empty array means "restore no
lorebooks" to an importing SillyTavern, which strips lorebook links from the
cards it lands on. Importing an empty charLore here would re-arm exactly that.

RETIRED (Stage 7 cut-over). The keys this lifts across are the *old* frontend's
schema, and `scripts/prune_settings.py` has since dropped all but the nine the
browser client still reads. Running this again would put them back. Kept only
because the ST-side reading logic is the record of where that blob lived; it has
no live caller.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aspect.state import ui_settings
from aspect.config import settings

# The key the Character Library frontend stores itself under, inside
# SillyTavern's `extension_settings`. Matches SETTINGS_KEY in web/library.js.
CL_SETTINGS_KEY = "SillyTavernCharacterGallery"

# Where a default SillyTavern checkout keeps the default user's settings.
DEFAULT_ST_SETTINGS = Path.home() / "workspaces" / "SillyTavern" / "data" / "default-user" / "settings.json"

# Keys worth naming in the report: the ones whose loss the user would actually
# notice, as opposed to the ~100 display preferences.
NOTABLE = (
    "chubToken",
    "datacatToken",
    "datacatFollowedCreators",
    "janitoraiToken",
    "saucepanToken",
    "civitaiApiKey",
    "disabledProviders",
    "providerOrder",
    "customCSS",
)


def describe(value: object) -> str:
    """A one-line summary that never prints a credential."""
    if value is None:
        return "unset"
    if isinstance(value, str):
        return f"set ({len(value)} chars)" if value else "empty"
    if isinstance(value, (list, dict)):
        return f"{len(value)} item{'' if len(value) == 1 else 's'}"
    return repr(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=DEFAULT_ST_SETTINGS,
        help="SillyTavern's settings.json (default: %(default)s)",
    )
    parser.add_argument(
        "--to",
        dest="dest",
        type=Path,
        default=settings.settings_file,
        help="where to write (default: %(default)s)",
    )
    parser.add_argument("--apply", action="store_true", help="write; otherwise report only")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing destination instead of refusing",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        print(f"no SillyTavern settings at {args.source}", file=sys.stderr)
        print("pass --from <path> to point at one", file=sys.stderr)
        return 1

    try:
        source_blob = json.loads(args.source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"could not read {args.source}: {exc}", file=sys.stderr)
        return 1

    extension_settings = source_blob.get("extension_settings")
    if not isinstance(extension_settings, dict):
        print(f"{args.source} has no extension_settings object", file=sys.stderr)
        return 1

    blob = extension_settings.get(CL_SETTINGS_KEY)
    if not isinstance(blob, dict):
        print(
            f"{args.source} has no {CL_SETTINGS_KEY!r} settings -- "
            "was Character Library ever run in this SillyTavern?",
            file=sys.stderr,
        )
        return 1

    print(f"source      {args.source}")
    print(f"destination {args.dest}")
    print(f"found       {len(blob)} keys under {CL_SETTINGS_KEY}")
    print()
    for key in NOTABLE:
        if key in blob:
            print(f"  {key:26s} {describe(blob[key])}")
    print()

    store = ui_settings.SettingsStore(args.dest)
    if args.dest.exists() and not args.force:
        try:
            existing = store.read()
        except ui_settings.SettingsError as exc:
            print(f"{args.dest} exists and is unreadable ({exc})", file=sys.stderr)
            print("inspect it by hand, or pass --force to overwrite", file=sys.stderr)
            return 1
        print(
            f"{args.dest} already exists with {len(existing)} keys; refusing to overwrite.",
            file=sys.stderr,
        )
        print("pass --force if replacing it is what you want.", file=sys.stderr)
        return 1

    if not args.apply:
        print("dry run -- nothing written. Re-run with --apply.")
        return 0

    try:
        store.write(blob)
    except ui_settings.SettingsError as exc:
        print(f"write failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(blob)} keys to {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
