"""`/api/v1` media-folder routes: the image folders that hang off a card.

Two resources share one implementation here -- galleries (`data/galleries/`)
and expressions (`data/expressions/`), docs/FORKS_AND_EXTRAS_PLAN.md §2. Both
are a folder named `<Name>_<gallery_id>` and resolved by that id suffix, never
by the name -- a card rename moves nothing on disk. See `aspect.cards.gallery`.

`register_folder_routes` is the ~200 lines of list/get/thumb/upload/delete
both resources need identically; `GALLERY` and `EXPRESSION` at the bottom are
the only two places that say which is which. A change to one resource's
behaviour that should not apply to the other does not belong in this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile

from aspect.api.schemas import (
    GalleryBulkDeleteIn,
    GalleryBulkDeleteOut,
    GalleryFileOut,
    GalleryFilesOut,
    GalleryFileWrittenOut,
    GalleryFolderOut,
    MediaUploadOut,
    MediaUploadSkippedOut,
    ThumbsPrunedOut,
)
from aspect.api.v1 import _shared
from aspect.archive import catalog, thumbs
from aspect.cards import edit, gallery
from aspect.config import settings
from aspect.media import expressions, uploads

# Which element a file is for, by extension. Sniffing the bytes would be more
# honest but needs an open per file, and this list exists so a folder of 400
# files can be described in one scandir.
_GALLERY_KINDS: dict[str, str] = {
    **{ext: "image" for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff")},
    **{ext: "video" for ext in (".mp4", ".webm", ".mov", ".m4v", ".mkv")},
    **{ext: "audio" for ext in (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac")},
}
# What `gallery(...)` can actually render -- see aspect/archive/thumbs.py.
_THUMBABLE = thumbs.THUMBABLE_EXTS


@dataclass(frozen=True, slots=True)
class FolderKind:
    """One media-folder resource: where its files live on disk, and which of
    `ThumbnailStore`'s parallel method sets renders their thumbnails.

    `root` and the thumb accessors are callables (bound methods / lambdas over
    `_shared.thumbnail_store`) rather than a `Path` or a store captured once,
    so they read `settings`/`_shared.thumbnail_store` fresh on every call --
    both are process-wide singletons a test repoints per run (see
    `tests/conftest.py`'s `archive_dirs`), and capturing either at import time
    would freeze the binding to whatever was current then.
    """

    segment: str  # URL segment: "galleries" | "expressions"
    singular: str  # for messages and summaries: "gallery" | "expression"
    root: Callable[[], Path]
    thumb: Callable[[Path, str, str, int], thumbs.ThumbFile | None]
    thumb_forget: Callable[[str, str | None], int]
    thumb_prune: Callable[[str, set[str]], int]
    # Why an otherwise-valid image may not live in *this* resource, or None to
    # take it. The only behavioural difference between the two mounts
    # (docs/FORKS_AND_EXTRAS_PLAN.md §9): a gallery takes any image, an
    # expressions folder takes only one whose filename parses to one of ST's
    # 28 labels. Asked after the WebP rename, which cannot change the label.
    reject: Callable[[str], str | None] = lambda name: None


def _store(kind: FolderKind, directory: Path, filename: str, data: bytes) -> GalleryFileWrittenOut:
    """Convert and write one uploaded file into `directory`, creating the
    folder only once there is something to put in it -- a refused upload must
    not leave an empty folder behind, which is the difference between "this
    character has no sprites" and "this character has a sprites folder with
    nothing in it" everywhere downstream.

    Raises `uploads.RejectedUpload`; the two callers differ only in whether
    that is a 422 or one line of a report.
    """
    prepared = uploads.prepare(filename, data)
    refusal = kind.reject(prepared.name)
    if refusal:
        raise uploads.RejectedUpload(refusal)

    directory.mkdir(parents=True, exist_ok=True)
    target = _shared.safe_child(directory, prepared.name)
    replaced = target.is_file()
    try:
        edit.write_atomic(target, prepared.data)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot write {prepared.name}: {exc}") from exc

    # An overwrite reuses the name, and thumbs are keyed on it.
    kind.thumb_forget(directory.name, prepared.name)
    quoted_folder = quote(directory.name, safe="")
    quoted_name = quote(prepared.name, safe="")
    return GalleryFileWrittenOut(
        folder=directory.name,
        name=prepared.name,
        size=len(prepared.data),
        path=f"user/images/{directory.name}/{prepared.name}",
        # Must match what `list_folder_files` builds -- the `/files/` segment
        # is part of the route, and dropping it yields a URL that 404s.
        url=f"{_shared.PREFIX}/{kind.segment}/{quoted_folder}/files/{quoted_name}",
        replaced=replaced,
    )


def register_folder_routes(kind: FolderKind, router: APIRouter) -> None:
    """Register `kind`'s list/get/thumb/upload/delete routes onto `router`."""

    @router.get(
        f"/{kind.segment}", response_model=list[GalleryFolderOut], summary=f"{kind.singular.capitalize()} folders on disk"
    )
    def list_folders() -> list[GalleryFolderOut]:
        """Every folder in the directory, each paired with the cards that
        claim it.

        Claimed by gallery *id*, not by folder name: the name is derived from
        the card's current name and drifts the moment a card is renamed,
        whereas the id is the actual link. So `card_ids: []` now means a
        folder no card carries the id for -- genuinely orphaned -- rather
        than merely misnamed. A folder can have more than one claimant: a
        fork deliberately shares its parent's `gallery_id`
        (docs/FORKS_AND_EXTRAS_PLAN.md §3), so two cards pointing at the same
        folder is normal, not a collision to resolve.
        """
        idx = _shared.index()
        claimed: dict[str, list[str]] = {}
        for r in idx.cards():
            if r.gallery_id:
                claimed.setdefault(r.gallery_id, []).append(r.filename)
        try:
            with os.scandir(kind.root()) as entries:
                folders = sorted(e.name for e in entries if e.is_dir() and not e.name.startswith("."))
        except OSError:
            return []
        return [GalleryFolderOut(folder=f, card_ids=claimed.get(gallery.id_of(f), [])) for f in folders]

    @router.get(
        f"/{kind.segment}/{{folder}}", response_model=GalleryFilesOut, summary=f"Files in one {kind.singular}"
    )
    def list_folder_files(folder: str) -> GalleryFilesOut:
        """A folder's contents. 404 when the folder is not there -- unlike
        SillyTavern's `/api/images/list`, which creates the directory as a
        side effect of being asked about it, so the frontend could never tell
        an empty folder from a missing one."""
        path = _shared.media_dir(kind.root(), folder)
        try:
            with os.scandir(path) as entries:
                files = sorted(
                    (e for e in entries if e.is_file() and not e.name.startswith(".")),
                    key=lambda e: e.name.casefold(),
                )
        except OSError as exc:
            raise HTTPException(status_code=404, detail=f"no {kind.singular} folder {folder!r}") from exc

        quoted_folder = quote(folder, safe="")
        items: list[GalleryFileOut] = []
        total_bytes = 0
        for entry in files:
            st = entry.stat()
            total_bytes += st.st_size
            suffix = Path(entry.name).suffix.casefold()
            quoted_name = quote(entry.name, safe="")
            base = f"{_shared.PREFIX}/{kind.segment}/{quoted_folder}/files/{quoted_name}"
            items.append(
                GalleryFileOut(
                    name=entry.name,
                    kind=_GALLERY_KINDS.get(suffix, "other"),
                    size=st.st_size,
                    modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                    url=base,
                    thumb_url=f"{base}/thumb" if suffix in _THUMBABLE else None,
                )
            )
        return GalleryFilesOut(folder=folder, total=len(items), bytes=total_bytes, items=items)

    @router.get(f"/{kind.segment}/{{folder}}/files/{{filename}}", summary=f"One {kind.singular} file")
    def get_file(folder: str, filename: str, request: Request) -> Response:
        path = _shared.safe_child(_shared.media_dir(kind.root(), folder), filename)
        return _shared.serve_file(
            path,
            media_type=thumbs.media_type_of(path),
            request=request,
            cache_control=_shared.THUMB_CACHE_CONTROL,
        )

    @router.get(f"/{kind.segment}/{{folder}}/files/{{filename}}/thumb", summary=f"{kind.singular.capitalize()} file thumbnail")
    def get_thumb(
        folder: str,
        filename: str,
        request: Request,
        size: int = Query(thumbs.GALLERY_THUMB_SIZE, ge=32, le=1024),
    ) -> Response:
        """A fitted derivative of one file, generated on a cache miss.

        Unlike the avatar thumb this does *not* fall back to the original on
        failure: a page can ask for a hundred of these at once, and answering
        a failure with a multi-megabyte source is how one unrenderable file
        takes the page down with it.
        """
        directory = _shared.media_dir(kind.root(), folder)
        source = _shared.safe_child(directory, filename)
        if not source.is_file():
            raise HTTPException(status_code=404, detail=f"no file {filename!r} in {kind.singular} {folder!r}")
        thumb = kind.thumb(source, directory.name, filename, size)
        if thumb is None:
            raise HTTPException(status_code=415, detail=f"{filename!r} cannot be thumbnailed")
        return _shared.serve_file(
            thumb.path,
            media_type=thumb.media_type,
            request=request,
            cache_control=_shared.THUMB_CACHE_CONTROL,
        )

    @router.post(
        f"/{kind.segment}/{{folder}}/files",
        response_model=GalleryFileWrittenOut,
        status_code=201,
        summary=f"Add a file to a {kind.singular}",
    )
    def upload_file(
        folder: str,
        file: UploadFile = File(description="The file to store."),
    ) -> GalleryFileWrittenOut:
        """Store one image in a folder, creating it if it is not there yet.

        Multipart rather than base64-in-JSON: these are multi-megabyte
        binaries, and base64 inflates them by a third for the whole round
        trip. The adapter converts on the client side, where the frontend's
        encoded copy already exists.

        Unlike the bulk route below, a single upload that is refused is a 422:
        there is nothing partial about one file, and the caller asked for this
        one specifically.
        """
        directory = _shared.media_dir(kind.root(), folder)
        try:
            written = _store(kind, directory, file.filename or "", file.file.read())
        except uploads.RejectedUpload as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return written

    @router.post(
        f"/{kind.segment}/{{folder}}/zip",
        response_model=MediaUploadOut,
        status_code=201,
        summary=f"Import a zip of files into a {kind.singular}",
    )
    def upload_zip(
        folder: str,
        file: UploadFile = File(description="A zip whose image entries are unpacked into the folder."),
    ) -> MediaUploadOut:
        """Unpack a zip into a folder, flattened to basenames.

        The flattening is ST's (`getImageBuffers` keeps only
        `path.parse(name).base`), which means both shapes
        `aspect.media.expressions` exports load here: the flat
        single-character zip and the `<folder>/<file>` multi-character one.
        The latter collapses every character into this one folder, exactly as
        ST's own importer would -- documented in §2 as the reason the two
        exports are labelled differently in the UI.

        Reports per file rather than failing whole: a 90-sprite pack with two
        strays should write 88 and name the two.
        """
        payload = file.file.read()
        try:
            entries = uploads.zip_entries(payload)
        except uploads.RejectedUpload as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        directory = _shared.media_dir(kind.root(), folder)
        written: list[GalleryFileWrittenOut] = []
        skipped: list[MediaUploadSkippedOut] = []
        for name, data in entries:
            try:
                written.append(_store(kind, directory, name, data))
            except uploads.RejectedUpload as exc:
                skipped.append(MediaUploadSkippedOut(name=name, reason=str(exc)))
        return MediaUploadOut(folder=directory.name, written=written, skipped=skipped)

    @router.delete(f"/{kind.segment}/{{folder}}/files/{{filename}}", status_code=204, summary=f"Bin a {kind.singular} file")
    def delete_file(folder: str, filename: str) -> Response:
        """Move one file to the bin. Binned rather than unlinked for the same
        reason cards are -- see `aspect.cards.edit`."""
        directory = _shared.media_dir(kind.root(), folder)
        path = _shared.safe_child(directory, filename)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no file {filename!r} in {kind.singular} {folder!r}")
        try:
            edit.to_trash(path)
        except edit.WriteError as exc:
            raise _shared.write_error(exc) from exc
        kind.thumb_forget(directory.name, filename)
        return Response(status_code=204)

    @router.post(
        f"/{kind.segment}/{{folder}}/files/bulk-delete",
        response_model=GalleryBulkDeleteOut,
        summary=f"Bin many {kind.singular} files at once",
    )
    def bulk_delete_files(folder: str, body: GalleryBulkDeleteIn) -> GalleryBulkDeleteOut:
        """The gallery pane's batch-select delete -- bin a selection of files
        in one pass instead of N single-file requests from the client.

        Deliberately leaves the folder's `.media.json` manifest untouched,
        same as `delete_file` above: a binned file still has a manifest entry
        claiming its source URL as downloaded, so a plain localize run (which
        skips any card its last run completed without checking the manifest
        entries against disk) does not go re-fetch it. Only a full rescan --
        `mode="everything"` -- re-derives the URL list from the card's own
        text and finds the gap.
        """
        directory = _shared.media_dir(kind.root(), folder)
        deleted: list[str] = []
        failed: dict[str, str] = {}
        for name in body.names:
            try:
                path = _shared.safe_child(directory, name)
            except HTTPException as exc:
                failed[name] = str(exc.detail)
                continue
            if not path.is_file():
                failed[name] = f"no file {name!r} in {kind.singular} {folder!r}"
                continue
            try:
                edit.to_trash(path)
            except edit.WriteError as exc:
                failed[name] = str(exc)
                continue
            kind.thumb_forget(directory.name, name)
            deleted.append(name)
        return GalleryBulkDeleteOut(deleted=deleted, failed=failed)

    @router.post(
        f"/{kind.segment}/{{folder}}/thumbs/prune", response_model=ThumbsPrunedOut, summary=f"Drop orphaned {kind.singular} thumbs"
    )
    def prune_thumbs(folder: str) -> ThumbsPrunedOut:
        """Thumbs are generated at write time, so the only orphans left are
        ones whose source file left by a route other than `DELETE
        .../files/{filename}` (which already forgets its own thumb)."""
        directory = _shared.media_dir(kind.root(), folder)
        live = {entry.name for entry in os.scandir(directory) if entry.is_file()} if directory.is_dir() else set()
        removed = kind.thumb_prune(directory.name, live)
        return ThumbsPrunedOut(folder=directory.name, removed=removed)


def _resolve_expressions_folder(record: catalog.CardSummary) -> Path | None:
    """The directory `record`'s expressions live in, or None when there is
    none on disk. Resolves by `gallery_id`, exactly like `_gallery_out` --
    galleries and expressions share the key on purpose (§2)."""
    if not record.gallery_id:
        return None
    wanted = gallery.folder_name(record.name, record.gallery_id)
    folder = gallery.resolve_folder(settings.expressions_dir, wanted) or wanted
    directory = settings.expressions_dir / folder
    return directory if directory.is_dir() else None


def _register_bulk_expressions_export(router: APIRouter) -> None:
    """`GET /expressions/export.zip` -- several characters' expressions in one
    zip, each under its own on-disk folder name (see
    `aspect.media.expressions.zip_many`).

    Registered before the generic `/{folder}` routes below it (see
    `register_folder_routes`) are added to this same router, so
    `/expressions/export.zip` is matched as this literal path rather than as
    `folder="export.zip"` -- the same ordering trap
    `characters.get_have_fragments` documents for `/characters/{card_id}`.

    No UI calls this yet: nothing in the archive client currently offers a
    multi-character selection to export, so this is a primitive without a
    button, the same as `fork_of` (docs/FORKS_AND_EXTRAS_PLAN.md §3) -- built
    now because a future bulk-select UI needs no server change to use it.
    """

    @router.get("/expressions/export.zip", summary="Download several characters' expressions as one zip")
    def export_expressions_zip(
        ids: list[str] = Query(alias="id", description="Repeatable; which characters to include."),
    ) -> Response:
        if not ids:
            raise HTTPException(status_code=422, detail="at least one id is required")
        idx = _shared.index()
        folders: list[tuple[str, Path]] = []
        for card_id in ids:
            record = _shared.require(idx, card_id)
            directory = _resolve_expressions_folder(record)
            if directory is not None:
                folders.append((directory.name, directory))
        if not folders:
            raise HTTPException(status_code=404, detail="none of the requested characters have an expressions folder")
        data = expressions.zip_many(folders)
        headers = {"Content-Disposition": _shared.content_disposition("expressions.zip")}
        return Response(content=data, media_type="application/zip", headers=headers)


GALLERY = FolderKind(
    segment="galleries",
    singular="gallery",
    root=lambda: settings.galleries_dir,
    thumb=lambda source, folder, filename, size: _shared.thumbnail_store.gallery(source, folder, filename, size),
    thumb_forget=lambda folder, filename: _shared.thumbnail_store.forget_gallery(folder, filename),
    thumb_prune=lambda folder, live: _shared.thumbnail_store.prune_gallery(folder, live),
)

EXPRESSION = FolderKind(
    segment="expressions",
    singular="expression",
    root=lambda: settings.expressions_dir,
    thumb=lambda source, folder, filename, size: _shared.thumbnail_store.expression(source, folder, filename, size),
    thumb_forget=lambda folder, filename: _shared.thumbnail_store.forget_expression(folder, filename),
    thumb_prune=lambda folder, live: _shared.thumbnail_store.prune_expression(folder, live),
    reject=expressions.rejection_reason,
)

router = APIRouter()
register_folder_routes(GALLERY, router)

expressions_router = APIRouter()
_register_bulk_expressions_export(expressions_router)
register_folder_routes(EXPRESSION, expressions_router)
