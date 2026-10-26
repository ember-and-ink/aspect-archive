"""State and helpers shared by more than one `/api/v1` route module.

Import the module, never the names:

    from aspect.api.v1 import _shared
    _shared.thumbnail_store.get(...)

`thumbnail_store` and `job_store` are process-wide singletons that tests repoint
and the server's startup hook binds to its event loop. A
`from ._shared import thumbnail_store` freezes the binding at import time, so a
test that repoints the store would leave this module still writing into the
developer's real archive -- a failure this suite has actually had (see the note
on `archive_dirs` in tests/conftest.py).
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse

from aspect.archive import catalog, thumbs
from aspect.cards import edit, gallery
from aspect.config import settings
from aspect.media import jobs as media_jobs

logger = logging.getLogger("aspect.api")

# The prefix every route in this package is mounted under, and the base the card
# and gallery URLs in responses are built from. A constant rather than a read off
# `router.prefix`: the routers are per-module now and carry no prefix of their
# own -- it is applied once, on the parent in `__init__`.
PREFIX = "/api/v1"

thumbnail_store = thumbs.ThumbnailStore()

# 3C-2 -- the job runner (docs/PHASE_3C_PLAN.md §7). Bound to the running
# event loop by `aspect.server`'s startup hook; see aspect/media/jobs.py.
job_store = media_jobs.JobStore(thumbnail_store)

# Thumbs are content-addressed by (mtime, size) via their ETag, so a long
# max-age costs nothing: a regenerated thumb changes its ETag and the
# revalidation picks it up.
THUMB_CACHE_CONTROL = "public, max-age=86400"


def index() -> catalog.ArchiveIndex:
    """The archive index, as it stands.

    Deliberately no refresh here. It used to stat-sweep the archive per request
    (behind a 2 s debounce) so outside writes appeared live, but on the Unraid
    share that sweep is ~0.5 s and nothing outside this process writes the
    archive in practice. The index is built at startup, refreshed by every write
    route, and rescanned on demand by `POST /refresh` -- see `aspect.archive.catalog`.
    """
    return catalog.index()


def require(idx: catalog.ArchiveIndex, card_id: str) -> catalog.CardSummary:
    record = idx.get(card_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no card named {card_id!r} in the archive")
    return record


def safe_child(root: Path, *parts: str) -> Path:
    """A path under `root`, or a 400.

    Gallery folder and file names arrive from the client -- they are how the
    frontend addresses images -- so every one of them is a path traversal until
    proven otherwise. Rejecting separators and `..` up front is not enough on its
    own (a symlink inside the galleries directory would still escape), so the
    resolved result is checked against the resolved root as well.
    """
    for part in parts:
        if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
            raise HTTPException(status_code=400, detail=f"illegal path component {part!r}")
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise HTTPException(status_code=400, detail="path escapes the gallery root")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"cannot resolve path: {exc}") from exc
    return candidate


def media_dir(root: Path, folder: str, *, create: bool = False) -> Path:
    """The directory a client's folder name refers to, under `root` -- either
    `settings.galleries_dir` or `settings.expressions_dir`, which resolve a
    folder name the same way (see `aspect.cards.gallery`).

    Validation first (the name is a path traversal until proven otherwise), then
    resolution by gallery id, so a card renamed after its images were downloaded
    still finds them. `create` is for uploads, which are allowed to bring a
    folder into existence; every other caller wants the miss.
    """
    checked = safe_child(root, folder)
    resolved = gallery.resolve_folder(root, folder)
    if resolved is not None:
        return root / resolved
    if create:
        checked.mkdir(parents=True, exist_ok=True)
    return checked


def gallery_dir(folder: str, *, create: bool = False) -> Path:
    return media_dir(settings.galleries_dir, folder, create=create)


def expression_dir(folder: str, *, create: bool = False) -> Path:
    return media_dir(settings.expressions_dir, folder, create=create)


def gallery_dir_for_card(idx: catalog.ArchiveIndex, record: catalog.CardSummary) -> tuple[str, Path]:
    """The `(folder name, directory)` a card's own gallery resolves to,
    creating both the id and the folder if this is the card's first media
    write. Resolving from `gallery_id` rather than a client-supplied folder
    name is what the media-download route needs that `gallery_dir` doesn't:
    docs/PHASE_3C_PLAN.md §3 -- "a renamed card can't miss its own gallery."
    """
    gallery_id = record.gallery_id
    if not gallery_id:
        path = idx.root / record.filename
        _outer, data = edit.read_card(path)
        gallery_id = gallery.ensure_id(data)
        if gallery_id:
            edit.patch_card(path, data)
    wanted = gallery.folder_name(record.name, gallery_id)
    resolved = gallery.resolve_folder(settings.galleries_dir, wanted) or wanted
    directory = settings.galleries_dir / resolved
    directory.mkdir(parents=True, exist_ok=True)
    return resolved, directory


def write_error(exc: edit.WriteError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def content_disposition(name: str) -> str:
    """RFC 6266 attachment header for `name`: an ASCII fallback plus a UTF-8
    form, because card and character names are full of curly apostrophes and
    em dashes that a bare `filename=` mangles."""
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'


def serve_file(
    path: Path,
    *,
    media_type: str,
    request: Request,
    download_as: str | None = None,
    cache_control: str | None = None,
) -> Response:
    """Send a file with a conditional-GET short circuit.

    A browse grid asks for hundreds of thumbnails per scroll and re-asks on every
    navigation, so answering the repeats with a 304 and no body is the difference
    between a warm grid costing kilobytes and costing tens of megabytes. The
    validator is (mtime_ns, size) -- the same pair the index invalidates on, so a
    card and its thumb can never disagree about whether they changed.
    """
    try:
        st = path.stat()
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"{path.name} is not on disk") from exc

    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    headers: dict[str, str] = {"ETag": etag}
    if cache_control:
        headers["Cache-Control"] = cache_control
    if download_as:
        headers["Content-Disposition"] = content_disposition(download_as)

    if etag in [t.strip() for t in (request.headers.get("if-none-match") or "").split(",")]:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type=media_type, headers=headers)
