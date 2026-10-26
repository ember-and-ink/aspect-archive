"""Bookkeeping endpoints the userscripts call around an export run.

Two questions and two resets: which of these card ids are already on disk
(`/existing`, so a bulk export can skip them before the slow per-card loop),
which lorebooks are already cached (`/lorebooks/existing`, same idea -- fetching
a lorebook is the slow part of a saucepan export), and the two clear routes that
empty each store.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from aspect import deps
from aspect.api.build_schemas import (
    ExistingRequest,
    ExistingResponse,
    LorebookExistingRequest,
    LorebookExistingResponse,
)

logger = logging.getLogger("aspect.api.capture")

router = APIRouter()

@router.get("/capture-status")
async def capture_status(name: str) -> dict[str, Any]:
    return {"name": name, **deps.capture_store.status(name)}


@router.post("/clear-captures")
async def clear_captures() -> dict[str, Any]:
    removed = deps.capture_store.clear()
    return {"ok": True, "removed": removed}


@router.post("/lorebooks/existing")
async def lorebooks_existing(req: LorebookExistingRequest) -> LorebookExistingResponse:
    """Split the requested lorebook ids into the ones already cached (skip the
    fetch, reference by id in the build) and the ones missing (fetch, then send
    up -- the build endpoint caches them write-through)."""
    cached, missing = deps.lorebook_cache.split(req.source, req.ids)
    return LorebookExistingResponse(cached=cached, missing=missing)


@router.post("/clear-lorebooks")
async def clear_lorebooks() -> dict[str, Any]:
    removed = deps.lorebook_cache.clear()
    return {"ok": True, "removed": removed}


@router.post("/existing")
async def existing(req: ExistingRequest) -> ExistingResponse:
    """Report which of the given card ids are already saved on disk, so a bulk
    export can skip them before the slow one-at-a-time classify/build loop."""
    return ExistingResponse(existing=sorted(deps.png_writer.existing(req.ids)))
