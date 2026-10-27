"""The server-side media download pipeline -- docs/PHASE_3C_PLAN.md §3, step 4.

One function, `download_item`, does everything the plan lists for one URL, in
order: guard, name-index skip, dead-ledger skip, fetch, sniff, normalize to
WebP, content-hash dedupe, write, thumbnail, manifest record. It is the thing
`POST /api/v1/characters/{id}/media` (aspect/api/v1/) calls once per item and
streams the result of as one NDJSON line.

**Sniffing never trusts the source.** `sniff_media` is a verbatim port of
`validateMediaContent` (`30-media-localization-feature.js:622-725`), magic
bytes and the MP4 audio-vs-video atom walk included: the content type decides
the extension, never the `Content-Type` header or the URL's own suffix. A CDN
that mislabels a JPEG as `application/octet-stream` still gets sniffed and
saved correctly; a URL that serves an HTML error page gets caught here rather
than saved as a broken image.

**WebP happens once, at intake, for stills only.** png/jpeg/bmp and
single-frame gif/webp are re-encoded (`quality=82, method=4`); animated
gif/webp and svg pass through untouched -- see §4 and §6 of the plan for why
animated formats are deliberately out of scope here.

**Images only.** Audio and video are refused, not stored: see
`UNSUPPORTED_EXT_RE` for why (nothing downstream can use them) and the two
places that enforce it (`download_item` step 0b on the URL suffix,
`finish_item` step 5b on the sniffed type).

**Dedupe is a local `scandir` and some SHA-256 reads, not an HTTP round
trip** -- the entire point of moving the fetch server-side (§3 "Dedup is a
scandir"). `GalleryIndex.build` scans the folder once per request and is
reused across every item in the batch; the SHA-256 half of it is built only
if some item actually gets far enough to need it. Ahead of both sits
`manifest_hit`, an exact URL lookup in the manifest this gallery already
wrote -- the only skip that works for URLs whose filename is too short to
derive a dedup key from.

**`download_batch` runs the items concurrently**, bounded per host, and is
what both entry points loop over. Its docstring has the reason that needs no
lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageSequence

from aspect.cards import edit
from aspect.archive import thumbs
from aspect.config import settings
from aspect.media import guard as media_guard, manifest as media_manifest, mega as media_mega, names as media_names

logger = logging.getLogger("aspect.media.writer")

# Verbatim MAX_MEDIA_BYTES already lives in media_guard; re-exported for
# callers that only import this module.
MAX_MEDIA_BYTES = media_guard.MAX_MEDIA_BYTES

# Port of the `mimeToExt` map (30-media-localization-feature.js:993-1017).
MIME_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/aac": "aac",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
}

# Still-image types converted to WebP at intake (§4). Anything else that gets
# this far -- animated gif/webp, svg -- is stored exactly as sniffed. Audio and
# video never reach here; step 5b refuses them first.
_WEBP_CONVERTIBLE_TYPES = {"image/png", "image/jpeg", "image/bmp", "image/gif", "image/webp"}

_WEBP_QUALITY = 82
_WEBP_METHOD = 4

# 401/402/403 are ours, not media-dedup.js's: an access refusal doesn't
# change on retry the way a 429 or a 5xx does. Credentials we don't have
# aren't credentials we'll acquire, and a host that pulled an image behind
# auth (preview.redd.it without its signature, Cloudflare on chub.ai) is not
# going to hand it back on attempt four. 429 stays transient -- that one
# genuinely is "come back later".
PERMANENT_HTTP = frozenset({400, 401, 402, 403, 404, 410, 451})

MEDIA_EXT_RE = re.compile(r"\.(png|jpg|jpeg|webp|gif|bmp|svg|avif)$", re.I)

# Images only, by policy. The archive's whole output is V3 PNG cards and
# gallery folders for SillyTavern, and ST has no surface that plays a gallery
# mp3; its expression feature wants static png/webp sprites, which a webm
# cannot drive. The corpus proved the point -- 87 audio files (570MB, one
# character alone holding 143MB of mp3) and 16 video files (28MB), zero of
# them usable downstream. So audio and video are refused rather than stored
# and ignored, at both doors: by URL suffix before any bytes move, and by
# sniffed type in `finish_item` for the URLs whose suffix lies or is absent.
UNSUPPORTED_EXT_RE = re.compile(r"\.(mp3|wav|ogg|opus|m4a|m4b|flac|aac|mid|midi|mp4|webm|mov|m4v|mkv|avi|flv)$", re.I)


def unsupported_url_reason(url: str) -> str | None:
    """Why this URL is refused on its suffix alone, or None to let it through.

    Matched against the *path* only: a query string routinely carries its own
    dots (`?v=1.2`) and a CDN signature can end in anything, so matching the
    whole URL would both miss `a.mp3?token=x` and misjudge `a.png?fmt=mp4`.
    Nothing is persisted for this skip -- it is a pure function of the URL, so
    re-deciding it next run costs nothing and a policy change needs no
    unwinding of manifests.
    """
    match = UNSUPPORTED_EXT_RE.search(urlsplit(url).path)
    if not match:
        return None
    return f"{match.group(1).lower()} is not an image (audio/video not archived)"


@dataclass(frozen=True, slots=True)
class SniffResult:
    valid: bool
    media_type: str | None
    reason: str


def _mp4_has_video_track(data: bytes) -> bool:
    """Port of `mp4HasVideoTrack` (30-media-localization-feature.js:561-610).

    Scans MP4/M4A atoms for a `hdlr` box whose handler type is `vide`; the
    same container shape covers video and audio-only M4A, and this is the
    only way to tell them apart from the bytes.
    """
    length = len(data)
    pos = 0
    container_atoms = {"moov", "trak", "mdia", "minf", "stbl", "udta", "edts"}
    while pos < length - 24:
        atom_size = int.from_bytes(data[pos : pos + 4], "big")
        atom_type = data[pos + 4 : pos + 8].decode("latin-1")

        if atom_type == "hdlr" and atom_size >= 24:
            handler_type = data[pos + 16 : pos + 20].decode("latin-1")
            if handler_type == "vide":
                return True

        if atom_size in (0, 1):
            break
        if atom_type in container_atoms:
            pos += 8
        else:
            pos += atom_size
        if atom_size < 8 and atom_type not in container_atoms:
            break
    return False


def sniff_media(data: bytes, content_type: str | None) -> SniffResult:
    """Port of `validateMediaContent` (30-media-localization-feature.js:622-725)."""
    if not data or len(data) < 8:
        return SniffResult(False, None, "Content too small to be valid media")

    b = data
    if b[0:4] == b"\x89PNG":
        return SniffResult(True, "image/png", "Valid PNG")
    if b[0:3] == b"\xff\xd8\xff":
        return SniffResult(True, "image/jpeg", "Valid JPEG")
    if b[0:4] == b"GIF8":
        return SniffResult(True, "image/gif", "Valid GIF")
    if b[0:4] == b"RIFF" and len(b) >= 12 and b[8:12] == b"WEBP":
        return SniffResult(True, "image/webp", "Valid WebP")
    if b[0:2] == b"BM":
        return SniffResult(True, "image/bmp", "Valid BMP")
    if len(b) >= 12 and b[4:8] == b"ftyp":
        brand = b[8:12].decode("latin-1", "replace")
        if brand in ("M4A ", "M4B ", "M4P "):
            return SniffResult(True, "audio/mp4", "Valid M4A audio (brand)")
        if _mp4_has_video_track(b):
            return SniffResult(True, "video/mp4", "Valid MP4 video (has video track)")
        return SniffResult(True, "audio/mp4", "Valid M4A audio (no video track)")
    if b[0:4] == b"\x1a\x45\xdf\xa3":
        return SniffResult(True, "video/webm", "Valid WebM")
    if b[0:3] == b"ID3" or (b[0] == 0xFF and b[1] in (0xFB, 0xFA, 0xF3, 0xF2)):
        return SniffResult(True, "audio/mpeg", "Valid MP3")
    if b[0:4] == b"OggS":
        return SniffResult(True, "audio/ogg", "Valid OGG")
    if b[0:4] == b"RIFF" and len(b) >= 12 and b[8:12] == b"WAVE":
        return SniffResult(True, "audio/wav", "Valid WAV")
    if b[0:4] == b"fLaC":
        return SniffResult(True, "audio/flac", "Valid FLAC")

    text_start = b[: min(100, len(b))].decode("utf-8", "replace")
    if "<?xml" in text_start or "<svg" in text_start:
        return SniffResult(True, "image/svg+xml", "Valid SVG")
    if "<!DOCTYPE" in text_start or "<html" in text_start or "<HTML" in text_start:
        return SniffResult(False, "text/html", "Content is HTML (likely error page)")

    if content_type and content_type.startswith(("image/", "audio/", "video/")):
        return SniffResult(True, content_type, "Unknown format, trusting content-type")

    return SniffResult(False, None, "Unknown or invalid media format")


def extension_for(media_type: str, url: str) -> str:
    """Port of the extension half of `saveMediaFromMemory`
    (30-media-localization-feature.js:990-1039): exact MIME match first, then
    a subtype-derived guess for audio/video, then the URL's own suffix, then
    `png` as the image default."""
    if media_type in MIME_TO_EXT:
        return MIME_TO_EXT[media_type]
    if media_type.startswith("audio/"):
        subtype = media_type.split("/", 1)[1].split(";")[0]
        return subtype.replace("x-", "") or "audio"
    if media_type.startswith("video/"):
        subtype = media_type.split("/", 1)[1].split(";")[0]
        return subtype.replace("x-", "") or "video"
    match = re.search(r"\.([a-zA-Z0-9]+)(?:\?|$)", url)
    if match:
        return match.group(1).lower()
    return "png"


def _is_animated(data: bytes, media_type: str) -> bool:
    """Whether an image the sniff says is gif/webp actually carries more than
    one frame. Only these two formats can be animated among the still types
    WebP normalization would otherwise touch (§4/§6: animated stays as-is)."""
    if media_type not in ("image/gif", "image/webp"):
        return False
    try:
        with Image.open(io.BytesIO(data)) as image:
            frames = 0
            for _ in ImageSequence.Iterator(image):
                frames += 1
                if frames > 1:
                    return True
    except (OSError, ValueError):
        return False
    return False


def normalize_to_webp(data: bytes, media_type: str) -> tuple[bytes, str]:
    """`(bytes, media_type)` to actually write for a sniffed still image --
    re-encoded to WebP unless it's animated, in which case it passes through
    unchanged. Anything not in `_WEBP_CONVERTIBLE_TYPES` also passes through
    (svg, audio, video)."""
    if media_type not in _WEBP_CONVERTIBLE_TYPES:
        return data, media_type
    if _is_animated(data, media_type):
        return data, media_type
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            has_alpha = "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if has_alpha else "RGB")
            buffer = io.BytesIO()
            image.save(buffer, "WEBP", quality=_WEBP_QUALITY, method=_WEBP_METHOD)
            return buffer.getvalue(), "image/webp"
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        logger.warning("media_writer: cannot normalize %s to WebP: %s", media_type, exc)
        return data, media_type


def classify_failure(*, blocked: bool, status: int | None, message: str) -> bool:
    """Whether a failed attempt is permanent (never worth retrying) -- port
    of `classifyFailure` (media-dedup.js:278-292), server-relevant subset.
    A guard rejection is always permanent: the URL's scheme or hostname
    doesn't change between attempts. An HTTP status is permanent iff it's in
    `PERMANENT_HTTP`. Everything else (network errors, sniff failures, size
    cap) is transient -- the bytes might just be temporarily unreachable or
    the CDN might be serving a temporary error page."""
    if blocked:
        return True
    if status is not None:
        return status in PERMANENT_HTTP
    return False


# --------------------------------------------------------------------------
# Local dedupe -- name index + content hash, both a scandir, never HTTP
# --------------------------------------------------------------------------


# Content digests, keyed by (path, size, mtime_ns) so a file that hasn't
# changed is never re-read. Media files here are write-once (`write_atomic`
# renames a fresh name into place), so the identity triple is enough -- an
# edit in place changes size or mtime and re-hashes. Bounded because the
# archive holds ~18k media files across ~3.9k galleries and a long-lived
# server would otherwise pin every one it ever touched.
_MAX_DIGEST_CACHE = 50_000
_digest_cache: dict[tuple[str, int, int], str] = {}


def _cached_digest(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _digest_cache.get(key)
    if hit is not None:
        return hit
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    if len(_digest_cache) >= _MAX_DIGEST_CACHE:
        _digest_cache.clear()
    _digest_cache[key] = digest
    return digest


@dataclass
class GalleryIndex:
    """One gallery folder's existing files, indexed two ways so an item can
    be skipped before a byte is fetched (name) or after (content hash).

    The name index is built eagerly -- it's a `scandir` and some string work.
    The hash index is **lazy**: it costs a full read of every file in the
    folder, and a run whose items all skip on the name index never needs it.
    That is the common case by far (a re-run over a card whose media is
    already downloaded), and for the browser-fetch door it used to be paid
    once *per item*: a 200-file / 59MB gallery re-read and re-hashed itself
    200 times over. Digests are memoized across builds by
    (path, size, mtime), so even a run that does save new files only hashes
    what actually changed.
    """

    by_key: dict[str, str] = field(default_factory=dict)  # media_key -> filename
    digest_of: dict[str, str] = field(default_factory=dict)  # filename -> sha256
    _dir: Path | None = None
    _names: list[str] = field(default_factory=list)  # media files, in scan order
    _name_set: set[str] = field(default_factory=set)  # same, for membership
    _by_hash: dict[str, str] | None = None  # sha256 -> filename, built on demand

    @classmethod
    def build(cls, gallery_dir: Path) -> "GalleryIndex":
        idx = cls(_dir=gallery_dir)
        try:
            entries = sorted(p for p in gallery_dir.iterdir() if p.is_file() and not p.name.startswith("."))
        except OSError:
            return idx
        strong_keys: set[str] = set()
        for path in entries:
            name = path.name
            if not MEDIA_EXT_RE.search(name):
                continue
            idx._names.append(name)
            idx._name_set.add(name)
            stripped = media_names.PREFIXED_NAME_RE.match(Path(name).stem)
            if stripped:
                key = media_names.media_key(stripped.group(1))
                if len(key) >= media_names.MIN_KEY_LENGTH:
                    idx.by_key[key] = name
                    strong_keys.add(key)
            else:
                key = media_names.media_key(Path(name).stem)
                if len(key) >= media_names.MIN_KEY_LENGTH and key not in strong_keys:
                    idx.by_key[key] = name
        return idx

    def _hashes(self) -> dict[str, str]:
        if self._by_hash is None:
            by_hash: dict[str, str] = {}
            base = self._dir
            for name in self._names:
                digest = self.digest_of.get(name)
                if digest is None and base is not None:
                    digest = _cached_digest(base / name)
                if digest is None:
                    continue
                by_hash.setdefault(digest, name)
                self.digest_of[name] = digest
            self._by_hash = by_hash
        return self._by_hash

    @property
    def by_hash(self) -> dict[str, str]:
        """The content-hash index, materialized on first access."""
        return self._hashes()

    def find_by_name(self, url: str, filename_hint: str | None, prefix: str) -> str | None:
        """A filename already on disk that this item's name keys would match,
        honoring the same prefix-priority rule as `prefixPriority`
        (media-dedup.js:140-145): never report a match that would need
        downgrading from a higher-priority prefix to `prefix`."""
        for key in media_names.keys_for_item(url, filename_hint):
            match = self.by_key.get(key)
            if not match:
                continue
            if match.startswith(prefix + "_"):
                return match
            if media_names.prefix_priority(match) >= media_names.PREFIX_PRIORITY.get(prefix, 0):
                return match
        return None

    def find_by_hash(self, digest: str) -> str | None:
        return self._hashes().get(digest)

    def has_file(self, file_name: str) -> bool:
        """Whether the folder still holds this exact filename -- what makes a
        manifest entry trustworthy without a `stat` per item."""
        return file_name in self._name_set

    def digest_for(self, file_name: str) -> str:
        """One file's digest, without materializing the whole hash index --
        for the name-match branch, which needs the digest of the single file
        it matched and nothing else."""
        known = self.digest_of.get(file_name)
        if known is not None:
            return known
        if self._dir is None:
            return ""
        digest = _cached_digest(self._dir / file_name)
        if digest is None:
            return ""
        self.digest_of[file_name] = digest
        return digest

    def note_saved(self, url: str, filename_hint: str | None, prefix: str, file_name: str, digest: str) -> None:
        for key in media_names.keys_for_item(url, filename_hint):
            self.by_key[key] = file_name
        if file_name not in self._name_set:
            self._names.append(file_name)
            self._name_set.add(file_name)
        if self._by_hash is not None:
            self._by_hash.setdefault(digest, file_name)
        self.digest_of[file_name] = digest


# --------------------------------------------------------------------------
# The fetch + guard
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DownloadOutcome:
    status: Literal["saved", "skipped", "error"]
    url: str
    file: str | None = None
    reason: str | None = None
    bytes: int | None = None
    permanent: bool | None = None


def manifest_hit(manifest: dict[str, Any], index_state: GalleryIndex, url: str) -> str | None:
    """The local file this exact URL already became in this gallery, if it is
    still on disk -- else None.

    The name index (step 2) can only skip a URL whose *filename* survives
    `media_names.media_key` with at least `MIN_KEY_LENGTH` characters. Whole
    hosts fail that: postimg serves galleries as `i.postimg.cc/<id>/1.webp`,
    `.../2.webp`, and a key of `"1"` is too short to index on, so every one of
    those URLs fell through to a real fetch on every re-run and was only then
    thrown away by the content-hash dedupe in `finish_item`. A 25-image
    postimg card therefore re-downloaded 25 images to save none of them, every
    single time -- minutes of wall clock for a no-op.

    The manifest already records exactly what we need to avoid that: `files`
    is keyed by source URL and names the file it became. Checking it costs a
    dict lookup against an index that is already built, and it is exact rather
    than heuristic -- no name derivation involved, so it works for any URL
    shape at all. It is deliberately narrower than the name index, though: it
    only fires for a URL *this* gallery has itself downloaded before, which is
    what makes "the file is still there" the only extra thing to verify.
    """
    entry = manifest.get("files", {}).get(url)
    if not isinstance(entry, dict):
        return None
    file_name = entry.get("file")
    if not isinstance(file_name, str) or not file_name:
        return None
    return file_name if index_state.has_file(file_name) else None


def size_of(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


# Shared with the browser's CORS passthrough (aspect/api/cors_proxy.py); it lived
# here first, and the name is kept so the call sites below read unchanged.
_preflight_dns = media_guard.preflight_dns


@contextlib.asynccontextmanager
async def _gate(semaphore: asyncio.Semaphore | None):
    """`async with` over an optional semaphore -- a plain no-op when a single
    item is downloaded outside any batch."""
    if semaphore is None:
        yield
        return
    async with semaphore:
        yield


async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[bytes | None, str | None, str | None]:
    """`(body, content_type, error)` -- exactly one of `body` or `error` is
    set. Streams with the size cap rather than buffering a potentially huge
    body first."""
    try:
        async with client.stream("GET", url, follow_redirects=True) as response:
            if response.status_code >= 400:
                return None, None, f"HTTP {response.status_code}"
            try:
                body = await media_guard.read_body_with_cap(response, media_guard.MAX_MEDIA_BYTES)
            except media_guard.MediaTooLargeError as exc:
                return None, None, str(exc)
            return body, response.headers.get("content-type"), None
    except httpx.HTTPError as exc:
        return None, None, str(exc)


def finish_item(
    gallery_dir: Path,
    folder_name: str,
    *,
    url: str,
    filename_hint: str | None,
    prefix: str,
    index: int,
    index_state: GalleryIndex,
    manifest: dict[str, Any],
    ledger: dict[str, Any],
    body: bytes,
    content_type: str | None,
    thumbnail_store: thumbs.ThumbnailStore,
) -> DownloadOutcome:
    """Steps 5-10 of the plan's §3 list, for bytes already in hand -- shared by
    `download_item` (bytes just fetched by this server) and the
    `POST .../media/bytes` route (bytes the *browser* fetched, e.g. MEGA's
    AES-CTR decrypt or Pixiv's session-proxied image -- see plan §6, "one
    writer, two entry doors")."""

    # 5. Sniff.
    sniff = sniff_media(body, content_type)
    if not sniff.valid:
        result = media_manifest.record_failure(ledger, url, permanent=False, status=None, message=sniff.reason)
        if result["dead"]:
            media_manifest.record_dead(manifest, url, sniff.reason, attempts=result["attempts"])
            return DownloadOutcome("skipped", url, reason=sniff.reason)
        return DownloadOutcome("error", url, reason=sniff.reason, permanent=False)

    # 5b. Image-only policy, for the URLs the suffix gate could not judge:
    # extensionless CDN links, `?download=1` redirects, an `.png` that is
    # really an mp4. Unlike the suffix gate this one is recorded permanently in
    # both ledgers, because the URL carries no hint -- without that, every run
    # would re-fetch the same 14MB mp3 in order to throw it away again.
    if not (sniff.media_type or "").startswith("image/"):
        reason = f"{sniff.media_type or 'unknown'} is not an image (audio/video not archived)"
        media_manifest.record_failure(ledger, url, permanent=True, status=None, message=reason)
        media_manifest.record_dead(manifest, url, reason)
        return DownloadOutcome("skipped", url, reason=reason, permanent=True)

    media_manifest.record_success(ledger, url)

    # 6. Normalize to WebP (stills only).
    written_bytes, written_type = normalize_to_webp(body, sniff.media_type or "")

    # 7. Content hash dedupe against the folder.
    digest = hashlib.sha256(written_bytes).hexdigest()
    existing_by_hash = index_state.find_by_hash(digest)
    if existing_by_hash:
        media_manifest.record_saved(manifest, url, existing_by_hash, digest, size=len(written_bytes))
        return DownloadOutcome("skipped", url, file=existing_by_hash, reason="already have this content")

    # 8. Write, atomically.
    ext = extension_for(written_type, url)
    sanitized_name = (media_names.media_key(filename_hint) if filename_hint else "") or (
        media_names.extract_sanitized_url_name(url) or "media"
    )
    file_name = media_names.local_filename(prefix, index, sanitized_name, ext)
    target = gallery_dir / file_name
    try:
        edit.write_atomic(target, written_bytes)
    except OSError as exc:
        return DownloadOutcome("error", url, reason=f"cannot write {file_name}: {exc}", permanent=False)

    # 9. Thumbnail, immediately.
    thumbnail_store.generate_gallery(target, folder_name, file_name)

    # 10. Record.
    index_state.note_saved(url, filename_hint, prefix, file_name, digest)
    media_manifest.record_saved(manifest, url, file_name, digest, size=len(written_bytes))
    return DownloadOutcome("saved", url, file=file_name, bytes=len(written_bytes))


async def download_item(
    client: httpx.AsyncClient,
    gallery_dir: Path,
    folder_name: str,
    *,
    url: str,
    filename_hint: str | None,
    prefix: str,
    index: int,
    index_state: GalleryIndex,
    manifest: dict[str, Any],
    ledger: dict[str, Any],
    thumbnail_store: thumbs.ThumbnailStore,
    fetch_gate: "asyncio.Semaphore | None" = None,
    retry_dead: bool = False,
) -> DownloadOutcome:
    """Everything the plan's §3 numbered list says for one URL, end to end.

    `retry_dead` (Rescan everything / Rescan broken) fetches a URL that either
    dead ledger already marks gone. Nothing is cleared first: a recovered URL
    leaves both ledgers through the ordinary success path, and one still
    failing keeps its manifest `dead` entry and its attempt count, so it is
    dead again the moment the run ends rather than re-arming Localize all.
    """

    # 0. Manifest hit -- this gallery already downloaded this exact URL and
    # still has the file. Nothing left to decide, so this runs ahead of even
    # the guard: no fetch happens either way.
    already = manifest_hit(manifest, index_state, url)
    if already:
        return DownloadOutcome("skipped", url, file=already, reason="already downloaded")

    # 0b. Image-only policy (see `UNSUPPORTED_EXT_RE`). Ahead of the guard and
    # the ledgers because it is the cheapest check there is and the one most
    # likely to fire on a creator's embedded soundtrack: a 14MB mp3 refused
    # here costs nothing, whereas the sniff backstop has to fetch it first.
    unsupported = unsupported_url_reason(url)
    if unsupported:
        return DownloadOutcome("skipped", url, reason=unsupported, permanent=True)

    # 3. Per-gallery dead ledger -- already known gone for this character.
    dead_here = manifest.get("dead", {}).get(url)
    if dead_here and not retry_dead:
        return DownloadOutcome("skipped", url, reason=dead_here.get("reason", "known dead"))

    is_mega = media_mega.is_mega_url(url)

    # 1. Guard, before anything touches the network. A `mega://` reference
    # isn't a fetchable address -- it's a token this server resolves to a
    # real MEGA storage URL just before fetching, and the guard runs against
    # *that* URL inside `media_mega.fetch_and_decrypt` instead.
    if not is_mega:
        safety = media_guard.is_url_safe_for_download(url)
        if not safety.ok:
            media_manifest.record_failure(ledger, url, permanent=True, status=None, message=safety.reason)
            media_manifest.record_dead(manifest, url, safety.reason)
            return DownloadOutcome("skipped", url, reason=safety.reason, permanent=True)

    # Cross-character dead ledger -- a URL confirmed gone on another card.
    if not retry_dead and media_manifest.is_dead(ledger, url):
        reason = media_manifest.dead_reason(ledger, url)
        media_manifest.record_dead(manifest, url, reason)
        return DownloadOutcome("skipped", url, reason=reason)

    # 2. Name index -- already have an equivalent file, before any bytes move.
    existing_name = index_state.find_by_name(url, filename_hint, prefix)
    if existing_name:
        # Record the mapping even though nothing was fetched: this URL *is*
        # satisfied by that file, and the hash-dedupe branch in `finish_item`
        # already records its equivalent. Without this a card whose media was
        # all downloaded under SillyTavern reports `files: 0` from
        # `/media/status` forever -- 18k inherited files invisible to it.
        # Costs one file's digest (memoized), not the folder's.
        media_manifest.record_saved(
            manifest,
            url,
            existing_name,
            index_state.digest_for(existing_name),
            size=size_of(gallery_dir / existing_name),
        )
        return DownloadOutcome("skipped", url, file=existing_name, reason="filename match")

    # Everything above this line is local and instant; everything below talks
    # to the network, so only this half is gated when a batch runs items
    # concurrently. A skip must never queue behind someone else's slow fetch.
    async with _gate(fetch_gate):
        if not is_mega:
            # `getaddrinfo` is a blocking call -- on the event loop it would
            # stall every other item in the batch, so it goes to a thread.
            dns_reason = await asyncio.to_thread(_preflight_dns, url)
            if dns_reason:
                media_manifest.record_failure(ledger, url, permanent=True, status=None, message=dns_reason)
                media_manifest.record_dead(manifest, url, dns_reason)
                return DownloadOutcome("skipped", url, reason=dns_reason, permanent=True)

        # 4. Fetch.
        if is_mega:
            body, content_type, error = await media_mega.fetch_and_decrypt(client, url)
        else:
            body, content_type, error = await _fetch(client, url)

    if body is None:
        status = int(error.split(" ", 1)[1]) if error and error.startswith("HTTP ") else None
        permanent = classify_failure(blocked=False, status=status, message=error or "")
        result = media_manifest.record_failure(
            ledger, url, permanent=permanent, status=status, message=error or "download failed"
        )
        if result["dead"]:
            media_manifest.record_dead(manifest, url, error or "download failed", attempts=result["attempts"])
            return DownloadOutcome("skipped", url, reason=error, permanent=permanent)
        return DownloadOutcome("error", url, reason=error, permanent=False)

    return finish_item(
        gallery_dir,
        folder_name,
        url=url,
        filename_hint=filename_hint,
        prefix=prefix,
        index=index,
        index_state=index_state,
        manifest=manifest,
        ledger=ledger,
        body=body,
        content_type=content_type,
        thumbnail_store=thumbnail_store,
    )


# --------------------------------------------------------------------------
# The batch -- both entry points' per-item loop, once
# --------------------------------------------------------------------------


_BATCH_DONE = object()


async def download_batch(
    client: httpx.AsyncClient,
    gallery_dir: Path,
    folder_name: str,
    *,
    items: Sequence[dict[str, Any]],
    prefix: str,
    start_index: int,
    index_state: GalleryIndex,
    manifest: dict[str, Any],
    ledger: dict[str, Any],
    thumbnail_store: thumbs.ThumbnailStore,
    should_cancel: Callable[[], bool] | None = None,
    concurrency: int | None = None,
    per_host: int | None = None,
) -> AsyncIterator[DownloadOutcome]:
    """`download_item` over a whole list, yielding each outcome as it finishes
    -- the loop `POST /characters/{id}/media` and the background job runner
    both used to keep their own copy of.

    Items run `concurrency` at a time, with no more than `per_host` of them
    against any one hostname (see `settings.media_concurrency`). Yield order is
    completion order, not input order; both callers only accumulate counters
    and append log lines, so nothing depends on the original sequence. The
    file index each item is named from still comes from its *input* position,
    so filenames are unaffected by how the run interleaves.

    An item may carry its own `"prefix"`, overriding this call's `prefix` for
    that one item -- `_discovered_items` (aspect/api/v1/media.py) uses this so
    a `discover=true` job can mix embedded, lorebook, and gallery-extractor
    items (each its own dedupe-priority class, see media_names.PREFIX_PRIORITY)
    in one batch without misclassifying any of them. `"retry_dead"` is per
    item for the same reason: a rescan mixes retried dead links with ordinary
    items.

    Concurrency is safe here without a lock precisely because only the fetch
    awaits: `finish_item` -- hash dedupe, write, thumbnail, manifest record --
    is synchronous from start to finish, so it cannot interleave with another
    item on the single-threaded event loop. Two items carrying identical bytes
    can both be fetched, but the second still finds the first's digest and
    skips rather than writing a duplicate.
    """
    if not items:
        return

    limit = max(1, concurrency if concurrency is not None else settings.media_concurrency)
    host_limit = max(1, per_host if per_host is not None else settings.media_per_host_concurrency)

    queue: asyncio.Queue = asyncio.Queue()
    host_gates: dict[str, asyncio.Semaphore] = {}
    next_pos = 0
    stop = False
    failure: BaseException | None = None

    async def worker() -> None:
        nonlocal next_pos, stop
        while not stop:
            if should_cancel is not None and should_cancel():
                stop = True
                return
            if next_pos >= len(items):
                return
            position = next_pos
            next_pos += 1
            item = items[position]
            url = item["url"]
            host = (urlsplit(url).hostname or "").lower()
            gate = host_gates.get(host)
            if gate is None:
                gate = asyncio.Semaphore(host_limit)
                host_gates[host] = gate
            outcome = await download_item(
                client,
                gallery_dir,
                folder_name,
                url=url,
                filename_hint=item.get("filename"),
                prefix=item.get("prefix") or prefix,
                index=start_index + position,
                index_state=index_state,
                manifest=manifest,
                ledger=ledger,
                thumbnail_store=thumbnail_store,
                fetch_gate=gate,
                retry_dead=bool(item.get("retry_dead")),
            )
            await queue.put(outcome)

    workers = [asyncio.create_task(worker()) for _ in range(min(limit, len(items)))]

    async def supervise() -> None:
        nonlocal failure, stop
        try:
            await asyncio.gather(*workers)
        except BaseException as exc:  # one item's crash must stop the rest
            failure = exc
            stop = True
        finally:
            await queue.put(_BATCH_DONE)

    supervisor = asyncio.create_task(supervise())
    try:
        while True:
            outcome = await queue.get()
            if outcome is _BATCH_DONE:
                break
            yield outcome
    finally:
        # A caller that stops iterating early (the NDJSON client hung up)
        # must not leave downloads running against a manifest nobody saves.
        stop = True
        for task in workers:
            task.cancel()
        supervisor.cancel()
        await asyncio.gather(*workers, supervisor, return_exceptions=True)

    if failure is not None:
        raise failure
