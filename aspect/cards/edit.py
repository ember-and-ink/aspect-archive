"""Writing to the archive: patch a card in place, replace its avatar, bin it.

Three rules hold this module together, and each one exists because breaking it
would lose data quietly rather than loudly.

**A field edit never touches a pixel.** `pngtools.inject_text_chunks` rewrites
only the tEXt chunks and copies every other chunk -- IHDR, PLTE, tRNS, IDAT,
IEND -- byte for byte, so pngquant's quantized IDAT survives a card edit exactly.
Re-encoding through Pillow would inflate a 700 KB card back to 1.8 MB and strip
the card while it was at it. The one operation that legitimately re-encodes is
replacing the avatar, and it goes the other way round: new pixels, existing card
put back on top.

**Every write is atomic.** Temp file in the same directory, then `os.replace`.
A reader mid-write must never see a truncated PNG, and a crash mid-write must
leave the old card intact rather than half a new one.

**Nothing is unlinked.** Deletes move into `settings.trash_dir`, which is never
scanned, indexed or exported -- the same contract as `data/_quarantine/`. A card
deleted by a misclick is the failure an archive exists to prevent, and disk is
cheaper than the card.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from aspect.cards import gallery, pngtools
from aspect.cards.avatar_image import normalize_avatar
from aspect.config import settings

logger = logging.getLogger("aspect.cards.edit")


class WriteError(Exception):
    """A write that could not be carried out. Carries a message meant for a
    human looking at a toast, not a stack trace."""


# Extension keys the archive owns and cannot reconstruct from anything else, so
# a payload that simply omits them is treated as "unchanged" rather than
# "delete". `gallery_id` is the only link between a card and its images -- lose
# it and the folder is orphaned with no way back -- and `jai` carries the
# provenance every importer stamps: the source id the filename fragment and the
# dedupe check both key on, the source URL, and `linkedAt`, which is the archive's
# only trustworthy "date added". A client that means to drop one has to send it
# explicitly as null.
_PRESERVED_EXTENSIONS = ("gallery_id", "jai")


def write_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data` in one step. The temp file is a sibling so the
    rename stays on one filesystem, where `os.replace` is atomic; across a mount
    boundary it silently degrades to copy-then-delete, which is the thing being
    avoided."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def read_card(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The `(envelope, data)` pair embedded in the card at `path`.

    Raises WriteError rather than returning None: every caller here is about to
    write, and writing over a file whose current contents could not be read is
    how a non-card PNG becomes a card and a corrupt card becomes a plausible one.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WriteError(f"cannot read {path.name}: {exc}") from exc
    envelope = pngtools.read_envelope(raw)
    if envelope is None:
        raise WriteError(f"{path.name} carries no readable character card")
    outer, data = envelope
    if not isinstance(data, dict):
        raise WriteError(f"{path.name} has a card `data` that is not an object")
    return outer, data


def merge_card(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """The card `data` to write, given what is on disk and what the client sent.

    A whole-document replace, with one carve-out: the extension keys in
    `_PRESERVED_EXTENSIONS` are carried over when the payload omits them
    entirely. Replace rather than merge because the client holds the complete
    card and treats itself as the owner -- a merge could never express a
    *deleted* field, and clearing a scenario is an ordinary edit.
    """
    merged = dict(incoming)
    extensions = merged.get("extensions")
    extensions = dict(extensions) if isinstance(extensions, dict) else {}
    previous = existing.get("extensions")
    previous = previous if isinstance(previous, dict) else {}
    for key in _PRESERVED_EXTENSIONS:
        if key not in extensions and key in previous:
            extensions[key] = previous[key]
    merged["extensions"] = extensions
    return merged


def patch_card(path: Path, data: dict[str, Any]) -> None:
    """Re-embed `data` into the card at `path`, keeping its pixels byte-identical.

    `embed_card` rebuilds the canonical envelope -- spec header plus the V2
    top-level mirror -- so a patched card is shaped exactly like a freshly built
    one, and `inject_text_chunks` leaves every non-text chunk alone.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WriteError(f"cannot read {path.name}: {exc}") from exc
    envelope = pngtools.read_envelope(raw)
    if envelope is None:
        raise WriteError(f"{path.name} carries no readable character card")
    try:
        patched = pngtools.embed_card(raw, envelope[0], data)
    except (ValueError, TypeError) as exc:
        raise WriteError(f"cannot re-embed the card: {exc}") from exc
    write_atomic(path, patched)


def replace_avatar(path: Path, image_bytes: bytes, *, compress: bool = True) -> None:
    """Give the card at `path` new pixels, keeping its embedded card intact.

    The only write here that re-encodes, and it runs the same pipeline intake
    does -- normalize to PNG, crop a detected panel stack to its primary
    portrait, cap the longest side, quantize -- so a card whose image was
    replaced is indistinguishable from one that arrived that way. The card is
    injected last because pngquant strips every text chunk it is given.
    """
    envelope, data = read_card(path)

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image = normalize_avatar(image.convert("RGBA"))
    except (OSError, ValueError, TypeError, Image.DecompressionBombError) as exc:
        raise WriteError(f"that file is not an image this can read: {exc}") from exc

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    pixels = buffer.getvalue()

    if compress and settings.compress:
        binary = _pngquant()
        if binary is not None:
            quantized = pngtools.quantize(pixels, binary)
            if quantized is not None:
                pixels = quantized

    try:
        rebuilt = pngtools.embed_card(pixels, envelope, data)
    except (ValueError, TypeError) as exc:
        raise WriteError(f"cannot re-embed the card into the new image: {exc}") from exc
    write_atomic(path, rebuilt)


def _pngquant() -> Path | None:
    """The pngquant binary, resolved the same way PngWriter resolves it: the
    vendored one if it is there, else whatever is on PATH, else none and the
    write falls back to an unquantized PNG. In the container the vendored macOS
    binary is excluded by `.dockerignore` precisely so the apt one wins."""
    configured = settings.pngquant_bin
    if configured.exists():
        return configured
    found = shutil.which("pngquant")
    return Path(found) if found else None


def _trash_root() -> Path:
    """Today's bin, created on first use. Dated so a delete can be undone by
    looking at when it happened, which is how anyone actually remembers it, and
    so one bad afternoon's deletes stay together."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    root = settings.trash_dir / stamp
    root.mkdir(parents=True, exist_ok=True)
    return root


def _unique(target: Path) -> Path:
    """`target`, or the first `name (2).ext` style variant that is free. Deleting
    two cards with the same filename on the same day must not have the second
    one overwrite the first inside the bin -- that would make the bin lossy,
    which is the one thing it may not be."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(2, 1000):
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    raise WriteError(f"cannot find a free name for {target.name} in the bin")


def to_trash(path: Path) -> Path:
    """Move a file or directory into the bin and return where it landed."""
    destination = _unique(_trash_root() / path.name)
    try:
        shutil.move(str(path), str(destination))
    except OSError as exc:
        raise WriteError(f"cannot bin {path.name}: {exc}") from exc
    logger.info("binned %s -> %s", path.name, destination)
    return destination


def trash_gallery(name: str, gallery_id: Any) -> tuple[str, Path] | None:
    """Bin a card's gallery folder, resolved by its id rather than its name --
    see `gallery.resolve_folder`.

    Returns `(folder name it had on disk, where it landed)`, or None when there
    was nothing to bin. The on-disk name is handed back because it, not the
    computed one, is what the gallery thumbnail cache is keyed on.
    """
    folder = gallery.folder_name(name, gallery_id)
    resolved = gallery.resolve_folder(settings.galleries_dir, folder)
    if not resolved:
        return None
    return resolved, to_trash(settings.galleries_dir / resolved)
