"""Thumbnails for the browse grid.

The archive averages 800 KB a card, so a 40-card viewport is 32 MB of PNG. The
grid cannot serve originals; it needs a small derivative of every card. What it
does *not* need is a thumbnailer built from scratch, because two populated caches
came across at cutover and cover 99.6% of the archive between them:

    data/cache/thumbs/avatar/    <- SillyTavern's thumbnails/avatar
                                    4,663 files, 10.3 KB average, keyed by the
                                    *exact* card filename. 3,823 of 3,839 cards
                                    already have one; 16 need generating, and
                                    840 belong to cards that no longer exist.
    data/cache/thumbs/gallery/   <- CharacterLibrary's user/cl_thumbs
                                    3,446 folders on the <Name>_<gallery_id>
                                    convention.

So the job is inherit, generate on miss, prune the orphans -- and one trap.

**Every inherited avatar thumb is JPEG data behind a `.png` extension.** All
4,663 of them: 96x144, progressive JPEG, named `<card>.png` because SillyTavern
names the thumb after the card and never revisits the extension. Deriving a
`Content-Type` from the extension therefore serves `image/png` for JPEG bytes on
99.6% of the archive. Browsers sniff and cope, but caches and any non-browser
client are entitled not to, so the media type here is always read from the file's
magic number and never from its name.

Avatar thumbs generated on a miss keep the card's filename rather than "fixing"
the extension. A cache where one file in 240 is addressed differently from the
rest is a worse bug than an inaccurate extension that is never trusted anyway,
and the sniff makes the inaccuracy harmless.

What they no longer keep is the JPEG: everything generated here is WebP now,
roughly half the bytes at matched quality. That is a one-way door on drop-in
SillyTavern compatibility, and taken deliberately -- host compatibility is
already out of scope. It needs no migration either, precisely because of the
sniff: inherited JPEGs and generated WebP coexist in a directory and both serve
correctly. Only the *gallery* cache carries the new format in its filenames
(`_<size>.webp`), because its geometry changed at the same time and its old
files are being purged rather than kept.

The whole directory is a cache: deleting it costs one regeneration pass, never
data.
"""

from __future__ import annotations

import glob
import io
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from aspect.cards import pngtools
from aspect.config import settings

logger = logging.getLogger("aspect.archive.thumbs")

# SillyTavern's avatar thumbnail geometry, matched deliberately. The inherited
# cache is 3,823 files at 96x144 and a generated thumb has to sit beside them
# without the grid showing two sizes -- so this is fixed by the inheritance, not
# chosen. Raising it means regenerating the whole cache and throwing away the
# 99.6% head start, which is a separate decision from getting the grid working.
THUMB_SIZE = (96, 144)
# The gallery-thumb edge. Was 384 to match CharacterLibrary's inherited folders;
# now sized to what the grid actually renders -- see `_render_thumb` on why the
# geometry changed with it.
GALLERY_THUMB_SIZE = 288
# What `gallery(...)` can actually render. A `.gif` thumbnails to its first frame,
# which is what a grid wants; video and audio get no thumb at all rather than a
# placeholder the client would have to recognise. Shared between the API (which
# extension gets a `thumb_url`) and `scripts/sync_thumbs.py --galleries` (which
# extension gets pre-rendered).
THUMBABLE_EXTS = frozenset((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))

# Every thumb this module *generates* is WebP. At the sizes a thumb cache deals
# in it is roughly half the bytes of the JPEG it replaces at matched quality
# (measured over 60 real gallery images: 14.9 KB vs 31.8 KB) for ~15% more
# encode time, which a write-once cache does not care about. Nothing has to be
# migrated for this: the media type is sniffed from the file's magic number
# rather than its name, so an inherited JPEG and a generated WebP can sit in the
# same directory and both be served correctly.
_THUMB_FORMAT = "WEBP"
_THUMB_EXT = ".webp"
_WEBP_QUALITY = 80
# The extensions a *gallery* thumb file can carry: what we write now, plus the
# JPEG the inherited cache is full of. Pruning has to recognise both or it stops
# recognising its own files and leaves them behind forever.
_GALLERY_THUMB_EXTS = (_THUMB_EXT, ".jpg")

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)
_FALLBACK_MEDIA_TYPE = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class ThumbFile:
    """A thumbnail on disk, with the media type its *bytes* say it is."""

    path: Path
    media_type: str


def sniff_media_type(header: bytes) -> str:
    """The media type of an image from its leading bytes. WebP needs both ends of
    its 12-byte RIFF header checked, hence the special case; everything else is a
    plain prefix. Unrecognized bytes get the generic type rather than a guess --
    an unknown thumb is a bug to notice, not to paper over with `image/png`."""
    for magic, media_type in _MAGIC:
        if header.startswith(magic):
            return media_type
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return _FALLBACK_MEDIA_TYPE


def media_type_of(path: Path) -> str:
    """Sniff a thumb file's media type. Never trusts the extension -- see the
    module docstring: the inherited avatar cache is JPEG named `.png`."""
    try:
        with path.open("rb") as fh:
            return sniff_media_type(fh.read(12))
    except OSError:
        return _FALLBACK_MEDIA_TYPE


class ThumbnailStore:
    """The thumbnail cache: look up, generate on miss, prune orphans."""

    def __init__(self, root: Path | None = None, archive_dir: Path | None = None) -> None:
        self.root = root or settings.thumbs_dir
        self.archive_dir = archive_dir or settings.archive_dir

    @property
    def avatar_dir(self) -> Path:
        return self.root / "avatar"

    @property
    def gallery_dir(self) -> Path:
        return self.root / "gallery"

    @property
    def expression_dir(self) -> Path:
        return self.root / "expression"

    def avatar_path(self, filename: str, size: int | None = None) -> Path:
        """Where a card's avatar thumb lives. Keyed by the exact card filename,
        extension included, which is SillyTavern's convention and what makes the
        inherited cache usable without a rename pass.

        `size` is the *height* of a larger derivative, and lands in its own
        sibling directory rather than beside the inherited files. The inherited
        cache is 3,839 files at one fixed geometry and it is worth keeping
        exactly as it is: mixing sizes into it would mean either a rename pass
        over the lot or a directory where the size of a file is unknowable from
        its name. `size=None` -- the default -- is that inherited cache.
        """
        if size is None:
            return self.avatar_dir / filename
        return self.root / f"avatar_{size}" / filename

    def gallery_path(self, folder: str, filename: str, size: int = GALLERY_THUMB_SIZE) -> Path:
        """Where a gallery image's thumb lives: `<folder>/<file>_<size>.webp`.

        CharacterLibrary's `cl_thumbs` layout, kept -- the size is in the *name*
        rather than a subdirectory, so one folder holds every size ever
        requested for its images. Only the extension moved, because the bytes
        are WebP now and a `.jpg` holding WebP would be the same
        extension-is-a-lie trap the avatar cache already carries; there is no
        reason to inherit that deliberately when starting a fresh geometry.
        """
        return self.gallery_dir / folder / f"{filename}_{size}{_THUMB_EXT}"

    def expression_path(self, folder: str, filename: str, size: int = GALLERY_THUMB_SIZE) -> Path:
        """Where an expression sprite's thumb lives -- the gallery cache's own
        `<folder>/<file>_<size>.webp` scheme, under its own root so the two
        never collide even when a character's gallery and expression folders
        share the same name."""
        return self.expression_dir / folder / f"{filename}_{size}{_THUMB_EXT}"

    def gallery(
        self, source: Path, folder: str, filename: str, size: int = GALLERY_THUMB_SIZE
    ) -> ThumbFile | None:
        """A gallery image's thumbnail, generating it if the cache misses.

        `source` is passed in rather than derived because the galleries live
        outside this store's roots -- the store owns the cache, not the images.
        """
        path = self.gallery_path(folder, filename, size)
        if path.is_file():
            return ThumbFile(path, media_type_of(path))
        return self.generate_gallery(source, folder, filename, size)

    def generate_gallery(
        self, source: Path, folder: str, filename: str, size: int = GALLERY_THUMB_SIZE
    ) -> ThumbFile | None:
        """Render one gallery image's thumb and cache it. Returns None for
        anything Pillow cannot open -- galleries are images-only now
        (`writer.UNSUPPORTED_EXT_RE`), but an svg or a truncated file still
        can't be rendered, and that is the caller's problem to present rather
        than this one's to guess at."""
        try:
            data = _render_thumb(source.read_bytes(), size=(size, size))
        except (OSError, ValueError, TypeError, Image.DecompressionBombError) as exc:
            logger.info("thumbs: cannot render gallery image %s/%s: %s", folder, filename, exc)
            return None
        path = self.gallery_path(folder, filename, size)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".part")
        temporary.write_bytes(data)
        temporary.replace(path)
        return ThumbFile(path, sniff_media_type(data))

    def expression(
        self, source: Path, folder: str, filename: str, size: int = GALLERY_THUMB_SIZE
    ) -> ThumbFile | None:
        """An expression sprite's thumbnail, generating it if the cache misses.
        Mirrors `gallery()` exactly -- see there for why `source` is passed in
        rather than derived."""
        path = self.expression_path(folder, filename, size)
        if path.is_file():
            return ThumbFile(path, media_type_of(path))
        return self.generate_expression(source, folder, filename, size)

    def generate_expression(
        self, source: Path, folder: str, filename: str, size: int = GALLERY_THUMB_SIZE
    ) -> ThumbFile | None:
        """Render one expression sprite's thumb and cache it. Mirrors
        `generate_gallery()`."""
        try:
            data = _render_thumb(source.read_bytes(), size=(size, size))
        except (OSError, ValueError, TypeError, Image.DecompressionBombError) as exc:
            logger.info("thumbs: cannot render expression image %s/%s: %s", folder, filename, exc)
            return None
        path = self.expression_path(folder, filename, size)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".part")
        temporary.write_bytes(data)
        temporary.replace(path)
        return ThumbFile(path, sniff_media_type(data))

    def avatar(
        self, filename: str, *, size: int | None = None, generate: bool = True
    ) -> ThumbFile | None:
        """A card's avatar thumbnail, generating it if the cache misses.

        Returns None only when the thumb is absent *and* cannot be made -- an
        unparseable card, an unreadable file. Callers fall back to serving the
        full PNG, which is correct but heavy, so a None is worth logging."""
        path = self.avatar_path(filename, size)
        if path.is_file():
            return ThumbFile(path, media_type_of(path))
        if not generate:
            return None
        return self.generate_avatar(filename, size)

    def generate_avatar(self, filename: str, size: int | None = None) -> ThumbFile | None:
        """Render one card's avatar thumb and cache it. Overwrites any existing
        thumb, so this doubles as the repair path for a stale or corrupt one."""
        source = self.archive_dir / filename
        try:
            data = _render_thumb(source.read_bytes(), size=_avatar_box(size))
        except (OSError, ValueError, TypeError, Image.DecompressionBombError) as exc:
            logger.warning("thumbs: cannot render %s: %s", filename, exc)
            return None
        path = self.avatar_path(filename, size)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a browse grid requests dozens of thumbs at once and a
        # reader must never see a half-written file.
        temporary = path.with_name(path.name + ".part")
        temporary.write_bytes(data)
        temporary.replace(path)
        return ThumbFile(path, sniff_media_type(data))

    # --- maintenance ---------------------------------------------------------

    def forget(self, filename: str) -> int:
        """Drop every cached avatar thumb for a card, at every size. Returns how
        many files went.

        Not housekeeping -- correctness. The avatar cache is keyed on the card
        filename and has no staleness check against the card's mtime, because a
        card's pixels never used to change. They do now: replacing an avatar
        rewrites the same filename with different pixels, and deleting a card
        frees its filename for a future card to reuse. Either way a thumb left
        behind is served in place of the real image indefinitely.
        """
        removed = 0
        for directory in sorted(self.root.glob("avatar*")):
            if not directory.is_dir():
                continue
            candidate = directory / filename
            try:
                candidate.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def forget_gallery(self, folder: str, filename: str | None = None) -> int:
        """Drop cached gallery thumbs -- one image's, at every size, or the whole
        folder's when `filename` is None."""
        directory = self.gallery_dir / folder
        if not directory.is_dir():
            return 0
        if filename is None:
            shutil.rmtree(directory, ignore_errors=True)
            return 1
        removed = 0
        # `<image>_<size>.<ext>`, so one image has as many thumbs as sizes asked
        # for -- and, across the format change, as extensions too.
        for extension in _GALLERY_THUMB_EXTS:
            for candidate in directory.glob(f"{glob.escape(filename)}_*{extension}"):
                try:
                    candidate.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def forget_expression(self, folder: str, filename: str | None = None) -> int:
        """Mirrors `forget_gallery()`, over the expression cache."""
        directory = self.expression_dir / folder
        if not directory.is_dir():
            return 0
        if filename is None:
            shutil.rmtree(directory, ignore_errors=True)
            return 1
        removed = 0
        for extension in _GALLERY_THUMB_EXTS:
            for candidate in directory.glob(f"{glob.escape(filename)}_*{extension}"):
                try:
                    candidate.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def prune_gallery(self, folder: str, live_filenames: set[str]) -> int:
        """Delete cached gallery thumbs whose source image is no longer in the
        gallery folder -- docs/PHASE_3C_PLAN.md §5. `live_filenames` is the
        caller's own scandir of the gallery, so this only ever reads the
        thumb cache."""
        directory = self.gallery_dir / folder
        if not directory.is_dir():
            return 0
        removed = 0
        for candidate in directory.iterdir():
            if not candidate.is_file():
                continue
            source = _gallery_thumb_source_name(candidate.name)
            if source is None or source in live_filenames:
                continue
            try:
                candidate.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def prune_expression(self, folder: str, live_filenames: set[str]) -> int:
        """Mirrors `prune_gallery()`, over the expression cache."""
        directory = self.expression_dir / folder
        if not directory.is_dir():
            return 0
        removed = 0
        for candidate in directory.iterdir():
            if not candidate.is_file():
                continue
            source = _gallery_thumb_source_name(candidate.name)
            if source is None or source in live_filenames:
                continue
            try:
                candidate.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def missing(self, filenames: list[str]) -> list[str]:
        """Which of these cards have no avatar thumb yet."""
        return [name for name in filenames if not self.avatar_path(name).is_file()]

    def stale(self, filenames: list[str]) -> list[Path]:
        """Avatar thumbs whose card is gone -- renamed or deleted. 840 of these
        came across at cutover, the residue of every `make names` rename and
        every card removed from the archive over its life."""
        known = set(filenames)
        try:
            entries = sorted(self.avatar_dir.iterdir())
        except OSError:
            return []
        return [p for p in entries if p.is_file() and p.name not in known]


_GALLERY_THUMB_RE = re.compile(
    r"^(?P<name>.+)_(?P<size>\d+)(?P<ext>%s)$"
    % "|".join(re.escape(extension) for extension in _GALLERY_THUMB_EXTS)
)


def _gallery_thumb_source_name(thumb_filename: str) -> str | None:
    """The source image a gallery thumb file name (`<image>_<size>.<ext>`) was
    rendered from, or None for anything that isn't one of ours. Both the WebP
    written now and the inherited JPEG count as ours -- a pruner that only knows
    the current format silently abandons the cache it is meant to be cleaning."""
    match = _GALLERY_THUMB_RE.match(thumb_filename)
    return match.group("name") if match else None


def _avatar_box(size: int | None) -> tuple[int, int]:
    """The crop box for an avatar thumb of the given height.

    Fixed at THUMB_SIZE's 2:3 aspect whatever the height, because the grid tile
    is 2:3 and a thumb that does not match it either letterboxes or gets
    stretched by the browser. So one number is enough to ask for a bigger one.
    """
    if size is None:
        return THUMB_SIZE
    width, height = THUMB_SIZE
    return (max(1, round(size * width / height)), size)


def _render_thumb(source: bytes, *, size: tuple[int, int] = THUMB_SIZE) -> bytes:
    """An image's pixels as a small WebP, cover-cropped to fill `size` exactly.

    Both callers cover-crop, because both consumers do. A *card* avatar fills a
    2:3 grid tile; a *gallery* image fills a square one (`.sprite-item` is
    `aspect-ratio: 1` with `object-fit: cover`). Gallery thumbs used to be
    *fitted* inside a square box instead, on the reasoning that galleries hold
    every aspect ratio and cropping a wide illustration to a square destroys the
    picture. True in general, but it was the wrong trade here: the browser then
    cover-cropped the result anyway, so the preserved edge was decoded and
    discarded, and -- worse -- the box was sized on the image's *long* edge while
    the tile renders from its *short* one. A 2:3 portrait fitted into 384 came
    out 256 wide and looked soft in a 150px tile at 2x DPR while shipping a third
    of its bytes into a crop. Cropping here instead makes the delivered pixels
    exactly the rendered pixels. The thumb is a worse picture and a better
    thumbnail; the full image is one click away.

    For a card the text chunks are irrelevant here -- only the pixels matter --
    but they are why the source is large, and why `Image.open` on a 1.2 MB card
    is still the cheap part next to the resize.
    """
    image = Image.open(io.BytesIO(source))
    image.load()
    # Flatten onto white rather than dropping the alpha channel, so a
    # transparent-background avatar comes out white-backed instead of black.
    # WebP could carry the alpha, but the tiles it lands in have their own
    # background and a half-transparent thumb reads as a rendering bug.
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        flattened = Image.new("RGB", image.size, (255, 255, 255))
        flattened.paste(image, mask=image.split()[-1])
        image = flattened
    else:
        image = image.convert("RGB")

    target_w, target_h = size
    src_w, src_h = image.size
    # Fill the box and overflow, then trim the overflow off -- but never upscale.
    # A gallery holding a 64px sprite should get a 64px thumb, not a blurry 288px
    # one that is bigger than the image it stands in for; the tile's own
    # `object-fit: cover` finishes the job for anything that lands short.
    scale = min(1.0, max(target_w / src_w, target_h / src_h))
    resized = image.resize(
        (max(1, round(src_w * scale)), max(1, round(src_h * scale))),
        Image.LANCZOS,
    )
    target_w = min(target_w, resized.width)
    target_h = min(target_h, resized.height)
    left = (resized.width - target_w) // 2
    # Bias the vertical crop upward: on a portrait avatar the face is above
    # centre, and a centred crop of a tall image is the classic way to serve
    # a grid full of torsos. Gallery art is mostly portrait too, so it wants the
    # same bias.
    top = (resized.height - target_h) // 3
    resized = resized.crop((left, top, left + target_w, top + target_h))

    buffer = io.BytesIO()
    resized.save(buffer, _THUMB_FORMAT, quality=_WEBP_QUALITY)
    return buffer.getvalue()


def has_embedded_card(png: bytes) -> bool:
    """Whether these bytes carry a character card. Only used by the maintenance
    script, to tell a real card apart from a stray image in the archive."""
    try:
        return pngtools.read_envelope(png) is not None
    except (ValueError, TypeError):
        return False
