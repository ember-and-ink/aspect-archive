"""Turning a hand-uploaded file into the bytes a media folder stores
(docs/FORKS_AND_EXTRAS_PLAN.md §9).

`aspect.media.writer` already decides what the archive keeps when it downloads
an image: sniff the bytes, refuse anything that is not an image, re-encode to
WebP unless it is animated. An upload gets the same treatment through the same
functions, so a gallery folder holds one format no matter which door its files
came in through -- and the images-only policy gains its fourth enforcement
point here rather than a second, divergent one.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from aspect.media import writer


class RejectedUpload(Exception):
    """Why one file is not being stored. The message is user-facing: it is
    what the pane prints beside the filename, so it says what arrived rather
    than which check failed."""


@dataclass(frozen=True, slots=True)
class PreparedUpload:
    name: str  # after the WebP rename; the stem is always the uploaded one
    data: bytes


def prepare(filename: str, data: bytes) -> PreparedUpload:
    """Sniff, gate and convert one uploaded file, or raise `RejectedUpload`."""
    name = Path(filename).name
    if not name or name.startswith("."):
        raise RejectedUpload("the upload has no usable filename")
    if not data:
        raise RejectedUpload("the file is empty")
    if len(data) > writer.MAX_MEDIA_BYTES:
        raise RejectedUpload(f"the file is larger than {writer.MAX_MEDIA_BYTES // (1024 * 1024)} MB")

    sniff = writer.sniff_media(data, None)
    if not sniff.valid or not sniff.media_type:
        raise RejectedUpload(sniff.reason)
    if not sniff.media_type.startswith("image/"):
        raise RejectedUpload(f"{sniff.media_type} is not an image; the archive stores images only")

    _check_readable(data, sniff.media_type)
    body, media_type = writer.normalize_to_webp(data, sniff.media_type)
    return PreparedUpload(_renamed(name, media_type), body)


def _check_readable(data: bytes, media_type: str) -> None:
    """Refuse a raster image the decoder cannot open.

    `normalize_to_webp` fails soft -- an unreadable image passes through with
    its original bytes, which is right for the download pipeline (something is
    better than nothing on a flaky CDN) and wrong here. A hand-uploaded file
    that will never render is a mistake worth reporting at the moment it is
    made, rather than a permanently broken thumbnail. SVG is exempt: Pillow
    does not open it, and it is stored as-is by design.
    """
    if media_type == "image/svg+xml":
        return
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except Exception as exc:  # Pillow raises a wide and undocumented set here
        raise RejectedUpload(f"not a readable image: {exc}") from exc


def _renamed(name: str, media_type: str) -> str:
    """`name` with its extension swapped for the one the stored bytes actually
    are. The stem is never touched -- ST derives an expression's label from it
    (§2), so preserving it verbatim is what keeps `joy-_00001_.png` a `joy`
    sprite after conversion."""
    ext = writer.MIME_TO_EXT.get(media_type)
    if not ext:
        return name
    stem = Path(name).stem
    return f"{stem}.{ext}"


# A pack is a few hundred sprites at most (the real corpus tops out at 155 per
# character), and every entry is decompressed into memory. The caps are here so
# a hostile or corrupt zip cannot do that unbounded -- neither is a limit a
# real pack comes anywhere near.
MAX_ZIP_ENTRIES = 2_000
MAX_ZIP_TOTAL_BYTES = 512 * 1024 * 1024


def zip_entries(data: bytes) -> list[tuple[str, bytes]]:
    """`(basename, bytes)` for every file in an uploaded zip.

    Flattened to basenames, exactly as SillyTavern's own `getImageBuffers`
    does, so both shapes `aspect.media.expressions` writes load: the flat
    single-character zip and the `<folder>/<file>` multi-character one. Skips
    directories, dotfiles and macOS's `__MACOSX/` resource forks.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise RejectedUpload(f"not a readable zip: {exc}") from exc

    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_ENTRIES:
            raise RejectedUpload(f"the zip holds more than {MAX_ZIP_ENTRIES} files")
        if sum(i.file_size for i in infos) > MAX_ZIP_TOTAL_BYTES:
            raise RejectedUpload("the zip's contents are too large to unpack")

        out: list[tuple[str, bytes]] = []
        for info in infos:
            name = Path(info.filename).name
            if not name or name.startswith(".") or info.filename.startswith("__MACOSX/"):
                continue
            out.append((name, archive.read(info)))
    return out
