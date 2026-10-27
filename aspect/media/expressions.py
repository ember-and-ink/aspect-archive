"""A character's expression sprites: which labels exist, which filenames
carry them, and zipping the folder for export.

Export comes in two shapes (docs/FORKS_AND_EXTRAS_PLAN.md §2), and they are genuinely
different archives, not the same one with a different root:

* `zip_one` -- one character, flattened to basenames at the zip root. ST's
  importer (`getImageBuffers` behind the *Import Expressions Pack* button)
  keeps only `path.parse(entry.fileName).base`, so a flat zip is the shape
  that button expects; nesting would still load, since it flattens anyway.
* `zip_many` -- several characters, each under its own on-disk folder name
  (`<Name>_<gallery_id>`, exactly the string `resolve_folder` matches on). Not
  loadable through `upload-zip`, which targets one folder and would collapse
  every character into it -- this is for dropping straight back into
  `data/expressions/`.

Plain `zipfile` into an in-memory buffer; both shapes are asked for by hand,
not on a hot path, and a character's expressions run tens of megabytes, not
the hundreds that would make buffering the wrong call.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

# SillyTavern's GoEmotions sprite set, verbatim -- `DEFAULT_EXPRESSIONS` in its
# expressions extension, and the only labels an upload is allowed to carry
# (docs/FORKS_AND_EXTRAS_PLAN.md §2). The real 7,539-file corpus used exactly
# these 28 and no strays.
DEFAULT_EXPRESSIONS = frozenset(
    {
        "admiration",
        "amusement",
        "anger",
        "annoyance",
        "approval",
        "caring",
        "confusion",
        "curiosity",
        "desire",
        "disappointment",
        "disapproval",
        "disgust",
        "embarrassment",
        "excitement",
        "fear",
        "gratitude",
        "grief",
        "joy",
        "love",
        "nervousness",
        "neutral",
        "optimism",
        "pride",
        "realization",
        "relief",
        "remorse",
        "sadness",
        "surprise",
    }
)


def label_of(filename: str) -> str:
    """The expression a sprite filename names, derived exactly as ST's
    `src/endpoints/sprites.js` derives it: lowercase the stem (the name minus
    its last extension), then cut at the first `-` or `.` left in it. So
    `joy.webp`, `joy-1.webp` and `joy-_00004_.webp` are all `joy`.

    Never used to rename anything -- filenames are stored and exported
    verbatim. The frontend's `expressionLabel` is the same function; both
    exist because the label is both a display grouping and an upload gate.
    """
    stem = Path(filename).stem.lower()
    return stem.split("-")[0].split(".")[0]


def rejection_reason(filename: str) -> str | None:
    """Why this sprite is not storable in an expressions folder, or None. The
    label is what is checked, not the rest of the filename: ST ignores
    everything after the first separator, so `joy-whatever_042.webp` is a
    perfectly good `joy`."""
    label = label_of(filename)
    if label not in DEFAULT_EXPRESSIONS:
        return f"{label!r} is not one of SillyTavern's {len(DEFAULT_EXPRESSIONS)} expressions"
    return None


def _files(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")
    )


def zip_one(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in _files(directory):
            zf.write(entry, arcname=entry.name)
    return buffer.getvalue()


def zip_many(folders: list[tuple[str, Path]]) -> bytes:
    """`folders` is `(folder name on disk, directory)` pairs -- the caller has
    already resolved each character's expressions folder."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder_name, directory in folders:
            for entry in _files(directory):
                zf.write(entry, arcname=f"{folder_name}/{entry.name}")
    return buffer.getvalue()
