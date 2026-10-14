"""Response schemas for `/api/v1`.

Separate from `proxy/cards/models.py`, which holds the request/response bodies of the
capture-and-build endpoints the userscripts talk to. Those are an internal
protocol between two halves of one tool; these are the archive's public contract,
consumed by a frontend that is versioned independently, and mixing the two would
make an incidental change to a build payload look like a breaking API change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class CardOut(BaseModel):
    """A card as the browse grid sees it: metadata, counts, and no prose.

    `description_chars` and the `has_*` flags are here instead of the text itself
    so a list of 3,839 cards stays small enough to send whole -- they answer "is
    this card substantial?" and "what is missing from it?", which is what a grid
    and a data-quality pass actually ask.
    """

    id: str = Field(description="The card's filename on disk, e.g. `Abbie_0d162f5f.png`.")
    name: str
    creator: str
    page_name: str = Field(description="The source page's title blurb, e.g. `Offer You Can't Refuse | Abbie`.")
    tags: list[str]
    source_kind: str = Field(description="Which importer wrote it: janitor_core, chub_import, datacat_import, saucepan_core, jannyai_import.")
    source_url: str
    card_id: str = Field(description="The source's own id for the character.")
    fragment: str = Field(description="The `_<id8>` slice of card_id that the filename carries.")
    gallery_id: str
    character_version: str
    greetings: int = Field(description="Primary greeting plus alternates.")
    lore_entries: int
    description_chars: int
    prompt_chars: int = Field(
        description="Characters across description, personality, scenario, first_mes and system_prompt -- the card's prompt weight. Roughly four characters to a token."
    )
    has_creator_notes: bool
    has_example_dialogue: bool
    favorite: bool = Field(
        default=False,
        description="Starred, out of the card's own `extensions.fav` -- so it travels with the PNG and round-trips through SillyTavern rather than living in this server's settings.",
    )
    is_fork: bool = Field(
        default=False,
        description="Whether the card carries an `extensions.fork` block -- for the tile badge and the Forks filter chip, without paying for the whole `extensions` blob on a list request.",
    )
    size: int = Field(description="Card PNG size in bytes.")
    modified: datetime = Field(
        description="The file's mtime. Says when the card was last *written*, which on this archive is dominated by bulk repair passes -- see `linked_at` for when it arrived."
    )
    linked_at: str = Field(
        default="",
        description="When this card was acquired, stamped by the importer into `extensions.jai.linkedAt`. Present on every card in the archive and the only trustworthy 'date added': mtimes were flattened by the bulk passes. A raw ISO-8601 string, passed through exactly as the card carries it.",
    )
    create_date: str = Field(
        default="",
        description="When the card was created, as SillyTavern means it: the root-level `create_date` the card carries, or the earliest provider `linkedAt` it is derived from for a card written before this archive stamped the field. Distinct from `linked_at`, which is `extensions.jai`'s stamp and is rewritten by bulk passes -- this one is stable. Raw ISO-8601, or empty for a card with no provenance at all.",
    )
    thumb_url: str
    png_url: str
    extensions: dict[str, Any] | None = Field(
        default=None,
        description="The card's `data.extensions` verbatim, only when asked for with `include=extensions`. Off by default: it is ~790 bytes a card, which doubles a whole-archive listing.",
    )
    error: str | None = Field(
        default=None,
        description="Why this card could not be parsed. Null for healthy cards; a card with an error has only its file fields filled in.",
    )


class FavoriteIn(BaseModel):
    """The one targeted write in the API. See `set_favorite` for why a single
    boolean does not go through the whole-document `PUT`."""

    value: bool = Field(description="True to star the card, false to unstar it.")


class FavoriteOut(BaseModel):
    id: str
    favorite: bool


class CardListOut(BaseModel):
    """A page of cards. `total` is the size of the filtered set, not the archive,
    so a client can page without asking twice."""

    total: int
    limit: int
    offset: int
    items: list[CardOut]


class GalleryOut(BaseModel):
    """A character's media folder on disk -- its image gallery, or (on
    `CardDetailOut.expressions`) its expression sprites. Both are folders
    named the same way and measured the same way, just under different roots.

    `folder` is computed from the card's current name plus its gallery_id -- the
    convention CharacterLibrary uses -- so `exists: false` with a non-empty
    folder means either no images were ever downloaded or the card was renamed
    out from under its folder. See `proxy.cards.gallery.folder_name`.
    """

    gallery_id: str
    folder: str
    exists: bool
    images: int
    bytes: int


class GalleryFileOut(BaseModel):
    """One file in a gallery folder. `kind` is sniffed from the extension rather
    than the bytes: a gallery holds images, video and audio side by side, and the
    client needs to know which element to render before it fetches anything."""

    name: str
    kind: str = Field(description="image, video, audio, or other.")
    size: int
    modified: datetime
    url: str
    thumb_url: str | None = Field(
        default=None,
        description="Null for files that cannot be thumbnailed -- video, audio, anything Pillow will not open.",
    )


class GalleryFilesOut(BaseModel):
    folder: str
    total: int
    bytes: int
    items: list[GalleryFileOut]


class GalleryBulkDeleteIn(BaseModel):
    """Bin many files out of one gallery (or expressions) folder in one pass --
    the gallery pane's batch-select delete."""

    names: list[str] = Field(description="Filenames within the folder, as the folder listing reports them.")


class GalleryBulkDeleteOut(BaseModel):
    """What a bulk gallery delete did. Same partial-success shape as
    `BulkDeleteOut` -- one bad name must not block the files that bin cleanly."""

    deleted: list[str] = Field(default=[], description="Filenames that were binned.")
    failed: dict[str, str] = Field(
        default={},
        description="Filename to the reason it could not be binned.",
    )


class GalleryFolderOut(BaseModel):
    """A gallery folder as the orphan sweep sees it: the name on disk, and the
    cards that claim it -- empty when nothing does, which is what makes it an
    orphan. A list, not one card, because a fork shares its parent's
    `gallery_id` by design (docs/FORKS_AND_EXTRAS_PLAN.md §3): two or more
    cards legitimately claiming the same folder is normal, not a data error.
    See `scripts/repair_galleries.py`."""

    folder: str
    card_ids: list[str]


class ForkParentOut(BaseModel):
    """The original a fork points at, resolved to what it's called right now
    -- `fork.of` is a fragment, not a filename, so this is a live lookup, not
    a copy of what the fork was told at creation time."""

    id: str
    name: str


class CardDetailOut(CardOut):
    """One card in full: the summary plus the entire embedded V3 card.

    `card` is the card's `data` object verbatim, straight off the PNG rather than
    out of the index -- descriptions, greetings, lorebook, every extension block.
    Unrewritten on purpose: an archive's detail view should show what is actually
    embedded, not this server's interpretation of it.
    """

    spec: str
    spec_version: str
    card: dict[str, Any]
    gallery: GalleryOut
    expressions: GalleryOut = Field(
        description="Same shape as `gallery`, measured against data/expressions/ instead."
    )
    expressions_zip_url: str | None = Field(
        description="GET this to download the character's expressions as a flat zip. Null when `expressions.exists` is false -- the client must never build this path itself."
    )
    forked_from: ForkParentOut | None = Field(
        default=None,
        description="The card `extensions.fork.of` resolves to right now, when this card is a fork. Null both for a card that isn't a fork and for a fork whose original has since been deleted -- the UI shows the latter as 'original no longer in archive', not as an error.",
    )


class CardWriteIn(BaseModel):
    """A replacement card body.

    `card` is the V3 `data` object whole -- name, prose, greetings, tags,
    lorebook, extensions -- not a patch. A client holds the complete card and
    treats itself as its owner, and a partial update could never express a
    *cleared* field, which clearing a scenario is. The two extension keys the
    archive owns (`gallery_id` and `jai`) are carried over when the payload
    omits them; see `proxy.cards.edit.merge_card`.

    Nothing here is re-sanitized. Intake cleans a card once on the way in; a
    human typing `{{char}}` into an edit box means it, and rewriting what they
    just confirmed in a diff would make that diff a lie.
    """

    card: dict[str, Any] = Field(description="The card's `data` object, complete.")


class BulkTagsIn(BaseModel):
    """A tag change applied across many cards in one pass.

    Bulk because it is one of the archive's five jobs and the only one that is
    inherently plural: doing it as N single-card writes re-reads and rewrites N
    PNGs from the client, one round trip each, with no way to report a partial
    failure coherently.
    """

    ids: list[str] = Field(description="Card filenames, as `/characters` reports them.")
    add: list[str] = Field(default=[], description="Tags to add, skipped where already present.")
    remove: list[str] = Field(default=[], description="Tags to remove, matched case-insensitively.")


class BulkTagsOut(BaseModel):
    """What a bulk tag pass did. `changed` counts cards actually rewritten --
    a card that already carried every tag being added is `unchanged`, not
    failed, and is not rewritten just to bump its mtime."""

    changed: int
    unchanged: int
    failed: dict[str, str] = Field(
        default={},
        description="Card id to the reason it could not be written. Partial success is normal and reported rather than rolled back: the cards that worked stay worked.",
    )


class TagsApplyIn(BaseModel):
    """A literal, corpus-wide tag rename/removal plan -- the apply half of the
    tag manager (see docs/PHASE_5_TAGS_PLAN.md). The plan is resolved client-side
    (vendored JS owns tag-matching semantics; the server makes no decisions of
    its own here), so every key is an exact string match against a card's tags,
    applied over every card in the archive rather than a selected subset -- the
    different job `BulkTagsIn` covers.
    """

    rename: dict[str, str] = Field(
        default={}, description="Exact card tag -> exact canonical it becomes."
    )
    remove: list[str] = Field(default=[], description="Exact card tags to drop.")


class TagsApplyOut(BaseModel):
    """What a plan apply did. Same partial-success contract as `BulkTagsOut`:
    no rollback across a corpus-wide rewrite, so what worked stays worked."""

    changed: int
    unchanged: int
    failed: dict[str, str] = Field(
        default={},
        description="Card id to the reason it could not be written. Partial success is normal and reported rather than rolled back.",
    )


class DuplicateMemberOut(BaseModel):
    """One card inside a candidate duplicate group -- enough to render a
    compare tile without a second request per card. Deliberately not the
    full `CardDetailOut`: a group review doesn't need the embedded prose,
    only what already distinguishes the members (and their thumbs)."""

    id: str = Field(description="The card's filename on disk, e.g. `Olivia_3725810.png`.")
    name: str
    page_name: str
    tags: list[str]
    description_chars: int
    character_version: str
    create_date: str
    thumb_url: str
    png_url: str


class DuplicatePairOut(BaseModel):
    """Why two members of a group were paired, in enough detail for a human
    to judge it rather than trust it blindly -- this is a bounded heuristic,
    not a verdict."""

    a: str
    b: str
    avatar_distance: int | None = Field(
        description="Hamming distance between the two avatars' 256-bit average-hash, or null if either thumb could not be read."
    )
    name_score: float = Field(description="difflib ratio over the casefolded names, 0-1.")
    text_score: float = Field(
        description="Best of description/first_mes/creator_notes difflib ratio, 0-1."
    )
    strength: Literal["strong", "weak"] = Field(
        description="'strong' when the avatar alone proves it or the name match is backed by real text overlap; 'weak' when only the name matches -- e.g. two different characters sharing a recurring name."
    )
    reasons: list[str] = Field(description="Human-readable match evidence, e.g. 'identical avatar', '92% text overlap'.")


class DuplicateGroupOut(BaseModel):
    """A cluster of a creator's cards that a scan thinks might be the same
    character. Always one creator -- cards from different creators are never
    compared, let alone grouped."""

    group_id: str = Field(description="Stable across scans as long as membership doesn't change -- derived from the sorted member filenames.")
    creator: str
    members: list[DuplicateMemberOut]
    pairs: list[DuplicatePairOut]


class DuplicatesOut(BaseModel):
    """The result of a full-archive duplicate scan."""

    groups: list[DuplicateGroupOut]
    scanned: int = Field(description="Cards considered -- i.e. belonging to a creator with 2+ cards.")


class CardImportOut(BaseModel):
    """What adopting an uploaded card PNG did.

    `duplicate` is a success, not a failure: a card whose id fragment is already
    in the archive is left exactly as it is and its filename reported, the same
    answer the `/build-*` routes give when a click re-acquires something already
    saved. The caller wanted the card present, and it is.
    """

    id: str = Field(description="The card's filename in the archive -- its id everywhere in the API.")
    name: str
    creator: str = ""
    source: str = Field(
        description="Where the card was judged to come from: `archive` for one of our own being re-adopted, else `chub`/`datacat`/`jannyai`/`png`."
    )
    duplicate: bool = Field(
        default=False, description="The fragment was already on disk and nothing was written."
    )
    overwritten: bool = Field(
        default=False, description="An existing card of the same id was replaced in place."
    )
    warnings: list[str] = Field(default=[])


class CharactersHaveIn(BaseModel):
    """`POST /characters/have` body -- provider card ids (Chub project ids,
    DataCat character ids, ...), not archive filenames."""

    ids: list[str] = Field(default_factory=list)


class CharactersHaveOut(BaseModel):
    """Which of the submitted provider ids already have a card on disk.
    UI_REWRITE_PLAN.md §3.8 -- Discover's "hide cards I have" and its
    pre-import duplicate guard both key off this, the same `_<id8>` fragment
    match `POST /existing` (the userscript's own route) already answers
    from."""

    have: list[str] = Field(default_factory=list)


class CharactersHaveFragmentsOut(BaseModel):
    """Every `_<id8>` fragment currently on disk.

    Fetched once by Discover instead of `POST /characters/have` per
    id-list-so-far: matching happens client-side against this set (the same
    fragment derivation, ported to TypeScript), which is what the id list
    grows into every scroll would otherwise force a fresh round trip for. Not
    paged or filtered -- a few thousand short strings, cheap to send whole."""

    fragments: list[str] = Field(default_factory=list)


class DeletedOut(BaseModel):
    """Where a delete put things. Paths, not booleans, because the whole point
    of binning rather than unlinking is that someone can go and get it back."""

    id: str
    card: str = Field(description="Where the card PNG landed under the bin.")
    gallery: str | None = Field(
        default=None,
        description="Where the gallery folder landed, or null when it was kept or was not there.",
    )


class BulkDeleteIn(BaseModel):
    """Bin many cards in one pass -- the batch-select grid's bulk delete."""

    ids: list[str] = Field(description="Card filenames, as `/characters` reports them.")
    gallery: Literal["keep", "delete"] = Field(
        default="keep",
        description="`delete` bins each card's gallery folder along with it. Default keeps them.",
    )


class BulkDeleteOut(BaseModel):
    """What a bulk delete did. Same partial-success shape as `BulkTagsOut` --
    one bad id must not block the cards that bin cleanly."""

    deleted: list[str] = Field(default=[], description="Card filenames that were binned.")
    failed: dict[str, str] = Field(
        default={},
        description="Card id to the reason it could not be binned.",
    )


class GalleryFileWrittenOut(BaseModel):
    """One uploaded gallery file. `path` is in SillyTavern's `user/images/...`
    shape because that is what the frontend's uploaders read back out of the
    reply and store as a local media path.

    `name` is the name on disk, which is not necessarily the name uploaded:
    every image is re-encoded to WebP on the way in, so the extension is
    swapped (the stem never is -- see `proxy.media.uploads`)."""

    folder: str
    name: str
    size: int
    path: str
    url: str
    replaced: bool = Field(
        default=False,
        description="True when this overwrote a file of the same name rather than adding one.",
    )


class MediaUploadSkippedOut(BaseModel):
    """One file a bulk upload did not store, and why -- in the uploader's own
    words, so the pane can name it. Skipping is per file rather than per
    request on purpose: a 90-sprite pack with two strays writes 88."""

    name: str
    reason: str


class MediaUploadOut(BaseModel):
    """The result of uploading one or more files to a media folder."""

    folder: str
    written: list[GalleryFileWrittenOut]
    skipped: list[MediaUploadSkippedOut]


class FacetValue(BaseModel):
    value: str
    count: int


class FacetsOut(BaseModel):
    """Every value the filters can take, with counts, over the whole archive.
    Computed on the unfiltered set so a filter UI does not shrink its own
    options as you use it."""

    tags: list[FacetValue]
    creators: list[FacetValue]
    sources: list[FacetValue]


class ThumbStatsOut(BaseModel):
    cached: int = Field(description="Avatar thumbs present in the cache.")
    missing: int = Field(description="Indexed cards with no thumb yet; generated on first request.")
    stale: int = Field(description="Thumbs whose card is gone -- prune with scripts/prune_thumbs.py.")


class IndexStatsOut(BaseModel):
    scanned: int
    parsed: int = Field(description="Cards re-read on the last refresh, i.e. new or changed.")
    unchanged: int
    removed: int
    seconds: float


class MediaExtStatOut(BaseModel):
    ext: str = Field(description="Lowercase file extension without the dot, e.g. 'png'. 'other' for anything unrecognized.")
    count: int
    bytes: int


class MediaDedupeOut(BaseModel):
    """Result of `POST /media/dedupe` -- exact-duplicate gallery files trashed.
    See `proxy/media/dedupe.py` for what counts as a safe duplicate."""

    folders_touched: int
    files_trashed: int
    bytes_freed: int
    unresolved: int = Field(
        description="Unreferenced files that weren't a byte-for-byte match of any file the manifest currently "
        "claims -- left alone rather than trashed."
    )


class MediaStatsOut(BaseModel):
    """Every file under the galleries directory, tallied by extension."""

    images: int
    bytes: int
    by_ext: list[MediaExtStatOut] = Field(description="Sorted by count, descending.")


class StatsOut(BaseModel):
    """The archive at a glance, and the health of the index behind it."""

    cards: int
    unreadable: int = Field(description="Cards present but unparseable. Listed at /api/v1/characters?health=broken.")
    bytes: int = Field(description="Card (.png) bytes on disk. See `media.bytes` for gallery images.")
    creators: int
    tags: int
    galleries: int = Field(description="Cards whose gallery folder exists on disk.")
    archive_dir: str
    thumbs: ThumbStatsOut
    index: IndexStatsOut
    media: MediaStatsOut


class MediaItemIn(BaseModel):
    """One URL for the media download route to fetch. `filename` is an
    extractor-supplied real name, when the caller has one -- it beats
    guessing from the URL, which is worthless for synthetic sources like
    `mega://folder/handle`."""

    url: str
    filename: str | None = Field(default=None, description="Extractor-supplied real filename, if known.")


class MediaDownloadIn(BaseModel):
    """`POST /characters/{id}/media` body -- docs/PHASE_3C_PLAN.md §3."""

    items: list[MediaItemIn]
    prefix: str = Field(
        default="localized_media",
        description="Filename prefix and dedupe-priority class: localized_media, lorebook_media, extgallery, or <provider>gallery.",
    )
    phase: str = Field(default="embedded", description="Label only, recorded in the manifest's run history.")


class MediaManifestFileOut(BaseModel):
    file: str
    sha256: str
    at: str
    # Recorded by the writer and summed by `GET /media/status`; without it here
    # the per-card read silently drops a field that is on disk.
    size: int | None = None


class MediaManifestDeadOut(BaseModel):
    reason: str
    attempts: int
    at: str


class MediaManifestRunOut(BaseModel):
    at: str
    phase: str
    saved: int
    skipped: int
    errors: int


class MediaManifestOut(BaseModel):
    """A gallery's `.media.json`, as-is -- the client renders it directly
    rather than the server reshaping it into a second view."""

    folder: str
    files: dict[str, MediaManifestFileOut]
    dead: dict[str, MediaManifestDeadOut]
    runs: list[MediaManifestRunOut]


class ThumbsPrunedOut(BaseModel):
    """`POST /galleries/{folder}/thumbs/prune` -- docs/PHASE_3C_PLAN.md §5."""

    folder: str
    removed: int


class MediaStatusEntryOut(BaseModel):
    """One card's row in `GET /media/status` -- a manifest summary, not the
    manifest itself (see `MediaManifestOut` for that)."""

    files: int
    bytes: int
    complete: bool
    dead: int
    last_run: str | None = None


class MediaStatusOut(BaseModel):
    """`GET /media/status` -- docs/PHASE_3C_PLAN.md §3. Cards with no gallery
    folder or no media run yet are simply absent, not zeroed entries: Bulk
    Localize treats "missing" and "never downloaded" as the same thing."""

    cards: dict[str, MediaStatusEntryOut]


class MediaBytesOut(BaseModel):
    """`POST /characters/{id}/media/bytes` response -- one item, not a batch,
    so no NDJSON framing (docs/PHASE_3C_PLAN.md §6, the browser-fetch door for
    MEGA/Pixiv)."""

    status: Literal["saved", "skipped", "error"]
    file: str | None = None


class MediaHaveIn(BaseModel):
    """`POST /characters/{id}/media/have` body -- the name-index half of
    `download_item`, asked ahead of time. Same items/prefix shape as the
    download routes so a caller can pass the identical list."""

    items: list[MediaItemIn]
    prefix: str = Field(
        default="extgallery",
        description="Filename prefix and dedupe-priority class the caller would save under.",
    )


class MediaHaveOut(BaseModel):
    """Which of the submitted URLs the gallery already satisfies: `have` maps
    those URLs to the local filename that covers them. URLs absent from the
    map still need fetching."""

    have: dict[str, str]


class MediaJobSubmitIn(BaseModel):
    """`POST /media/jobs` body -- the same items/prefix/phase shape as the
    synchronous `POST /characters/{id}/media`, but queued and run in the
    background instead of streamed over the request's own connection
    (docs/PHASE_3C_PLAN.md §7, "3C-2 -- the job runner").

    `discover=true` is UI_REWRITE_PLAN.md §3.4's alternative to an explicit
    `items` list: the server re-scans the card's own text for media URLs
    (the same walk `POST .../media/scan` previews) and downloads whatever it
    finds, in one job. `items` is ignored in that mode.

    `scope="all"` is Stage 6B's bulk localize: the same run over every card in
    the archive, sequentially, inside one job. It implies `discover` -- there is
    no per-card item list to supply -- and `items`/`card_id` are ignored."""

    scope: Literal["card", "all"] = Field(
        default="card",
        description='"card" downloads one card (card_id required); "all" walks the whole archive.',
    )
    card_id: str | None = Field(default=None, description='Required when scope is "card".')
    items: list[MediaItemIn] = Field(default_factory=list)
    discover: bool = Field(default=False, description="Scan the card server-side instead of taking an explicit item list.")
    skip_complete: bool = Field(
        default=True,
        description='scope="all" only: skip cards whose last run finished with no errors.',
    )
    prefix: str = Field(
        default="localized_media",
        description="Filename prefix and dedupe-priority class: localized_media, lorebook_media, extgallery, or <provider>gallery.",
    )
    phase: str = Field(default="embedded", description="Label only, recorded in the manifest's run history.")

    @model_validator(mode="after")
    def _card_id_required_for_card_scope(self) -> "MediaJobSubmitIn":
        if self.scope == "card" and not self.card_id:
            raise ValueError('card_id is required when scope is "card"')
        return self


class MediaScanSourceOut(BaseModel):
    """One gallery source on a card: an album page, a MEGA folder, a Civitai
    post, the card's own Chub gallery (`chub:<project id>`) -- or a URL nothing
    here can fetch.

    Reported rather than resolved. Resolving means an outbound request per
    source, and the scan is a preview the user is about to follow with a job
    that does the same work for real; a preview that can take seconds or fail
    is worse than one that says "and two galleries"."""

    url: str
    handler: str | None = Field(description="Extractor id, or null when nothing here handles this URL.")
    status: str = Field(description='"ready" or "unhandled".')


class MediaScanOut(BaseModel):
    """`POST /characters/{id}/media/scan` -- a dry run of the same discovery
    `discover=true` jobs use, so the UI can show "43 images found" before
    committing to a download. `embedded` and `lorebook` are already deduped
    against each other -- a URL in both surfaces is reported only in
    `embedded`.

    `sources` is why a card whose only media is a Civitai post no longer
    reports itself empty: its images are real but live behind a link, so the
    two URL lists are both empty and the count alone would say "nothing here"
    while the download would fetch forty images."""

    embedded: list[str]
    lorebook: list[str]
    sources: list[MediaScanSourceOut] = Field(default_factory=list)


class MediaJobOut(BaseModel):
    """`POST /media/jobs` response -- just enough to start polling."""

    job_id: str
    state: str
    total: int


class MediaJobEventOut(BaseModel):
    """One finished item, same shape as an NDJSON line from the synchronous route."""

    url: str
    status: Literal["saved", "skipped", "error"]
    file: str | None = None
    reason: str | None = None
    bytes: int | None = None


class MediaJobStatusOut(BaseModel):
    """`GET /media/jobs/{id}` -- poll this until `state` is `done`, `error`,
    or `cancelled`. `events` holds only items finished since the caller's
    last `after` cursor; `next_cursor` is what to pass next time so a poll
    loop doesn't re-send the whole history every tick."""

    job_id: str
    card_id: str | None = None
    phase: str
    prefix: str
    state: str
    total: int
    done: int
    saved: int
    skipped: int
    errors: int
    error: str | None = None
    events: list[MediaJobEventOut] = Field(default_factory=list)
    next_cursor: int = 0
    reason: str | None = None
    bytes: int | None = None

    # ---- Stage 6B: archive-wide runs ----------------------------------------
    scope: Literal["card", "all"] = "card"
    unit: Literal["items", "cards"] = Field(
        default="items",
        description='What total/done count. A scope="all" job counts cards; a single-card job counts URLs.',
    )
    cards_total: int = 0
    cards_done: int = 0
    cards_skipped: int = Field(
        default=0, description="Cards passed over because their last run was clean."
    )
    current_card_id: str | None = None
    events_dropped: int = Field(
        default=0,
        description="Events discarded by the ring buffer on a long run; the manifest, not this list, is the record of what happened.",
    )


class ProxyStatusOut(BaseModel):
    """`GET /proxy/status` -- is the configured outbound proxy actually carrying
    traffic, and what does the outside world see us as.

    `state` is what the UI's dot colours off:

    * `unset`     -- no proxy configured; grey. `direct_ip` is still filled in.
    * `ok`        -- the proxy leg worked and returned a *different* IP from the
                     direct leg; green. This is the only state that proves the
                     proxy is carrying traffic rather than merely existing.
    * `bypassed`  -- both legs worked and returned the same IP; amber. Either
                     the proxy is transparently forwarding from this same
                     address, or something is not routing through it.
    * `error`     -- the proxy leg failed; red, with `error` set.

    `url` is redacted -- the password is replaced with `***` before it leaves
    the server, since this response goes to a browser and into log lines.
    """

    configured: bool
    url: str | None = None
    state: Literal["unset", "ok", "bypassed", "error"]
    proxy_ip: str | None = None
    direct_ip: str | None = None
    latency_ms: int | None = None
    error: str | None = None


class UserscriptSpecOut(BaseModel):
    """One installable bridge, as the settings UI's picker sees it."""

    key: str = Field(description="`jai` or `saucepan` -- the path segment the generate call takes.")
    label: str
    site: str = Field(description="The site the script runs on, e.g. `janitorai.com`.")
    filename: str = Field(description="Suggested filename, and the name Tampermonkey shows.")
    description: str
    supports_tag_filter: bool = Field(
        description="Whether BULK_TAG_FILTER applies -- only the bridge that has a bulk sweep."
    )


class UserscriptRequest(BaseModel):
    """What to bake into a generated bridge.

    `server_url` is the *fallback* the script compiles in; Tampermonkey storage
    (`GM_setValue("serverUrl", ...)`) still overrides it at runtime. Omit it to
    keep the source default (127.0.0.1:8000).

    The two tag lists are omitted rather than empty to mean "leave the source
    defaults alone" -- an empty `include` is a real setting (export every card).
    """

    server_url: str | None = None
    include_tags: list[str] | None = None
    exclude_tags: list[str] | None = None


class UserscriptOut(BaseModel):
    """A compiled bridge, as text. Nothing is written to disk."""

    key: str
    filename: str
    source: str
    bytes: int


# ---------------------------------------------------------------------------
# Lorebooks -- derived from the cards, see proxy/archive/lorebooks.py
# ---------------------------------------------------------------------------


class LorebookCardOut(BaseModel):
    """A card that carries a lorebook -- enough for a tile and a link."""

    id: str = Field(description="The card's filename on disk.")
    name: str
    creator: str
    create_date: str
    thumb_url: str


class LorebookOut(BaseModel):
    """A lorebook as the browse list sees it: one row per distinct book,
    however many cards embed a copy."""

    id: str = Field(
        description="Derived from the provider's id where the importer stamped one, else from the entries themselves. Stable while the cards are."
    )
    name: str
    kinds: list[str] = Field(
        description="How its copies were recovered: `janitor` (split out by script id), `chub` (linked project) or `embedded` (no provider id, matched on content alone)."
    )
    refs: list[str] = Field(description="Provider identities, e.g. `jai:<script id>`, `chub:lorebooks/<creator>/<slug>`.")
    creators: list[str] = Field(description="Creators of the cards carrying it, most common first.")
    card_count: int
    entry_count: int = Field(description="Entry lines -- one per entry, however many versions it has.")
    revision_count: int = Field(description="Distinct entry-sets among the cards' copies.")
    changed_entries: int = Field(description="Entries with more than one version across the copies.")
    similar_count: int = Field(description="Other lorebooks sharing entries with this one -- merge candidates.")
    unresolved_entries: int = Field(description="Changed entries with no version chosen yet.")
    pending_cards: int = Field(
        description="Cards a sync would rewrite for this lorebook: those holding a version other than the chosen one, and those it is waiting to be attached to or detached from."
    )
    chars: int
    has_content: bool = Field(
        default=True,
        description="False for a lorebook known only from a provider's script list -- private, or never fetched. It has no entries and cannot be attached.",
    )
    listed_card_count: int = Field(
        default=0, description="Cards whose provider lists this lorebook but which do not embed it."
    )
    authors: list[str] = Field(
        default_factory=list, description="Who published the script, per the provider's listing. Not the card creators."
    )
    upstream_updated: str = Field(default="", description="The newest `updated_at` the provider reported for the script.")


class LorebookStatsOut(BaseModel):
    cards: int = Field(description="Cards carrying at least one lorebook entry.")
    lorebooks: int
    shared_lorebooks: int = Field(description="Lorebooks carried by more than one card.")
    entries_embedded: int = Field(description="Entries across every card's copy.")
    entries_unique: int = Field(description="Distinct entry versions.")
    changed_entries: int
    unresolved_entries: int
    pending_cards: int
    listed_only: int = Field(default=0, description="Lorebooks known only from a provider's script list, with no entries.")


class LorebooksOut(BaseModel):
    lorebooks: list[LorebookOut]
    stats: LorebookStatsOut


class LorebookRevisionOut(BaseModel):
    """One distinct entry-set of a lorebook, and the cards holding it. Ordered
    newest-first by the cards' creation dates -- a guess at recency, since
    entries carry no timestamp of their own."""

    fingerprint: str
    entry_count: int
    newest: str
    cards: list[LorebookCardOut]


class LorebookEntryOut(BaseModel):
    """An entry line in a list: its newest version's text, previewed."""

    id: str
    lorebook_id: str
    lorebook_name: str
    title: str
    keys: list[str]
    constant: bool
    preview: str
    chars: int
    version_count: int
    card_count: int
    resolved: bool = Field(description="One version, or one chosen -- nothing left to decide.")
    pending_cards: int = Field(default=0, description="Cards holding a version other than the chosen one.")


class LorebookSimilarOut(BaseModel):
    id: str
    name: str
    card_count: int
    entry_count: int
    shared: int
    relation: Literal["subset", "superset", "overlap", "title"] = Field(
        description="`subset`: this lorebook is wholly contained in the other. `superset`: the reverse. `title`: no shared entries (one side has none) but the same title from the same author."
    )


class LorebookMergeOut(BaseModel):
    """A merge the user declared, as the undo list shows it."""

    id: str
    a_name: str = ""
    b_name: str = ""
    at: str = ""


class LorebookDetailOut(LorebookOut):
    revisions: list[LorebookRevisionOut]
    listed_cards: list[LorebookCardOut] = Field(
        default_factory=list, description="Cards whose provider lists this lorebook but which do not embed it."
    )
    entries: list[LorebookEntryOut]
    similar: list[LorebookSimilarOut]
    merges: list[LorebookMergeOut] = Field(description="Manual merges that make up this lorebook.")
    attaching: list[LorebookCardOut] = Field(
        default_factory=list, description="Cards this lorebook will be attached to at the next sync."
    )
    detaching: list[LorebookCardOut] = Field(
        default_factory=list, description="Cards this lorebook will be taken off at the next sync."
    )


class LorebookEntryCandidateOut(BaseModel):
    """Another entry of the same lorebook that may be this one under a
    different title: the two never appear on the same card and their text is
    alike. A suggestion to review, not a finding."""

    id: str
    title: str
    preview: str
    similarity: float = Field(description="difflib ratio over the two entries' newest text, 0-1.")
    version_count: int
    card_count: int


class LorebookMergeIn(BaseModel):
    into: str = Field(description="The id of the lorebook (or entry) to merge with.")


class LorebookChooseIn(BaseModel):
    hash: str = Field(description="The version every card should carry -- one of the entry's own.")


class LorebookRefOut(BaseModel):
    id: str = Field(description="The id of the result. A merge changes ids, so follow this rather than the one sent.")


class LorebookChooseNewestOut(BaseModel):
    chosen: int = Field(description="Entries that had no version chosen and now have their newest one.")


class LorebookEntryVersionOut(BaseModel):
    hash: str
    title: str
    keys: list[str]
    secondary_keys: list[str]
    constant: bool
    content: str
    newest: str
    cards: list[LorebookCardOut]


class LorebookEntryDetailOut(BaseModel):
    id: str
    lorebook_id: str
    lorebook_name: str
    title: str
    versions: list[LorebookEntryVersionOut]
    chosen: str = Field(default="", description="Hash of the chosen version, or empty.")
    merges: list[LorebookMergeOut] = Field(default_factory=list)
    candidates: list[LorebookEntryCandidateOut] = Field(default_factory=list)


class LorebookEntriesOut(BaseModel):
    entries: list[LorebookEntryOut]
    total: int = Field(description="Matches before `limit`/`offset`.")


class CardLorebookOut(BaseModel):
    """One of the lorebooks a card's embedded book was assembled from -- or one
    waiting to be attached to it."""

    lorebook_id: str
    name: str
    kind: str
    entry_count: int
    card_count: int = Field(description="Cards carrying this lorebook, this one included.")
    revision: str = Field(description="Fingerprint of the copy this card holds.")
    is_newest: bool = Field(description="Whether that copy is the lorebook's newest revision.")
    revision_count: int
    state: Literal["attached", "attaching", "detaching", "listed"] = Field(
        default="attached",
        description="`attaching`: not on the card yet, will be after a sync. `detaching`: on the card, will be removed by one. `listed`: the provider lists it for this character but the card does not embed it (private, or never fetched).",
    )
    pending_upgrades: int = Field(
        default=0, description="Entries of this copy a sync would replace with their chosen version."
    )


class LorebookSyncCardOut(BaseModel):
    """What a sync would do to one card."""

    card: LorebookCardOut
    upgrades: int = Field(description="Entries that would be replaced by their chosen version.")
    duplicates: int = Field(
        default=0,
        description="Entries that would be removed: the card holds the chosen version already, beside this stale one.",
    )
    attach: list[str] = Field(description="Names of the lorebooks that would be attached.")
    detach: list[str] = Field(description="Names of the lorebooks that would be detached.")
    has_original: bool = Field(description="Whether this card's original lorebook is already saved from an earlier sync.")


class LorebookSyncPlanOut(BaseModel):
    cards: list[LorebookSyncCardOut]


class LorebookSyncIn(BaseModel):
    lorebook: str = Field(default="", description="Only this lorebook's changes. Empty for every lorebook.")
    cards: list[str] = Field(default_factory=list, description="Only these cards. Empty for every card.")


class LorebookSyncFailureOut(BaseModel):
    id: str
    name: str
    error: str


class LorebookSyncOut(BaseModel):
    synced: int = Field(description="Cards rewritten.")
    upgraded: int = Field(description="Entries replaced by their chosen version, across those cards.")
    removed: int = Field(default=0, description="Stale entries dropped from cards that already held the chosen version.")
    attached: int
    detached: int
    originals_saved: int = Field(description="Cards whose original lorebook was saved by this sync, being their first.")
    failed: list[LorebookSyncFailureOut] = Field(default_factory=list)


class LorebookOriginalOut(BaseModel):
    """The lorebook a card had before its first sync."""

    exists: bool
    taken_at: str = ""
    entry_count: int = 0
    differs: bool = Field(default=False, description="Whether the card's lorebook is no longer its original.")
