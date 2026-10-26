"""`/api/v1` character routes: browse the archive, read one card in full, and
the write half -- replace contents, replace the image, retag in bulk, bin a card
-- plus the card's own PNG and thumbnail.

Every write goes through `proxy.cards.edit`; read that module's docstring before
changing anything here that mutates. A field edit must never re-encode pixels,
every write is atomic, and nothing is ever unlinked.
"""

from __future__ import annotations

import copy
import html
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)

from proxy import deps
from proxy.api.schemas import (
    BulkDeleteIn,
    BulkDeleteOut,
    BulkTagsIn,
    BulkTagsOut,
    CardDetailOut,
    CardImportOut,
    CardListOut,
    CardOut,
    CardWriteIn,
    CharactersHaveFragmentsOut,
    CharactersHaveIn,
    CharactersHaveOut,
    DeletedOut,
    FavoriteIn,
    FavoriteOut,
    ForkParentOut,
    GalleryOut,
    TagsApplyIn,
    TagsApplyOut,
)
from proxy.api.v1 import _shared
from proxy.api.v1 import media as v1_media
from proxy.archive import catalog
from proxy.cards import avatar_refetch, edit, forks, gallery, intake, pngtools
from proxy.cards.naming import id_fragment
from proxy.media import expressions
from proxy.media import manifest as media_manifest
from proxy.config import settings

logger = logging.getLogger("jai_proxy.api")

router = APIRouter()

# What `sort` accepts, mapped to the record attribute it orders by. A whitelist
# rather than a getattr on user input, and small on purpose -- these are the
# orderings a browse grid has a use for.
_SORTS: dict[str, str] = {
    "name": "name",
    "creator": "creator",
    "modified": "mtime",
    # When the card arrived, not when its file was last touched. The better
    # default for "recently added" -- see CardOut.linked_at.
    "added": "linked_at",
    # When the card was created rather than when it arrived here. Stable across
    # the bulk passes, which is what makes it worth having as its own ordering.
    "created": "create_date",
    "size": "size",
    "greetings": "greeting_count",
    "lore": "lore_entry_count",
    "description": "description_chars",
    "prompt": "prompt_chars",
}

def _card_out(record: catalog.CardSummary, *, extensions: bool = False) -> CardOut:
    quoted = quote(record.filename, safe="")
    return CardOut(
        extensions=record.extensions if extensions else None,
        id=record.filename,
        name=record.name,
        creator=record.creator,
        page_name=record.page_name,
        tags=list(record.tags),
        source_kind=record.source_kind,
        source_url=record.source_url,
        card_id=record.card_id,
        fragment=record.fragment,
        gallery_id=record.gallery_id,
        character_version=record.character_version,
        greetings=record.greeting_count,
        lore_entries=record.lore_entry_count,
        description_chars=record.description_chars,
        prompt_chars=record.prompt_chars,
        has_creator_notes=record.has_creator_notes,
        has_example_dialogue=record.has_example_dialogue,
        favorite=record.favorite,
        is_fork=bool(record.fork_of),
        size=record.size,
        avatar_width=record.avatar_width,
        avatar_height=record.avatar_height,
        modified=datetime.fromtimestamp(record.mtime, tz=timezone.utc),
        linked_at=record.linked_at,
        create_date=record.create_date,
        thumb_url=f"{_shared.PREFIX}/characters/{quoted}/thumb",
        png_url=f"{_shared.PREFIX}/characters/{quoted}/png",
        error=record.error or None,
    )


def _folder_out(record: catalog.CardSummary, root: Path) -> GalleryOut:
    """A card's media folder under `root`, measured on disk -- `root` is either
    `settings.galleries_dir` or `settings.expressions_dir`, the two folders a
    card can carry (docs/FORKS_AND_EXTRAS_PLAN.md §2). One `scandir` of one
    directory, so it belongs on the detail view and not on a list of
    thousands.

    `folder` reports the directory that actually holds the files, which after a
    rename is not the name the card's own fields would compute -- see
    `gallery.resolve_folder`."""
    wanted = gallery.folder_name(record.name, record.gallery_id)
    folder = gallery.resolve_folder(root, wanted) or wanted
    out = GalleryOut(
        gallery_id=record.gallery_id, folder=folder, exists=False, images=0, bytes=0
    )
    if not folder:
        return out
    path = root / folder
    try:
        with os.scandir(path) as entries:
            files = [e for e in entries if e.is_file() and not e.name.startswith(".")]
    except (OSError, ValueError):
        return out
    out.exists = True
    out.images = len(files)
    out.bytes = sum(e.stat().st_size for e in files)
    return out



def _media_map(gallery_out: GalleryOut) -> dict[str, str | None]:
    """Source URL -> the gallery file that holds it, or None for a URL the
    manifest records as dead. The creator-notes renderer reads its images from
    this and nothing else: a URL missing here is shown as "not in gallery"
    rather than fetched from the source site.

    Each URL is keyed both as the manifest has it -- discovery reads the card's
    raw text -- and HTML-unescaped, because the renderer looks up an `<img>`'s
    *decoded* `src`: a query string written `&amp;` in the notes would
    otherwise never find its own file. A manifest entry whose file has since
    been binned is left out, the same check `writer.manifest_hit` makes."""
    if not gallery_out.exists or not gallery_out.folder:
        return {}
    directory = settings.galleries_dir / gallery_out.folder
    try:
        on_disk = {name for name in os.listdir(directory) if not name.startswith(".")}
    except OSError:
        return {}
    manifest = media_manifest.load_manifest(directory)
    quoted_folder = quote(gallery_out.folder, safe="")
    out: dict[str, str | None] = {url: None for url in manifest["dead"]}
    # Files second, so a URL with a file on disk resolves to it even if it was
    # also marked dead at some point.
    for url, entry in manifest["files"].items():
        name = entry.get("file") if isinstance(entry, dict) else None
        if name in on_disk:
            out[url] = f"{_shared.PREFIX}/galleries/{quoted_folder}/files/{quote(name, safe='')}"
    for url, value in list(out.items()):
        out.setdefault(html.unescape(url), value)
    return out

# What a `scope`d search matches `q` against, as a function of the record. The
# default (`all`) uses the pre-folded haystack; the rest fold on demand, which
# costs about a millisecond across the whole archive and keeps the index from
# carrying four more derived strings for a feature driven from one overlay.
_SCOPES: dict[str, Callable[[catalog.CardSummary], str]] = {
    "all": lambda r: r.haystack,
    "name": lambda r: r.name.casefold(),
    "creator": lambda r: r.creator.casefold(),
    "tags": lambda r: " ".join(r.tags).casefold(),
}


def _cards_needing_media() -> set[str]:
    """Filenames whose media was attempted and did not fully come down.

    The chip §3.3 deferred, and the shape it settled on: manifest I/O, paid only
    when the filter is actually asked for, and applied *after* the cheap
    predicates have narrowed the set -- never inside `_matches`, which runs per
    record on every list call.

    "Needs media" is evidence-based on purpose: a card whose last run reported
    errors, or that carries dead URLs. A card nobody has ever scanned is not
    claimed to need anything, because deciding that would mean re-reading every
    card's prose to discover URLs -- exactly the work the list path exists not to
    do. `POST /characters/{id}/media/scan` is where that question gets answered,
    one card at a time.
    """
    idx = _shared.index()
    needing: set[str] = set()
    for record in idx.cards():
        folder = gallery.resolve_folder(
            settings.galleries_dir, gallery.folder_name(record.name, record.gallery_id)
        )
        if not folder:
            continue
        gallery_dir = settings.galleries_dir / folder
        try:
            media_manifest.manifest_path(gallery_dir).stat()
        except OSError:
            continue
        manifest = media_manifest.load_manifest(gallery_dir)
        last_run = manifest["runs"][-1] if manifest["runs"] else None
        if (last_run and last_run.get("errors", 0)) or manifest["dead"]:
            needing.add(record.filename)
    return needing


def _matches(
    record: catalog.CardSummary,
    *,
    terms: list[str],
    scope: str,
    tags: list[str],
    excluded_tags: list[str],
    creator: str | None,
    sources: set[str],
    has_lorebook: bool | None,
    has_gallery: bool | None,
    favorite: bool | None,
    is_fork: bool | None,
    fork_of: str | None,
    untagged: bool | None,
    min_greetings: int | None,
    added_after: str | None,
    low_res: bool | None,
) -> bool:
    # AND across search terms so a second word narrows rather than widens --
    # "korny abbie" should find the one card, not every card by either.
    if terms:
        haystack = _SCOPES[scope](record)
        if any(term not in haystack for term in terms):
            return False
    if tags or excluded_tags:
        present = {t.casefold() for t in record.tags}
        if not all(t in present for t in tags):
            return False
        # Exclude wins over include, which only matters for a client that sends
        # the same tag both ways -- the chip strip cannot, but a hand-built URL
        # can, and "not this tag" is the stronger statement of the two.
        if any(t in present for t in excluded_tags):
            return False
    if creator is not None and record.creator.casefold() != creator:
        return False
    # OR across the kinds given, because the UI's Source filter is per *platform*
    # and a platform can span two importer kinds -- Chub is `chub_import` for the
    # bulk pass and `chub_core` for a live capture, and "cards from Chub" means
    # both. Repeatable rather than prefix-matched: the kind vocabulary is the
    # server's, and a client that wants one exact kind can still send just that.
    if sources and record.source_kind.casefold() not in sources:
        return False
    if has_lorebook is not None and bool(record.lore_entry_count) != has_lorebook:
        return False
    if has_gallery is not None and bool(record.gallery_id) != has_gallery:
        return False
    if favorite is not None and record.favorite != favorite:
        return False
    if is_fork is not None and bool(record.fork_of) != is_fork:
        return False
    # Exact fragment match, for the Related pane's "forks of this card" --
    # `fork_of` is already the *root*, flattened (docs/FORKS_AND_EXTRAS_PLAN.md
    # §3), so this finds every sibling fork at once regardless of which one
    # of them the caller is looking at.
    if fork_of is not None and record.fork_of != fork_of:
        return False
    if untagged is not None and (not record.tags) != untagged:
        return False
    if min_greetings is not None and record.greeting_count < min_greetings:
        return False
    if low_res is not None and avatar_refetch.is_low_res(record.avatar_width, record.avatar_height) != low_res:
        return False
    if added_after is not None:
        # String comparison, not date parsing: `linked_at` is stored as the raw
        # ISO-8601 the importer stamped and ISO-8601 sorts lexically, so this is
        # the same ordering `sort=added` uses. A card with no stamp at all is
        # not "added recently" -- it is undated, and drops out.
        if not record.linked_at or record.linked_at < added_after:
            return False
    return True


def _etag_of(path: Path) -> str:
    """The validator `_shared.serve_file` hands out, recomputed for a write's
    precondition check. Same (mtime_ns, size) pair the index invalidates on, so
    a client that has read a card can prove it is writing over what it read."""
    st = path.stat()
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'


def _check_precondition(path: Path, if_match: str | None) -> None:
    """Enforce `If-Match` when the client sent one.

    Optional, and worth having even so. Two tabs open on the same card is the
    ordinary case here, and the loser of that race silently reverts the winner's
    edit -- with a whole-document replace there is no partial overlap to soften
    it. A client that sends the ETag it read gets told (412) instead.
    """
    if not if_match or if_match == "*":
        return
    current = _etag_of(path)
    if current not in [t.strip() for t in if_match.split(",")]:
        raise HTTPException(
            status_code=412,
            detail="the card changed since you read it; reload before saving over it",
        )


@router.get("/characters", response_model=CardListOut, summary="List and filter cards")
def list_characters(
    q: str | None = Query(
        None,
        description="Free text over name, creator, page title, filename and tags. Space-separated terms are ANDed. Prose is not searched.",
    ),
    scope: Literal["all", "name", "creator", "tags"] = Query(
        "all",
        description="Which fields `q` is matched against. A narrower scope narrows the same query -- it never reaches fields `all` does not cover, and prose is searched by none of them.",
    ),
    tag: list[str] = Query(default=[], description="Repeatable; a card must carry every tag given."),
    exclude_tag: list[str] = Query(
        default=[],
        description="Repeatable; a card carrying any of these is left out. Overrides `tag` when both name the same one.",
    ),
    creator: str | None = Query(None, description="Exact creator match, case-insensitive."),
    source: list[str] = Query(
        default=[],
        description="Repeatable; exact `source_kind` match, e.g. `chub_import`. Several are ORed, which is how one platform spanning two importer kinds (`chub_import` and `chub_core`) is asked for.",
    ),
    has_lorebook: bool | None = Query(None),
    has_gallery: bool | None = Query(None, description="Whether the card carries a gallery_id at all."),
    favorite: bool | None = Query(None, description="Only starred cards, or only unstarred ones."),
    is_fork: bool | None = Query(None, description="Only forks, or only cards that aren't forks."),
    fork_of: str | None = Query(
        None,
        description="The root original's `_<id8>` fragment -- every fork of that card (siblings included, per the flattened lineage in docs/FORKS_AND_EXTRAS_PLAN.md §3), and nothing else.",
    ),
    untagged: bool | None = Query(None, description="`true` for cards carrying no tags at all -- the tagging backlog."),
    min_greetings: int | None = Query(
        None, ge=0, description="Cards with at least this many greetings. `2` is the mock's multi-greeting chip."
    ),
    added_after: str | None = Query(
        None,
        description="Cards acquired at or after this ISO-8601 instant, compared against `linked_at`. Cards with no stamp are excluded.",
    ),
    low_res: bool | None = Query(
        None,
        description="`true` for cards whose image is under 250px on both sides -- a thumbnail saved in place of the portrait, which `POST /characters/{id}/avatar/refetch` can repair.",
    ),
    needs_media: bool | None = Query(
        None,
        description=(
            "`true` for cards whose media was attempted and did not fully come down -- the last run "
            "reported errors, or the manifest carries dead URLs. Costs a manifest read per card, so it "
            "is only paid when asked for. A card that has never been scanned counts as not needing "
            "media: whether it has any is a question only `/characters/{id}/media/scan` can answer."
        ),
    ),
    health: Literal["ok", "broken", "all"] = Query(
        "ok",
        description="`ok` lists parseable cards (the default), `broken` the ones that fail to parse, `all` both.",
    ),
    sort: str = Query(
        "name",
        description="One of name, creator, added, created, modified, size, greetings, lore, description, prompt. Prefix with `-` to reverse.",
    ),
    limit: int = Query(60, ge=0, le=5000, description="0 means no limit -- the whole filtered set."),
    offset: int = Query(0, ge=0),
    include: list[Literal["extensions"]] = Query(
        default=[],
        description="Optional heavier fields. `extensions` attaches each card's `data.extensions` -- provider links, gallery_id, version uids -- at roughly 790 bytes a card.",
    ),
) -> CardListOut:
    idx = _shared.index()
    if health == "ok":
        records = idx.cards()
    elif health == "broken":
        records = idx.broken()
    else:
        records = idx.all()

    terms = [t for t in (q or "").casefold().split() if t]
    wanted_tags = [t.casefold() for t in tag if t.strip()]
    unwanted_tags = [t.casefold() for t in exclude_tag if t.strip()]
    wanted_sources = {s.casefold() for s in source if s.strip()}
    matched = [
        r
        for r in records
        if _matches(
            r,
            terms=terms,
            scope=scope,
            tags=wanted_tags,
            excluded_tags=unwanted_tags,
            creator=creator.casefold() if creator else None,
            sources=wanted_sources,
            has_lorebook=has_lorebook,
            has_gallery=has_gallery,
            favorite=favorite,
            is_fork=is_fork,
            fork_of=fork_of,
            untagged=untagged,
            min_greetings=min_greetings,
            added_after=added_after,
            low_res=low_res,
        )
    ]

    if needs_media is not None:
        needing = _cards_needing_media()
        matched = [r for r in matched if (r.filename in needing) == needs_media]

    descending = sort.startswith("-")
    key_name = _SORTS.get(sort.lstrip("-"))
    if key_name is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown sort {sort!r}; expected one of {', '.join(sorted(_SORTS))} optionally prefixed with '-'",
        )
    if key_name in ("name", "creator"):
        # Case-insensitive, and tie-broken on the filename so the order is total:
        # 3,839 cards include plenty of shared names, and a browse grid that
        # reshuffles equal rows between requests breaks pagination.
        matched.sort(key=lambda r: (getattr(r, key_name).casefold(), r.filename), reverse=descending)
    else:
        matched.sort(key=lambda r: (getattr(r, key_name), r.filename.casefold()), reverse=descending)

    window = matched[offset:] if limit == 0 else matched[offset : offset + limit]
    want_extensions = "extensions" in include
    return CardListOut(
        total=len(matched),
        limit=limit,
        offset=offset,
        items=[_card_out(r, extensions=want_extensions) for r in window],
    )


@router.get("/characters/have-fragments", response_model=CharactersHaveFragmentsOut, summary="Every provider-id fragment already in the archive")
def characters_have_fragments() -> CharactersHaveFragmentsOut:
    """The whole `_<id8>` fragment set, for Discover to fetch once and match
    against locally instead of resending its ever-growing loaded-id list to
    `POST /characters/have` on every scroll tick. See that route's docstring
    for why the per-id version was slow, and `ArchiveIndex.fragments`.

    Registered before `/characters/{card_id}` on purpose: Starlette matches
    routes in declaration order, and a literal path below a `{card_id}`
    catch-all would be swallowed by it as "the card whose id is
    'have-fragments'" and 404."""
    return CharactersHaveFragmentsOut(fragments=sorted(catalog.index().fragments()))


@router.get("/characters/{card_id}", response_model=CardDetailOut, summary="One card in full")
def get_character(card_id: str, response: Response = None) -> CardDetailOut:  # type: ignore[assignment]
    # The detail read carries the same `If-Match` validator a write checks against
    # (`_etag_of`), so an editor gets its precondition token from the read that
    # populated the page -- no second request to fetch it. `response` is injected
    # for the HTTP route and left None for the internal callers below (`put_*`
    # return `get_character(...)`), which have their own response.
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    if response is not None and record.ok:
        response.headers["ETag"] = _etag_of(idx.root / record.filename)
    # The index already diagnosed this card on the scan that found it, and its
    # reason is more specific than anything recoverable here (a file that is not a
    # PNG, versus a PNG that was never a card, versus a corrupt payload). Report
    # that one rather than a second, vaguer opinion.
    if not record.ok:
        raise HTTPException(status_code=422, detail=record.error)
    path = idx.root / record.filename
    try:
        envelope = pngtools.read_envelope(path.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"cannot read card: {exc}") from exc
    if envelope is None:
        # Only reachable when the card changed between the scan and this read.
        raise HTTPException(status_code=422, detail="file carries no character card")
    outer, data = envelope
    expressions = _folder_out(record, settings.expressions_dir)
    gallery_out = _folder_out(record, settings.galleries_dir)
    return CardDetailOut(
        **_card_out(record).model_dump(),
        spec=str(outer.get("spec", "")),
        spec_version=str(outer.get("spec_version", "")),
        card=data,
        avatar_origin=avatar_refetch.origin(data),
        gallery=gallery_out,
        media=_media_map(gallery_out),
        expressions=expressions,
        expressions_zip_url=(
            f"{_shared.PREFIX}/characters/{quote(record.filename, safe='')}/expressions.zip"
            if expressions.exists
            else None
        ),
        forked_from=_forked_from(idx, data),
    )


def _forked_from(idx: catalog.ArchiveIndex, data: dict) -> ForkParentOut | None:
    """The card `extensions.fork.of` resolves to right now, or None -- both
    for a card that isn't a fork and for one whose original was since
    deleted. Resolved against the already-loaded index (a single
    `by_fragment` dict lookup), never a second disk read."""
    fork = (data.get("extensions") or {}).get("fork")
    of = fork.get("of") if isinstance(fork, dict) else None
    if not isinstance(of, str) or not of:
        return None
    parents = idx.by_fragment(of)
    if not parents:
        return None
    parent = parents[0]
    return ForkParentOut(id=parent.filename, name=parent.name)


@router.post("/characters", response_model=CardImportOut, summary="Adopt a card PNG into the archive")
def import_character(
    file: UploadFile = File(description="A tavern card PNG -- the card is read out of its tEXt chunks."),
    on_duplicate: Literal["skip", "overwrite"] = Form(
        "skip",
        description="What to do when the archive already holds this card's `_<id8>` fragment. `skip` reports the card already there; `overwrite` replaces it in place, under the name it currently carries.",
    ),
    fork_of: str | None = Form(
        None,
        description="The parent's `_<id8>` fragment. Adopts the uploaded PNG as a fork of that card (docs/FORKS_AND_EXTRAS_PLAN.md §3): forces `extensions.gallery_id` to the parent's and stamps `extensions.fork`. The uploaded card's own content and id are otherwise taken as given -- this is 'I already have a card that is a rewrite of one I own', not a clone.",
    ),
    note: str | None = Form(None, description="Free text for `extensions.fork.note`. Only meaningful with `fork_of`."),
) -> CardImportOut:
    """Take in a card as a file: a PNG dragged onto the import modal, or one
    unpacked from a Character Library bundle.

    The other door into the archive. The `/build-*` routes each know a site and
    take what the browser captured there; this one is handed the finished card
    and has to work out what it is -- see `proxy.cards.intake`, which owns that
    judgement and the cleaning that follows from it.

    Duplicates are decided on the `_<id8>` fragment alone, never the name, like
    every other duplicate check in the archive: a card renamed by `make names`
    is still the same card. A skipped duplicate is a 200 naming the file that is
    already here, not an error -- the caller wanted it present, and it is.
    """
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="the uploaded file is empty")
    try:
        prepared = intake.adopt(raw)
    except intake.IntakeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    idx = _shared.index()

    if fork_of:
        parents = idx.by_fragment(fork_of)
        if not parents:
            raise HTTPException(status_code=422, detail=f"fork_of names no card in the archive: {fork_of!r}")
        parent = parents[0]
        root = parent.fork_of or parent.fragment
        forks.stamp(
            prepared.payload,
            gallery_id=parent.gallery_id,
            root=root,
            of_filename=parent.filename,
            note=note or "",
        )
        # A rewrite handed back here still carries the id of the card it was
        # rewritten from, which is the archive's dedupe key -- without a new
        # one the fork is skipped as its own parent's duplicate.
        fresh = forks.reidentify(prepared.payload, parent.fragment)
        if fresh:
            prepared.card_id = fresh
            prepared.fragment = id_fragment(fresh)

    existing = idx.by_fragment(prepared.fragment) if prepared.fragment else ()
    if existing and on_duplicate == "skip":
        record = existing[0]
        logger.info("import: already have %s (%s)", prepared.name, record.filename)
        return CardImportOut(
            id=record.filename,
            name=record.name,
            creator=record.creator,
            source=prepared.source,
            duplicate=True,
            warnings=prepared.warnings,
        )

    # An overwrite writes over the file that is here now, whatever it is called;
    # letting the derived name win would leave two cards sharing one fragment.
    target = existing[0].filename if existing else None
    try:
        path = deps.png_writer.write_payload(
            prepared.payload,
            raw,
            creator=prepared.creator,
            name=prepared.name,
            card_id=prepared.card_id or None,
            filename=target,
            normalize=prepared.normalize,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"cannot write the card: {exc}") from exc

    # New pixels behind a name the cache may already hold -- the avatar cache has
    # no staleness check, so an overwrite would otherwise keep showing the old face.
    _shared.thumbnail_store.forget(path.name)
    catalog.index().refresh()
    v1_media.queue_direct_images(path.name)
    logger.info("import: saved %s card %s (%s)", prepared.source, prepared.name, path.name)
    return CardImportOut(
        id=path.name,
        name=prepared.name,
        creator=prepared.creator,
        source=prepared.source,
        overwritten=bool(existing),
        warnings=prepared.warnings,
    )


@router.post("/characters/{card_id}/fork", response_model=CardDetailOut, summary="Fork a card")
def fork_character(card_id: str) -> CardDetailOut:
    """Clone `card_id` into a new, standalone card that shares its gallery
    but not its identity -- docs/FORKS_AND_EXTRAS_PLAN.md §3, the "born here"
    primitive. `POST /characters` with `fork_of=` is the other one, for a
    fork arriving as a file rather than made here.

    The fork starts as a full copy of the parent's text -- the workflow is
    *rewrite*, not author-from-scratch -- so the two cards are
    content-identical the instant this returns; the id has to be fresh
    random rather than content-derived precisely because of that (forking the
    same card twice must not collide). Forking a fork produces a sibling, not
    a grandchild: `fork.of` always names the root original, flattened.
    """
    idx = _shared.index()
    parent = _shared.require(idx, card_id)
    parent_path = idx.root / parent.filename
    try:
        _outer, parent_data = edit.read_card(parent_path)
    except edit.WriteError as exc:
        raise _shared.write_error(exc) from exc

    root = forks.root_fragment(parent_data, parent.fragment)

    fresh_id = forks.generate_card_id()
    for _ in range(5):
        if id_fragment(fresh_id) not in idx.fragments():
            break
        fresh_id = forks.generate_card_id()
    else:
        raise HTTPException(status_code=500, detail="could not mint a unique fork id")

    data = copy.deepcopy(parent_data)
    extensions = data.get("extensions")
    extensions = dict(extensions) if isinstance(extensions, dict) else {}
    jai = dict(extensions.get("jai") or {})
    jai["id"] = fresh_id
    jai["sourceKind"] = "fork"
    extensions["jai"] = jai
    data["extensions"] = extensions

    payload = {"spec": "chara_card_v3", "spec_version": "3.0", "data": data}
    payload.update(data)  # V2-compat top-level mirror, same as intake._envelope
    forks.stamp(payload, gallery_id=parent.gallery_id, root=root, of_filename=parent.filename)

    try:
        path = deps.png_writer.write_payload(
            payload,
            parent_path.read_bytes(),
            creator=str(data.get("creator") or ""),
            name=str(data.get("name") or parent.name),
            card_id=fresh_id,
            normalize=False,  # the parent's pixels, already processed once
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"cannot write the fork: {exc}") from exc

    catalog.index().refresh()
    logger.info("fork: %s -> %s", parent.filename, path.name)
    return get_character(path.name)


@router.put("/characters/{card_id}", response_model=CardDetailOut, summary="Replace a card's contents")
def put_character(
    card_id: str,
    body: CardWriteIn,
    if_match: str | None = Header(None, alias="If-Match"),
) -> CardDetailOut:
    """Rewrite the card embedded in this PNG, leaving its pixels untouched.

    The card is re-embedded through `pngtools.embed_card`, which rewrites only
    the tEXt chunks -- so a pngquant-compressed avatar survives an edit byte for
    byte, and a card edited here is shaped exactly like a freshly built one.

    The filename does not change when the name does. It is `<slug(name)>_<id8>`,
    a derived label rather than the name itself, and it is what the frontend,
    the DOM, the thumbnail cache and every URL key on; moving it mid-edit would
    404 the card the user is looking at. It already diverges for every card with
    punctuation in its name, and the id8 fragment -- which is the archive's
    dedupe key, on its own, never the name -- is unaffected either way.
    """
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    path = idx.root / record.filename
    _check_precondition(path, if_match)

    incoming = body.card
    name = incoming.get("name")
    if not isinstance(name, str) or not name.strip():
        # The name is the one field with no sane default: it is what the card is
        # called everywhere, and an empty one turns a card into an unfindable
        # blank in the grid.
        raise HTTPException(status_code=422, detail="the card must have a non-empty `name`")

    try:
        _, existing = edit.read_card(path)
        edit.patch_card(path, edit.merge_card(existing, incoming))
    except edit.WriteError as exc:
        raise _shared.write_error(exc) from exc

    # The client is about to read its own write, and the index never rescans on
    # its own.
    catalog.index().refresh()
    return get_character(record.filename)


@router.post(
    "/characters/{card_id}/favorite",
    response_model=FavoriteOut,
    summary="Star or unstar a card",
)
def set_favorite(card_id: str, body: FavoriteIn) -> FavoriteOut:
    """Flip `extensions.fav` on one card.

    The one place a targeted write beats this API's whole-document rule. That
    rule exists to stop *ambiguous partial prose updates* -- two clients each
    holding half a card and each sure it owns the field it is writing. A star is
    none of that: one boolean, no field to conflict over, flipped from a grid
    tile by someone who has not read the card at all. Routing it through `PUT`
    would mean fetching a 1.2 MB detail payload and sending it back with an
    `If-Match`, per click.

    It lives in the card rather than in this server's settings so it survives
    export -- the star is on the PNG, and comes back with it.
    """
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    path = idx.root / record.filename
    try:
        _, data = edit.read_card(path)
        extensions = data.get("extensions")
        extensions = dict(extensions) if isinstance(extensions, dict) else {}
        extensions["fav"] = body.value
        data["extensions"] = extensions
        edit.patch_card(path, data)
    except edit.WriteError as exc:
        raise _shared.write_error(exc) from exc
    catalog.index().refresh()
    return FavoriteOut(id=record.filename, favorite=body.value)


@router.delete("/characters/{card_id}", response_model=DeletedOut, summary="Bin a card")
def delete_character(
    card_id: str,
    gallery_action: Literal["keep", "delete"] = Query(
        "keep",
        alias="gallery",
        description="`delete` bins the card's gallery folder along with it. Default keeps it -- images are often the expensive half and are not always re-downloadable.",
    ),
) -> DeletedOut:
    """Move a card into the bin. Nothing here unlinks anything.

    See `proxy.cards.edit` for why: this is the one archive operation with no undo
    and no second copy, and `data/.trash/` costs disk where the alternative costs
    the card. Emptying the bin is a separate, deliberate act.
    """
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    path = idx.root / record.filename

    try:
        binned_gallery = (
            edit.trash_gallery(record.name, record.gallery_id)
            if gallery_action == "delete"
            else None
        )
        binned_card = edit.to_trash(path)
    except edit.WriteError as exc:
        raise _shared.write_error(exc) from exc

    # A freed filename can be taken by a future card, and the avatar cache has no
    # staleness check -- leaving the thumb behind would show the deleted card's
    # face on its replacement.
    _shared.thumbnail_store.forget(record.filename)
    if binned_gallery is not None:
        _shared.thumbnail_store.forget_gallery(binned_gallery[0])
    catalog.index().refresh()

    return DeletedOut(
        id=record.filename,
        card=str(binned_card),
        gallery=str(binned_gallery[1]) if binned_gallery else None,
    )


@router.post("/characters/bulk-delete", response_model=BulkDeleteOut, summary="Bin many cards at once")
def bulk_delete(body: BulkDeleteIn) -> BulkDeleteOut:
    """The batch-select grid's only bulk action today -- bin a selection in one
    pass instead of N single-card requests from the client.

    Same shape as `bulk_tags`: each card bins independently and partial success
    is reported, not rolled back, since there is no undo for a bin that half
    landed either.
    """
    idx = _shared.index()
    deleted: list[str] = []
    failed: dict[str, str] = {}
    for card_id in body.ids:
        record = idx.get(card_id)
        if record is None:
            failed[card_id] = "no such card in the archive"
            continue
        path = idx.root / record.filename
        try:
            binned_gallery = (
                edit.trash_gallery(record.name, record.gallery_id)
                if body.gallery == "delete"
                else None
            )
            edit.to_trash(path)
        except edit.WriteError as exc:
            failed[record.filename] = str(exc)
            continue

        _shared.thumbnail_store.forget(record.filename)
        if binned_gallery is not None:
            _shared.thumbnail_store.forget_gallery(binned_gallery[0])
        deleted.append(record.filename)

    if deleted:
        catalog.index().refresh()
    return BulkDeleteOut(deleted=deleted, failed=failed)


@router.put("/characters/{card_id}/avatar", response_model=CardDetailOut, summary="Replace a card's image")
def put_character_avatar(
    card_id: str,
    image: UploadFile = File(description="The new image. Anything Pillow opens -- png, jpeg, webp."),
    if_match: str | None = Header(None, alias="If-Match"),
) -> CardDetailOut:
    """Give a card new pixels, keeping every field of the embedded card.

    The only write that re-encodes, and it runs intake's pipeline -- normalize,
    crop a detected panel stack, cap the longest side, quantize -- so a card
    whose image was replaced is indistinguishable from one that arrived that way.
    """
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    path = idx.root / record.filename
    _check_precondition(path, if_match)

    payload = image.file.read()
    if not payload:
        raise HTTPException(status_code=422, detail="the uploaded image is empty")
    try:
        edit.replace_avatar(path, payload)
    except edit.WriteError as exc:
        raise _shared.write_error(exc) from exc

    # Same filename, different pixels: without this the cached thumb is the old
    # face, served indefinitely, since the avatar cache has no staleness check.
    _shared.thumbnail_store.forget(record.filename)
    catalog.index().refresh()
    return get_character(record.filename)


@router.post(
    "/characters/{card_id}/avatar/refetch",
    response_model=CardDetailOut,
    summary="Re-download a card's original image",
)
def refetch_character_avatar(
    card_id: str,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> CardDetailOut:
    """Replace the card's image with its source's current full-resolution one,
    keeping every field of the embedded card -- the repair for a thumbnail
    saved in place of the portrait.

    Replaces only on a clear improvement: a failed download, a non-image, a
    thumbnail-sized source or one no larger than the card's image answers 422
    and leaves the card untouched.
    """
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    path = idx.root / record.filename
    _check_precondition(path, if_match)

    try:
        avatar_refetch.refetch(path)
    except avatar_refetch.RefetchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except edit.WriteError as exc:
        raise _shared.write_error(exc) from exc

    _shared.thumbnail_store.forget(record.filename)
    catalog.index().refresh()
    # With the response, so the new ETag comes back and the page's next edit
    # carries a current precondition.
    return get_character(record.filename, response)


@router.post("/characters/tags", response_model=BulkTagsOut, summary="Add and remove tags across many cards")
def bulk_tags(body: BulkTagsIn) -> BulkTagsOut:
    """Apply one tag change to a selection, in a single pass.

    Removals are matched case-insensitively and additions preserve the case they
    arrive in, which is how the archive's tags actually behave -- `Female` and
    `female` are the same tag written twice, and a bulk cleanup exists largely to
    collapse pairs like that.

    Partial success is reported, not rolled back. There is no transaction across
    3,000 PNG rewrites, and pretending otherwise by unwinding the ones that
    worked would turn one unreadable card into a no-op for the other 2,999.
    """
    if not body.add and not body.remove:
        raise HTTPException(status_code=422, detail="give at least one tag to add or remove")

    idx = _shared.index()
    drop = {t.casefold() for t in body.remove if t.strip()}
    add = [t.strip() for t in body.add if t.strip()]

    changed = unchanged = 0
    failed: dict[str, str] = {}
    for card_id in body.ids:
        record = idx.get(card_id)
        if record is None:
            failed[card_id] = "no such card in the archive"
            continue
        path = idx.root / record.filename
        try:
            _, data = edit.read_card(path)
            current = [t for t in data.get("tags", []) if isinstance(t, str)]
            kept = [t for t in current if t.casefold() not in drop]
            present = {t.casefold() for t in kept}
            for tag in add:
                if tag.casefold() not in present:
                    kept.append(tag)
                    present.add(tag.casefold())
            if kept == current:
                unchanged += 1
                continue
            data["tags"] = kept
            edit.patch_card(path, data)
            changed += 1
        except edit.WriteError as exc:
            failed[record.filename] = str(exc)

    if changed:
        catalog.index().refresh()
    return BulkTagsOut(changed=changed, unchanged=unchanged, failed=failed)


@router.post("/characters/have", response_model=CharactersHaveOut, summary="Which of these provider card ids are already in the archive")
def characters_have(body: CharactersHaveIn) -> CharactersHaveOut:
    """The `/api/v1` peer of `POST /existing` (`proxy/api/capture.py`) -- same
    id-fragment match, exposed here so Discover (UI_REWRITE_PLAN.md §3.8)
    doesn't have to reach into the userscript's own route namespace for the
    pre-import duplicate guard.

    Answered from `catalog.index()`'s in-memory fragment dict rather than
    `deps.png_writer.existing` -- that one globs the filesystem per id, which
    is fine for its actual caller (a rare bulk-export skip check) but not for
    a route that could be asked about hundreds of ids. Discover itself no
    longer calls this at all (see `/characters/have-fragments`); this stays
    for any other point check."""
    idx = catalog.index()
    have = {card_id for card_id in body.ids if idx.by_fragment(id_fragment(card_id))}
    return CharactersHaveOut(have=sorted(have))


@router.post("/tags/apply", response_model=TagsApplyOut, summary="Apply a tag rename/removal plan across the archive")
def apply_tags(body: TagsApplyIn) -> TagsApplyOut:
    """Apply a literal `{rename, remove}` plan to every card in the archive.

    The plan is resolved client-side by the vendored tag-tools JS (`buildBuckets`
    / `buildApplyPayload`) against a dictionary the user curated in the tag
    manager -- this route makes no matching decisions of its own, only literal
    string equality, which is what keeps "what you previewed is what lands on
    disk" true. See docs/PHASE_5_TAGS_PLAN.md §5.

    Different job from `POST /characters/tags`: that one adds/removes one tag
    over a selection the caller names; this one applies a whole rename/removal
    map over the entire archive in a single pass.

    Same partial-success contract as the bulk route: no rollback, and
    re-posting the same plan is a no-op (every card already reflects it, so
    `changed` reports 0).
    """
    if not body.rename and not body.remove:
        raise HTTPException(status_code=422, detail="give at least one rename or removal")

    idx = _shared.index()
    drop = {t for t in body.remove if t.strip()}
    rename = {k: v for k, v in body.rename.items() if k.strip() and v.strip()}

    changed = unchanged = 0
    failed: dict[str, str] = {}
    for record in idx.cards():
        path = idx.root / record.filename
        try:
            _, data = edit.read_card(path)
            current = [t for t in data.get("tags", []) if isinstance(t, str)]

            mapped: list[str] = []
            for tag in current:
                if tag in drop:
                    continue
                mapped.append(rename.get(tag, tag))

            # Two tags renaming onto the same canonical -- or a rename landing
            # on a tag the card already carries -- must collapse to one,
            # case-insensitively, keeping the first occurrence's casing.
            deduped: list[str] = []
            seen: set[str] = set()
            for tag in mapped:
                key = tag.casefold()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(tag)

            if deduped == current:
                unchanged += 1
                continue
            data["tags"] = deduped
            edit.patch_card(path, data)
            changed += 1
        except edit.WriteError as exc:
            failed[record.filename] = str(exc)

    if changed:
        catalog.index().refresh()
    return TagsApplyOut(changed=changed, unchanged=unchanged, failed=failed)


@router.get("/characters/{card_id}/png", summary="Download the card PNG")
def get_character_png(card_id: str, request: Request) -> Response:
    """The card file itself, bytes for byte -- the V3 tEXt chunks intact, so what
    comes out of here imports into SillyTavern directly. This is the single-card
    export; no re-encoding happens anywhere in the path, because re-encoding is
    what would strip the card."""
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    return _shared.serve_file(
        idx.root / record.filename,
        media_type="image/png",
        request=request,
        download_as=record.filename,
    )


@router.get("/characters/{card_id}/expressions.zip", summary="Download one character's expressions as a zip")
def get_character_expressions_zip(card_id: str) -> Response:
    """Every file in this card's expressions folder, flattened to basenames at
    the zip root -- the shape SillyTavern's *Import Expressions Pack* button
    expects (docs/FORKS_AND_EXTRAS_PLAN.md §2). Never embedded in the card PNG
    itself or in a bundle export: a character's expressions run an order of
    magnitude bigger than everything else archived for it."""
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    meta = _folder_out(record, settings.expressions_dir)
    if not meta.exists:
        raise HTTPException(status_code=404, detail=f"no expressions folder for {card_id!r}")
    data = expressions.zip_one(settings.expressions_dir / meta.folder)
    headers = {"Content-Disposition": _shared.content_disposition(f"{record.name} expressions.zip")}
    return Response(content=data, media_type="application/zip", headers=headers)


@router.get("/characters/{card_id}/thumb", summary="Card thumbnail")
def get_character_thumb(
    card_id: str,
    request: Request,
    size: int | None = Query(
        None,
        ge=48,
        le=1024,
        description="Thumbnail height in pixels; the 2:3 grid aspect is fixed. Omit for the inherited 96x144 cache, which covers the whole archive already and costs nothing to serve.",
    ),
) -> Response:
    """A ~10 KB derivative of the card, generated on a cache miss.

    Falls back to the full PNG when a thumb cannot be made -- a card too broken
    to render still has to appear in the grid, since being visible is how it gets
    noticed and fixed.
    """
    idx = _shared.index()
    record = _shared.require(idx, card_id)
    thumb = _shared.thumbnail_store.avatar(record.filename, size=size)
    if thumb is None:
        logger.info("thumbs: falling back to the full PNG for %s", record.filename)
        return _shared.serve_file(idx.root / record.filename, media_type="image/png", request=request)
    return _shared.serve_file(
        thumb.path,
        # Sniffed from the file's magic number, never its extension: the
        # inherited cache is JPEG data behind a `.png` name. See proxy/archive/thumbs.py.
        media_type=thumb.media_type,
        request=request,
        cache_control=_shared.THUMB_CACHE_CONTROL,
    )


