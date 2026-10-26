"""`/api/v1` archive-wide routes: filter facets, index and disk health, a forced
rescan, and the browser UI's own settings document.

The settings blob is user data, not a cache -- it is the only copy of the Chub
and DataCat tokens. See `aspect.state.ui_settings`.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from aspect.api.schemas import (
    CountOut,
    FacetsOut,
    FacetValue,
    IndexStatsOut,
    MediaExtStatOut,
    MediaStatsOut,
    StatsOut,
    ThumbStatsOut,
)
from aspect.api.v1 import _shared
from aspect.archive import catalog
from aspect.cards import gallery
from aspect.config import settings
from aspect.runtime import net
from aspect.state import ui_settings

logger = logging.getLogger("aspect.api")

router = APIRouter()

def _settings_store() -> ui_settings.SettingsStore:
    """Built per call so a test that repoints `settings.settings_file` -- or a
    hand-edit of the file between requests -- is picked up without a restart.
    Construction is just holding a path; there is nothing to cache."""
    return ui_settings.SettingsStore(settings.settings_file)


@router.get("/facets", response_model=FacetsOut, summary="Filter values with counts")
def facets(
    limit: int = Query(0, ge=0, description="Cap each facet to its most common N values; 0 for all."),
) -> FacetsOut:
    idx = _shared.index()
    tags: Counter[str] = Counter()
    creators: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    for record in idx.cards():
        tags.update(record.tags)
        if record.creator:
            creators[record.creator] += 1
        if record.source_kind:
            sources[record.source_kind] += 1

    def top(counter: Counter[str]) -> list[FacetValue]:
        # Count first, then name: the useful order for a filter list, and stable
        # because the name breaks the tie.
        items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].casefold()))
        return [FacetValue(value=v, count=c) for v, c in (items[:limit] if limit else items)]

    return FacetsOut(tags=top(tags), creators=top(creators), sources=top(sources))


def _media_stats() -> MediaStatsOut:
    """Every file under the galleries directory, tallied by extension.

    Two-level scandir -- one pass over the gallery folders, one over each
    folder's files -- the same shape as `list_galleries` + `list_gallery_files`
    in `aspect.api.v1.galleries`. The galleries directory never nests deeper than
    that, so this is the whole tree without needing to recurse.
    """
    counts: Counter[str] = Counter()
    byte_totals: Counter[str] = Counter()
    try:
        with os.scandir(settings.galleries_dir) as folders:
            folder_names = [e.name for e in folders if e.is_dir() and not e.name.startswith(".")]
    except OSError:
        folder_names = []
    for name in folder_names:
        try:
            with os.scandir(settings.galleries_dir / name) as files:
                for entry in files:
                    if not entry.is_file() or entry.name.startswith("."):
                        continue
                    ext = Path(entry.name).suffix.casefold().lstrip(".") or "other"
                    size = entry.stat().st_size
                    counts[ext] += 1
                    byte_totals[ext] += size
        except OSError:
            continue
    by_ext = [
        MediaExtStatOut(ext=ext, count=counts[ext], bytes=byte_totals[ext])
        for ext in sorted(counts, key=lambda e: -counts[e])
    ]
    return MediaStatsOut(images=sum(counts.values()), bytes=sum(byte_totals.values()), by_ext=by_ext)


@router.get("/count", response_model=CountOut, summary="How many cards the archive holds")
def count() -> CountOut:
    """Answered from the in-memory index alone. `/stats` stats every gallery file
    and thumb on disk -- ~5 s on the Unraid share -- which is fine behind the
    Settings section that shows those numbers and not for a browse header."""
    return CountOut(cards=len(_shared.index().cards()))


@router.get("/stats", response_model=StatsOut, summary="Archive and index health")
def stats() -> StatsOut:
    idx = _shared.index()
    records = idx.all()
    healthy = [r for r in records if r.ok]
    filenames = [r.filename for r in records]
    cached = sum(1 for name in filenames if _shared.thumbnail_store.avatar_path(name).is_file())
    galleries = sum(
        1
        for r in healthy
        if r.gallery_id
        and (settings.galleries_dir / gallery.folder_name(r.name, r.gallery_id)).is_dir()
    )
    last = idx.last_stats
    return StatsOut(
        cards=len(healthy),
        unreadable=len(records) - len(healthy),
        bytes=sum(r.size for r in records),
        creators=len({r.creator for r in healthy if r.creator}),
        tags=len({t for r in healthy for t in r.tags}),
        galleries=galleries,
        archive_dir=str(settings.archive_dir),
        thumbs=ThumbStatsOut(
            cached=cached,
            missing=len(filenames) - cached,
            stale=len(_shared.thumbnail_store.stale(filenames)),
        ),
        index=IndexStatsOut(
            scanned=last.scanned,
            parsed=last.parsed,
            unchanged=last.unchanged,
            removed=last.removed,
            seconds=round(last.seconds, 4),
        ),
        media=_media_stats(),
    )


@router.post("/refresh", response_model=IndexStatsOut, summary="Force a rescan")
def refresh() -> IndexStatsOut:
    """Rescan the archive directory. The only way a change made outside this
    process -- a file dropped in by hand, a `make` script -- reaches the index:
    reads never rescan, and this process's own write routes refresh themselves."""
    stats_ = catalog.index().refresh()
    return IndexStatsOut(
        scanned=stats_.scanned,
        parsed=stats_.parsed,
        unchanged=stats_.unchanged,
        removed=stats_.removed,
        seconds=round(stats_.seconds, 4),
    )


@router.get("/settings", summary="The browser UI's stored settings")
def get_settings() -> dict:
    """The settings blob, or `{}` if nothing has been stored yet.

    Deliberately untyped: this is an opaque object owned by the frontend. Giving
    it a pydantic model would put the vendored UI's 117-key schema into the
    server's contract, and every UI change would then need a matching change
    here -- the compatibility burden the whole pivot exists to shed.
    """
    try:
        return _settings_store().read()
    except ui_settings.SettingsError as exc:
        # 500, not an empty object. Handing back {} would look like a fresh
        # archive, and the frontend would fill in defaults and save them
        # straight over the damaged file, turning a recoverable problem into
        # a lost Chub token.
        logger.error("settings unreadable: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/settings", summary="Replace the browser UI's stored settings")
def put_settings(blob: dict) -> dict:
    """Replace the whole blob and return what was stored.

    Whole-document replace rather than a merge: the frontend already holds the
    complete settings object in memory and treats itself as the owner, and a
    merge endpoint could never express a *deleted* key -- which the frontend's
    own boot migrations rely on being able to do.
    """
    try:
        stored = _settings_store().write(blob)
    except ui_settings.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The one key the server itself reads out of this blob is `httpProxyUrl`,
    # and its lookup is memoized against the file's mtime. Mtime granularity is
    # only worth trusting when it has actually changed -- here we know for
    # certain the file just did.
    net.reset_cache()
    return stored
