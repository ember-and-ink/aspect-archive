#!/usr/bin/env python3
"""Write userscript/saucepan-proxy-bridge.user.js from userscript/src_saucepan/*.js.

The concatenation itself lives in `aspect/userscripts.py`, because the server
does the same job for the Settings -> Userscripts generator (with the server URL
and tag filter substituted in). This script is the repo-side half: the
unconfigured bundle, checked in so the file in the tree always matches its
sources.

Run via `make compile`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aspect.userscripts import SPECS, UserscriptError, write_userscript  # noqa: E402


def main() -> None:
    spec = SPECS["saucepan"]
    try:
        out = write_userscript(spec)
    except UserscriptError as exc:
        sys.exit(f"compile_userscript: {exc}")
    print(f"compiled {len(spec.modules) + 1} modules -> {out.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
