"""`/api/v1` media routes: fetching a card's remote images and video.

The browser discovers the URLs; the server does the fetching (Phase 3C, see
docs/PHASE_3C_PLAN.md). One-shot downloads stream progress back as NDJSON;
longer runs go through the background job store in `_shared`.
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from aspect.api.schemas import (
    MediaBytesOut,
    MediaDedupeOut,
    MediaDownloadIn,
    MediaHaveIn,
    MediaHaveOut,
    MediaItemIn,
    MediaJobEventOut,
    MediaJobOut,
    MediaJobStatusOut,
    MediaJobSubmitIn,
    MediaManifestOut,
    MediaScanOut,
    MediaScanSourceOut,
    MediaStatusEntryOut,
    MediaStatusOut,
)
from aspect.api.v1 import _shared
from aspect.cards import gallery, pngtools
from aspect.config import settings
from aspect.media import (
    dedupe as media_dedupe,
    discovery as media_discovery,
    extractors as media_extractors,
    jobs as media_jobs,
    manifest as media_manifest,
    status as media_status,
    writer as media_writer,
)
from aspect.runtime import net
from aspect.sources import chub as chub_source

router = APIRouter()

logger = logging.getLogger("aspect.api.media")

def _job_status_out(job: media_jobs.MediaJob, *, after: int = 0, include_events: bool = True) -> MediaJobStatusOut:
    events = [MediaJobEventOut(**e) for e in job.events_after(after)] if include_events else []
    return MediaJobStatusOut(
        job_id=job.id,
        card_id=job.card_id,
        phase=job.phase,
        prefix=job.prefix,
        state=job.state,
        total=job.total,
        done=job.done,
        saved=job.saved,
        skipped=job.skipped,
        errors=job.errors,
        error=job.error_message,
        events=events,
        # A monotonic sequence number, not an index into `events` -- a long
        # batch trims its buffer, and an index would then point at the wrong
        # event (`MediaJob.record_event`).
        next_cursor=job.events_seen,
        scope=job.scope,
        unit="cards" if job.scope == "all" else "items",
        cards_total=job.cards_total,
        cards_done=job.cards_done,
        cards_skipped=job.cards_skipped,
        current_card_id=job.current_card_id,
        events_dropped=job.events_dropped,
    )


@router.get("/media/status", response_model=MediaStatusOut, summary="Media-download status for every card")
def get_media_status() -> MediaStatusOut:
    """docs/PHASE_3C_PLAN.md §3 -- lets Bulk Localize skip characters it already
    finished without one request per card. Cards with no gallery folder on disk
    yet cost one failed `stat`; a folder that exists but was never downloaded
    into has no `.media.json` and costs the same. Only a folder with an actual
    manifest pays for a JSON read, and that file is small."""
    return MediaStatusOut(
        cards={
            filename: MediaStatusEntryOut(
                files=entry.files,
                bytes=entry.bytes,
                complete=entry.complete,
                dead=entry.dead,
                last_run=entry.last_run,
            )
            for filename, entry in media_status.card_status_map(_shared.index()).items()
        }
    )


@router.post("/media/dedupe", response_model=MediaDedupeOut, summary="Trash exact-duplicate gallery files")
def dedupe_media() -> MediaDedupeOut:
    """Byte-identical leftovers from a media re-download: the old file stays on
    disk under its old name after `.media.json` gets repointed at a new one on
    a rescan. Safe by construction -- a file is only trashed when its sha256
    matches a file the manifest currently claims, so a manually added extra is
    never touched. See `aspect/media/dedupe.py`."""
    result = media_dedupe.dedupe_galleries(settings.galleries_dir, apply=True)
    return MediaDedupeOut(
        folders_touched=result.folders_touched,
        files_trashed=result.files_trashed,
        bytes_freed=result.bytes_freed,
        unresolved=result.unresolved,
    )


@router.get("/characters/{card_id}/media", response_model=MediaManifestOut, summary="A card's media manifest")
def get_character_media(card_id: str) -> MediaManifestOut:
    """The gallery's `.media.json`, verbatim -- which source URLs became which
    local files, which are known permanently gone, and the recent run
    history. 404 only when the card itself doesn't exist; a card with no
    media run yet answers with an empty manifest rather than 404, since
    "never downloaded" is a normal state, not an error."""
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    wanted = gallery.folder_name(record.name, record.gallery_id)
    folder = gallery.resolve_folder(settings.galleries_dir, wanted) or wanted
    manifest = media_manifest.load_manifest(settings.galleries_dir / folder) if folder else media_manifest.empty_manifest()
    return MediaManifestOut(folder=folder, files=manifest["files"], dead=manifest["dead"], runs=manifest["runs"])


def _card_data(idx, card_id: str) -> dict:
    record = _shared.require(idx, card_id)
    path = idx.root / record.filename
    try:
        envelope = pngtools.read_envelope(path.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"cannot read card: {exc}") from exc
    if envelope is None:
        raise HTTPException(status_code=422, detail="file carries no character card")
    return envelope[1]


@router.post("/characters/{card_id}/media/scan", response_model=MediaScanOut, summary="Discover a card's remote media URLs, without downloading")
def scan_character_media(card_id: str) -> MediaScanOut:
    """Salvage item 1 (UI_REWRITE_PLAN.md §1.3, §3.4) -- the server-side walk
    that replaces the browser scanning a card's own fields. A dry run: it
    finds URLs, it does not fetch them, so the UI can show a count before the
    user commits to a job.

    Gallery sources are *listed*, not resolved, which keeps that promise: the
    scan stays instant and offline. Without them a card whose gallery is a
    Civitai post or a MEGA folder answered with two empty lists, and the pane
    said "no remote media URLs found" over a card with forty images behind a
    link -- and offered no Download button to prove otherwise."""
    idx = _shared.index()
    data = _card_data(idx, card_id)
    embedded, lorebook = media_discovery.find_character_media_urls(data)
    sources = [
        MediaScanSourceOut(url=ref.key, handler=ref.handler, status=ref.status)
        for ref in media_discovery.enumerate_sources(data)
        if ref.handler not in ("embedded", "lorebook") and ref.status != media_discovery.IGNORED
    ]
    return MediaScanOut(embedded=embedded, lorebook=lorebook, sources=sources)


@router.post("/characters/{card_id}/media", summary="Download a card's remote media")
def download_character_media(card_id: str, body: MediaDownloadIn) -> StreamingResponse:
    """The one centralized download route -- docs/PHASE_3C_PLAN.md §3.

    The browser keeps discovery (card scan, provider gallery APIs, extractor
    session cookies) and hands over a URL list; everything downstream --
    guard, fetch, sniff, WebP normalization, dedupe, write, thumbnail,
    manifest -- happens here, in `media_writer.download_item`, one call per
    item against a folder resolved from the card's own `gallery_id` so a
    rename mid-run can't miss it.

    Answers NDJSON, streamed: one `{"type": "item", ...}` line per URL as it
    finishes, then a final `{"type": "done", ...}` totals line. The existing
    `onLog`/`onProgress` UI callbacks (all four trigger surfaces named in the
    plan) map onto those events 1:1.
    """
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    folder_name, gallery_dir = _shared.gallery_dir_for_card(idx, record)

    async def generate():
        manifest = media_manifest.load_manifest(gallery_dir)
        ledger = media_manifest.load_dead_ledger()
        index_state = media_writer.GalleryIndex.build(gallery_dir)
        saved = skipped = errors = 0
        # A millisecond timestamp as the starting file index, same scheme the
        # JS loop used -- unique across runs without a persistent counter.
        start_index = int(time.time() * 1000)
        async with net.async_client(
            timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}
        ) as client:
            batch = media_writer.download_batch(
                client,
                gallery_dir,
                folder_name,
                items=[{"url": i.url, "filename": i.filename} for i in body.items],
                prefix=body.prefix,
                start_index=start_index,
                index_state=index_state,
                manifest=manifest,
                ledger=ledger,
                thumbnail_store=_shared.thumbnail_store,
            )
            async for outcome in batch:
                if outcome.status == "saved":
                    saved += 1
                elif outcome.status == "skipped":
                    skipped += 1
                else:
                    errors += 1
                yield (
                    json.dumps(
                        {
                            "type": "item",
                            "url": outcome.url,
                            "status": outcome.status,
                            "file": outcome.file,
                            "reason": outcome.reason,
                            "bytes": outcome.bytes,
                        }
                    )
                    + "\n"
                )

        media_manifest.append_run(
            manifest,
            {
                "at": media_manifest.now_iso(),
                "phase": body.phase,
                "saved": saved,
                "skipped": skipped,
                "errors": errors,
            },
        )
        media_manifest.save_manifest(gallery_dir, manifest)
        media_manifest.save_dead_ledger(ledger)
        yield (
            json.dumps(
                {"type": "done", "saved": saved, "skipped": skipped, "errors": errors, "total": len(body.items)}
            )
            + "\n"
        )

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/media/jobs", response_model=MediaJobOut, summary="Queue a background media-download run")
def submit_media_job(body: MediaJobSubmitIn) -> MediaJobOut:
    """docs/PHASE_3C_PLAN.md §7, "3C-2 -- the job runner". Same contract as
    `POST /characters/{id}/media` -- the browser still does discovery and
    hands over a resolved URL list -- but the download itself runs as a
    detached background task instead of over this request's own connection,
    so it survives the tab closing. Poll `GET /media/jobs/{id}` for progress.

    `discover=true` (UI_REWRITE_PLAN.md §3.4) skips the browser's list
    entirely: the server re-scans the card's own text (the same walk
    `POST .../media/scan` previews) and downloads everything it finds,
    embedded and lorebook URLs together, under one manifest run."""
    idx = _shared.index()
    if body.scope == "all":
        return _submit_batch_job(idx, body)

    record = _shared.require(idx, body.card_id)
    if body.discover:
        # Same walk the batch uses, album pages included, so a single-card
        # "Download" and a whole-archive run can never disagree about what a
        # card's media is.
        items, sources = _discovered_items(_card_data(idx, body.card_id))
    else:
        items = [{"url": i.url, "filename": i.filename} for i in body.items]
        # A caller-supplied URL list says nothing about the card's sources, so
        # it must not write the ledger -- claiming a gallery was dealt with on
        # the strength of a list someone else assembled is how the ledger would
        # start lying.
        sources = []
    folder_name, gallery_dir = _shared.gallery_dir_for_card(idx, record)
    job = _shared.job_store.submit(
        gallery_dir, folder_name, items, body.prefix, body.phase, card_id=body.card_id, sources=sources
    )
    return MediaJobOut(job_id=job.id, state=job.state, total=job.total)


def queue_direct_images(card_id: str) -> None:
    """Queue the download of every direct image a just-added card shows.

    Called by every door a new card comes in through -- the `/build-*` tail and
    `POST /characters` -- so a card's notes and greetings can render from its
    own gallery from the moment it lands, instead of hotlinking a source site
    that may be half-down by the time anyone looks. Direct images only (the
    `embedded`/`lorebook` URLs `find_character_media_urls` reads out of the
    text); folders and albums -- MEGA, imgbb, a Chub gallery -- are links to
    somewhere images *are*, not images the card shows, and stay Localize's job.

    No URLs, no job, and no gallery folder created for nothing. Never raises:
    a card that was written must not report failure because its media could
    not be queued -- Localize will pick it up either way.
    """
    try:
        idx = _shared.index()
        record = _shared.require(idx, card_id)
        embedded, lorebook = media_discovery.find_character_media_urls(_card_data(idx, card_id))
        items = [{"url": url, "filename": None, "prefix": "localized_media"} for url in embedded]
        items += [{"url": url, "filename": None, "prefix": "lorebook_media"} for url in lorebook]
        if not items:
            return
        folder_name, gallery_dir = _shared.gallery_dir_for_card(idx, record)
        _shared.job_store.submit(gallery_dir, folder_name, items, "localized_media", "embedded", card_id=card_id)
    except Exception:
        logger.exception("could not queue the image download for new card %s", card_id)


# The prefix each gallery handler's images are saved under. Everything an
# extractor resolves shares `extgallery`; Chub's own gallery has always had its
# own class, and its existing files on disk are named for it.
_GALLERY_PREFIX = {"chub": "chubgallery"}


def _is_gallery(ref) -> bool:
    return ref.handler is not None and ref.handler not in ("embedded", "lorebook")


def _resolve_galleries(data: dict, gallery_refs: list) -> tuple[list[dict], list[dict]]:
    """Resolve gallery roots to `(items, source_records)`. Network: one fetch
    (or API call) per ref, so callers pass only the refs they mean to pay for
    -- every gallery for a discover run, only the `failed` ones for a broken
    rescan."""
    items: list[dict] = []
    records: list[dict] = []
    if not gallery_refs:
        return items, records
    with net.sync_client(timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for ref in gallery_refs:
            if ref.handler == "chub":
                images = media_extractors.resolve_chub_gallery(client, chub_source.card_id(data))
            else:
                try:
                    images = media_extractors.resolve_gallery_url(client, ref.key)
                except media_extractors.GalleryGone as gone:
                    # Permanently absent: settled, like a dead image in
                    # `dead`, so the card stops re-arming over it.
                    records.append(
                        {
                            "key": ref.key,
                            "handler": ref.handler,
                            "status": media_manifest.SOURCE_FAILED,
                            "reason": str(gone),
                        }
                    )
                    continue
            if images is None:
                # Unreachable, not empty -- record nothing, so the card
                # stays unsatisfied and the next run tries again.
                continue
            prefix = _GALLERY_PREFIX.get(ref.handler, "extgallery")
            for image in images:
                items.append({"url": image.url, "filename": image.filename, "prefix": prefix})
            records.append(
                {
                    "key": ref.key,
                    "handler": ref.handler,
                    "status": media_manifest.SOURCE_DONE,
                    "count": len(images),
                }
            )
    return items, records


# Chub's gallery images are served from charhub; a dead one that the card's own
# text doesn't name can only have come from its Chub gallery.
_CHUB_IMAGE_HOSTS = ("charhub.io", "chub.ai")


def _dead_retry_items(manifest: dict, refs: list) -> list[dict]:
    """Every URL in the manifest's `dead`, as an item flagged to bypass the dead
    ledgers -- whether or not the card still names it.

    A dead entry records no prefix, so it is inferred: the card's own
    embedded/lorebook refs give theirs exactly, and anything else came out of
    an album -- `chubgallery` for a charhub host, `extgallery` otherwise. Only
    the filename prefix and the dedupe tiebreak ride on getting it right. The
    filename hint is left to the writer, which derives it from the URL."""
    direct = {"embedded": "localized_media", "lorebook": "lorebook_media"}
    prefix_for = {ref.key: direct[ref.handler] for ref in refs if ref.handler in direct}
    items = []
    for url in manifest.get("dead", {}):
        prefix = prefix_for.get(url)
        if prefix is None:
            host = (urlsplit(url).hostname or "").lower()
            is_chub = any(host == h or host.endswith("." + h) for h in _CHUB_IMAGE_HOSTS)
            prefix = "chubgallery" if is_chub else "extgallery"
        items.append({"url": url, "filename": None, "prefix": prefix, "retry_dead": True})
    return items


def _broken_items(data: dict, manifest: dict) -> tuple[list[dict], list[dict]]:
    """Rescan broken: the card's dead links, plus whatever its `failed` albums
    resolve to now -- and nothing else. Albums that resolved are not fetched
    again, and sources no run has recorded yet are left for Localize all."""
    refs = media_discovery.enumerate_sources(data)
    stored = manifest.get("sources") or {}
    failed = [
        ref
        for ref in refs
        if _is_gallery(ref) and (stored.get(ref.key) or {}).get("st") == media_manifest.SOURCE_FAILED
    ]
    items, records = _resolve_galleries(data, failed)
    for item in items:
        item["retry_dead"] = True
    return _merge_items(items, _dead_retry_items(manifest, refs)), records


def _merge_items(first: list[dict], second: list[dict]) -> list[dict]:
    """`first` then whatever in `second` it doesn't already carry, by URL."""
    seen = {item["url"] for item in first}
    return first + [item for item in second if item["url"] not in seen]


def _has_broken(manifest: dict) -> bool:
    if manifest.get("dead"):
        return True
    stored = manifest.get("sources") or {}
    return any(isinstance(e, dict) and e.get("st") == media_manifest.SOURCE_FAILED for e in stored.values())


def _discovered_items(data: dict) -> tuple[list[dict], list[dict]]:
    """`(items, source_records)` -- everything a server-side scan can find for
    one card, and what became of each *source* it found.

    The items are the card's own embedded and lorebook URLs plus any gallery
    source resolved through `media/extractors.py`: external album pages linked
    in the card's text (Stage 6B C3, the `extGallery` phase) and, for a
    Chub-sourced card, Chub's own first-party gallery for it (Stage 6B, the
    `chub` extractor).

    Gallery resolution costs at least one HTTP request, so it only runs for
    the minority of cards that actually have a source to resolve. Callers are
    on a worker thread or FastAPI's threadpool, which is why this is sync.

    Each item carries its *own* `prefix`, matching the class it was (or would
    have been) saved under: `localized_media` for embedded, `lorebook_media`
    for lorebook, `extgallery` for an extractor-resolved album/MEGA image,
    `chubgallery` for Chub's own gallery. `download_batch` falls back to the
    job's prefix when an item omits this key, but a `discover=true` job mixes
    all four classes in one call, and `GalleryIndex.find_by_name`'s
    priority-downgrade guard (media-dedup.js:140-145) refuses to match a
    lower-priority file (e.g. `extgallery_..._<mega-handle>.webp`, saved by
    the pre-rework browser pipeline) against a higher-priority query prefix
    like `localized_media`. Tagging every item here is what lets that guard
    still find the archive's existing gallery files instead of silently
    re-fetching and AES-decrypting all of them just to throw the bytes away
    at the content-hash step.

    The second half of the return is the source ledger's raw material: one
    record per source `manifest.effective_sources` can't derive from
    `files`/`dead` -- gallery roots with what they resolved to, URLs nothing
    here handles, and URLs the images-only policy refuses before any fetch.
    The caller writes them (`media_manifest.record_source`) once the run that
    used them is over, so a run that never happened records nothing.
    """
    refs = media_discovery.enumerate_sources(data)
    items: list[dict] = []
    records: list[dict] = []

    for ref in refs:
        if ref.handler == "embedded":
            items.append({"url": ref.key, "filename": None, "prefix": "localized_media"})
        elif ref.handler == "lorebook":
            items.append({"url": ref.key, "filename": None, "prefix": "lorebook_media"})
        elif ref.status == media_discovery.IGNORED:
            records.append(
                {"key": ref.key, "handler": None, "status": media_manifest.SOURCE_IGNORED, "reason": ref.reason}
            )
        elif ref.handler is None:
            records.append({"key": ref.key, "handler": None, "status": media_manifest.SOURCE_UNHANDLED})

    gallery_items, gallery_records = _resolve_galleries(data, [r for r in refs if _is_gallery(r)])
    items += gallery_items
    records += gallery_records

    # A card can reference the same image in prose, in an album, and in its
    # own provider gallery all at once.
    seen: set[str] = set()
    items = [i for i in items if not (i["url"] in seen or seen.add(i["url"]))]
    return items, records


def _submit_batch_job(idx, body: MediaJobSubmitIn) -> MediaJobOut:
    """Stage 6B bulk localize -- the archive-wide half of `POST /media/jobs`.

    Every card goes to the planner; the skip decision is made there rather
    than here. It used to be made here, from `card_status_map`'s `complete` --
    "the last run had no errors" -- which is a statement about a *run*, not
    about the card. A card whose run finished cleanly against a URL list that
    never contained its Civitai gallery was, by that definition, complete
    forever. The planner instead asks whether every source the card carries has
    been dealt with by code no older than what is running now
    (`manifest.sources_satisfied`), so adding an extractor re-arms exactly the
    cards it can now handle and nothing else.

    That check costs a card read and some regex, no network -- and the planner
    is already reading the card, on a worker thread, one at a time. What is
    still deliberately deferred to it is the expensive half: resolving a
    gallery directory mints a `gallery_id` and creates the folder, so doing it
    for every card up front would leave ~3,868 empty directories behind and
    rewrite every card that has no id yet.

    `body.mode` picks what a visited card costs. `pending` is the common run
    and skips satisfied cards. `everything` visits every card, re-resolves every
    album, and retries every dead link. `broken` visits only cards whose
    manifest has a dead link or a `failed` album and retries just those --
    which is what makes it cheap next to `everything`, whose time goes almost
    entirely on re-resolving albums that were fine.
    """
    card_ids = [record.filename for record in idx.cards()]

    def plan(card_id: str):
        live = _shared.index()
        record = _shared.require(live, card_id)
        data = _card_data(live, card_id)

        existing_dir = media_status.gallery_dir_if_present(record)
        manifest = media_manifest.load_manifest(existing_dir) if existing_dir is not None else None

        if body.mode == "broken":
            # Broken entries only ever live in a manifest, so a card without
            # one has nothing to retry -- and its folder must not be minted.
            if manifest is None or not _has_broken(manifest):
                return None
            items, sources = _broken_items(data, manifest)
        else:
            if body.mode == "pending" and manifest is not None:
                if media_manifest.sources_satisfied(manifest, media_discovery.enumerate_sources(data)):
                    return None
            items, sources = _discovered_items(data)
            if body.mode == "everything":
                for item in items:
                    item["retry_dead"] = True
                if manifest is not None:
                    items = _merge_items(items, _dead_retry_items(manifest, media_discovery.enumerate_sources(data)))

        if not items and not sources:
            # No media on this card at all -- return before
            # `gallery_dir_for_card`, which would otherwise create an empty
            # folder for it. A card with sources but no items (every gallery
            # unreachable, or only unhandled URLs) still gets a folder, because
            # recording that is the point.
            return None
        folder_name, gallery_dir = _shared.gallery_dir_for_card(live, record)
        return folder_name, gallery_dir, items, sources

    job = _shared.job_store.submit_batch(card_ids, body.prefix, body.phase, plan=plan)
    return MediaJobOut(job_id=job.id, state=job.state, total=job.total)


@router.get("/media/jobs/{job_id}", response_model=MediaJobStatusOut, summary="Poll a background media-download job")
def get_media_job(job_id: str, after: int = Query(default=0, ge=0, description="Skip events before this index.")) -> MediaJobStatusOut:
    job = _shared.job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_status_out(job, after=after)


@router.post("/media/jobs/{job_id}/cancel", summary="Cancel a queued or running media-download job")
def cancel_media_job(job_id: str) -> dict[str, bool]:
    if not _shared.job_store.cancel(job_id):
        raise HTTPException(status_code=404, detail="job not found or already finished")
    return {"cancelled": True}


@router.get("/media/jobs", response_model=list[MediaJobStatusOut], summary="List recent background media-download jobs")
def list_media_jobs(
    card_id: str | None = Query(default=None),
    active: bool = Query(default=False, description="Only queued/running jobs."),
) -> list[MediaJobStatusOut]:
    jobs = _shared.job_store.list_jobs(card_id=card_id, active_only=active)
    return [_job_status_out(job, include_events=False) for job in jobs]


@router.post("/characters/{card_id}/media/have", response_model=MediaHaveOut, summary="Which of these URLs the gallery already has")
def check_character_media_have(card_id: str, body: MediaHaveIn) -> MediaHaveOut:
    """The local-only skips of `media_writer.download_item` (its manifest hit
    and name index), split out so a caller that has to fetch bytes *itself*
    can ask before paying for them.

    The batch download route gets this for free: it runs both checks before it
    opens a connection. The browser-fetch door (§6) can't -- by the
    time bytes reach `POST .../media/bytes` they've already been downloaded
    and, for MEGA, AES-CTR-decrypted client side. A re-run over an already
    complete MEGA folder therefore re-downloaded and re-decrypted every file
    only to be told "already have this content" 200 times. This route lets
    that caller skip on the name index exactly like the server-fetch path
    does, before a byte moves.

    Recording the match in the manifest mirrors `download_item`'s name-match
    branch: the URL *is* satisfied by that file, and `/media/status` reads the
    manifest, so a skip that goes unrecorded leaves the card looking
    permanently un-downloaded.
    """
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    _folder_name, gallery_dir = _shared.gallery_dir_for_card(idx, record)

    index_state = media_writer.GalleryIndex.build(gallery_dir)
    manifest = media_manifest.load_manifest(gallery_dir)

    have: dict[str, str] = {}
    recorded = False
    for item in body.items:
        # Same two-step as `download_item`: the exact-URL manifest hit first
        # (it catches the URLs whose filenames are too short to index on, e.g.
        # postimg's `1.webp`), then the name index for everything else. A
        # manifest hit is already recorded, by definition -- that is what it
        # is -- so only the name-index branch has anything to write back.
        existing = media_writer.manifest_hit(manifest, index_state, item.url)
        if existing:
            have[item.url] = existing
            continue
        existing = index_state.find_by_name(item.url, item.filename, body.prefix)
        if not existing:
            continue
        have[item.url] = existing
        recorded = True
        media_manifest.record_saved(
            manifest,
            item.url,
            existing,
            index_state.digest_for(existing),
            size=media_writer.size_of(gallery_dir / existing),
        )

    if recorded:
        media_manifest.save_manifest(gallery_dir, manifest)

    return MediaHaveOut(have=have)


@router.post("/characters/{card_id}/media/bytes", response_model=MediaBytesOut, summary="Save one already-fetched media item")
def upload_character_media_bytes(
    card_id: str,
    file: UploadFile = File(description="The media bytes, already fetched by the browser."),
    url: str = Form(description="The source URL -- the manifest's key, and a naming fallback."),
    filename: str | None = Form(default=None, description="Extractor-supplied real filename, if known."),
    prefix: str = Form(default="extgallery"),
) -> MediaBytesOut:
    """The second entry door docs/PHASE_3C_PLAN.md §6 calls for: MEGA's per-file
    AES-CTR decrypt and Pixiv's session-proxied fetch happen in the browser and
    can't move server-side, but everything downstream of having the bytes --
    sniff, WebP-normalize, dedupe, write, thumbnail, manifest -- must still run
    through the one writer, not a second bespoke path. `media_writer.finish_item`
    is exactly the download route's steps 5-10, called here with bytes instead
    of a fetch.

    One item, not a batch -- callers already fetched it one at a time (a
    `downloadFn` per extracted image) -- so a single JSON object is the whole
    response, no NDJSON framing.
    """
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    folder_name, gallery_dir = _shared.gallery_dir_for_card(idx, record)

    payload = file.file.read()
    if not payload:
        raise HTTPException(status_code=422, detail="the upload has no bytes")

    manifest = media_manifest.load_manifest(gallery_dir)
    ledger = media_manifest.load_dead_ledger()
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    outcome = media_writer.finish_item(
        gallery_dir,
        folder_name,
        url=url,
        filename_hint=filename,
        prefix=prefix,
        index=int(time.time() * 1000),
        index_state=index_state,
        manifest=manifest,
        ledger=ledger,
        body=payload,
        content_type=file.content_type,
        thumbnail_store=_shared.thumbnail_store,
    )

    media_manifest.save_manifest(gallery_dir, manifest)
    media_manifest.save_dead_ledger(ledger)

    return MediaBytesOut(status=outcome.status, file=outcome.file, reason=outcome.reason, bytes=outcome.bytes)
