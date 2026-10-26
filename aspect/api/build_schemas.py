"""Wire shapes for the acquisition endpoints -- the two userscripts' half of the
server (`/build-*`, `/existing`, `/lorebooks/existing`, `/v1/chat/completions`).

Request models describe what a browser posts; `BuildResponse` is what every
build path returns. The card types these map onto live in `aspect.cards.models`.

Distinct from `aspect.api.schemas`, which is the archive's own `/api/v1` browse
and edit contract.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# /v1/chat/completions
# ---------------------------------------------------------------------------


class ChatCompletionRequest(BaseModel):
    """Passthrough shape of whatever JanitorAI sends. Extra fields are kept
    verbatim so nothing is lost on the way to the responder."""

    model_config = {"extra": "allow"}

    model: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False


# ---------------------------------------------------------------------------
# /build-jai
# ---------------------------------------------------------------------------


class BuildCharacter(BaseModel):
    # The userscript no longer sends a name -- the server derives the real
    # character name from character_json (chat_name). `name` is kept only as an
    # optional fallback for the degenerate name-only build (no character_json).
    name: str = ""
    id: str | None = None
    url: str | None = None


class BuildLorebook(BaseModel):
    id: str
    raw: dict[str, Any] = Field(default_factory=dict)


class BuildRequest(BaseModel):
    character: BuildCharacter
    # The raw JanitorAI /hampter/characters/<id> JSON. None only for the
    # degenerate "name-only" build (no definition to map).
    character_json: dict[str, Any] | None = None
    avatar_url: str | None = None
    avatar_b64: str | None = None
    lorebooks: list[BuildLorebook] = Field(default_factory=list)


class BuildResponse(BaseModel):
    ok: bool
    # True when the card was already on disk and the build was skipped: `path`
    # then points at the file that was already there, and nothing was written.
    duplicate: bool = False
    path: str | None = None
    warnings: list[str] = Field(default_factory=list)
    fields_present: dict[str, bool] = Field(default_factory=dict)
    # filename/card: populated by /build-chub and /build-datacat, whose browser
    # callers no longer assemble the card themselves (they only capture raw
    # provider JSON) and so need it handed back -- filename to target post-import
    # steps at the right card, card to feed findCharacterMediaUrls/
    # findCharacterGalleryUrls for the import-summary modal. Unpopulated (None)
    # on /build and /build-saucepan, which predate this need.
    filename: str | None = None
    card: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# /build-saucepan -- the saucepan peer of /build. `character` is the thin JSON
# the saucepan userscript fetched straight from saucepan's API:
# {id, definition, companion, lorebooks}. The server does all deobfuscation and
# mapping (see sources.saucepan) and reuses the same CardBuilder -> PngWriter tail.
# ---------------------------------------------------------------------------


class SaucepanBuildRequest(BaseModel):
    character: dict[str, Any] = Field(default_factory=dict)
    avatar_url: str | None = None
    avatar_b64: str | None = None


# ---------------------------------------------------------------------------
# /build-chub -- the Chub peer of /build-saucepan. The browser posts the raw
# Chub API node (GET /api/characters/{fullPath}?full=true, CORS-open, fetched
# client-side same as browse) plus its linked lorebook JSON if the card has
# one (also fetched client-side via the git API). The server builds + cleans
# + writes through sources.chub -- never through CardBuilder/pydantic, per its
# raw-dict-passthrough rule.
# ---------------------------------------------------------------------------


class ChubBuildRequest(BaseModel):
    node: dict[str, Any] = Field(default_factory=dict)
    linked_lorebook: dict[str, Any] | None = None
    avatar_url: str | None = None
    avatar_b64: str | None = None
    # Set only on a "Replace Existing" duplicate-resolution import: the
    # gallery_id read off the card being replaced (before it's deleted), so
    # the archive's gallery folder link survives the swap instead of orphaning
    # under a freshly minted id.
    gallery_id: str | None = None


# ---------------------------------------------------------------------------
# /build-datacat -- the DataCat peer of /build-chub. The browser posts the raw
# /api/characters/:id detail payload (character), with hydrated
# `character.scripts[]` lorebook content. The server ports buildV2FromDatacat
# (sources.datacat) to a neutral V2 dict, then goes through CardBuilder like
# /build-jai and /build-saucepan -- unlike Chub, a datacat character carries
# no priority/probability/selectiveLogic or int position to lose in the
# pydantic round-trip.
# ---------------------------------------------------------------------------


class DatacatBuildRequest(BaseModel):
    character: dict[str, Any] = Field(default_factory=dict)
    avatar_url: str | None = None
    avatar_b64: str | None = None
    # Set only on a "Replace Existing" duplicate-resolution import -- see
    # ChubBuildRequest.gallery_id.
    gallery_id: str | None = None


# ---------------------------------------------------------------------------
# /existing -- "which of these card ids do we already have on disk?" Lets a
# bulk export skip cards already saved before the slow per-card fetch loop.
# ---------------------------------------------------------------------------


class ExistingRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


class ExistingResponse(BaseModel):
    existing: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# /lorebooks/existing -- "which of these lorebook ids do we already have
# cached?" A lorebook is reused across many characters and fetching one is the
# slow part of an export (saucepan needs one request per chapter), so the
# userscript asks this first and only fetches the misses; the cached ones ride
# into the build by id (see LorebookCache).
# ---------------------------------------------------------------------------


class LorebookExistingRequest(BaseModel):
    source: str
    ids: list[str] = Field(default_factory=list)


class LorebookExistingResponse(BaseModel):
    cached: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
