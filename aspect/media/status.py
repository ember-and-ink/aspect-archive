"""Per-card media status, read once and shared.

Factored out of `GET /media/status` at Stage 6B (docs/UI_REWRITE_PLAN.md) so the
archive-wide batch job can plan a run without a second sweep over every
manifest. Two callers agree on one definition of "complete":

  - `GET /media/status`, the whole map, for the UI
  - `needs_media=true` on `GET /characters`, the browse chip

The third used to be `POST /media/jobs {scope: "all"}`, deciding which cards to
skip. It no longer is: "the last run had no errors" is a statement about a run,
not about a card, and a card whose run finished cleanly against a URL list that
never contained its Civitai gallery counted as complete forever. That decision
moved into the batch planner, which reads the card and asks
`manifest.sources_satisfied` instead. What survives here is the cheap,
manifest-only half of the same idea (`_has_newly_handleable_source`), so the
chip and the status map don't keep calling those cards finished either.

The sweep costs one failed `stat` for a card with no gallery folder, the same
for a folder that was never downloaded into, and one small JSON read for a
folder that has a manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aspect.cards import gallery
from aspect.config import settings
from aspect.media import manifest as media_manifest


@dataclass(frozen=True)
class CardMediaStatus:
    files: int
    bytes: int
    complete: bool
    dead: int
    last_run: str | None


def gallery_dir_if_present(record) -> Path | None:
    """The card's gallery folder, only if it already exists.

    Deliberately *not* `_shared.gallery_dir_for_card`, which mints a
    `gallery_id`, rewrites the PNG and creates the directory. A read-only sweep
    over 3,868 cards must never do any of that.
    """
    folder = gallery.resolve_folder(
        settings.galleries_dir, gallery.folder_name(record.name, record.gallery_id)
    )
    return settings.galleries_dir / folder if folder else None


def _has_newly_handleable_source(manifest) -> bool:
    """Whether this manifest records a source it could not handle at the time
    and *can* handle now.

    The manifest-only half of `manifest.sources_satisfied`. The full check
    needs the card's own text, which is far too expensive for a sweep that
    `GET /media/status` and the browse `needs_media` chip both run over every
    card -- but a URL already recorded `unhandled` needs no card read at all,
    and that is the case this exists for: after `media/civitai.py` landed, the
    cards carrying Civitai links stop reporting themselves complete without
    anyone re-scanning anything.
    """
    from aspect.media import extractors

    sources = manifest.get("sources")
    if not isinstance(sources, dict):
        return False
    for key, entry in sources.items():
        if not isinstance(entry, dict) or entry.get("st") != media_manifest.SOURCE_UNHANDLED:
            continue
        if extractors.extractor_for(key) is not None:
            return True
    return False


def card_status_map(idx) -> dict[str, CardMediaStatus]:
    """Every card that has a manifest, keyed by filename.

    A card missing from the result has never had a media run -- which is not
    the same as "has no media", and callers must not conflate the two. Deciding
    whether a never-scanned card *has* remote media means re-reading its prose;
    that is what `POST /characters/{id}/media/scan` is for, one card at a time.
    """
    out: dict[str, CardMediaStatus] = {}
    for record in idx.cards():
        gallery_dir = gallery_dir_if_present(record)
        if gallery_dir is None:
            continue
        try:
            media_manifest.manifest_path(gallery_dir).stat()
        except OSError:
            continue
        manifest = media_manifest.load_manifest(gallery_dir)
        runs = manifest["runs"]
        last_run = runs[-1] if runs else None
        out[record.filename] = CardMediaStatus(
            files=len(manifest["files"]),
            bytes=sum(f.get("size", 0) for f in manifest["files"].values()),
            complete=bool(last_run and last_run.get("errors", 0) == 0)
            and not _has_newly_handleable_source(manifest),
            dead=len(manifest["dead"]),
            last_run=last_run.get("at") if last_run else None,
        )
    return out
