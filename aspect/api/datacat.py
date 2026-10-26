"""`/api/v1/datacat` -- DataCat session transport for the browse/import UI.

DataCat (datacat.run) gates its REST API behind an anonymous session token and
a read-path allow-list; this router provides that surface directly from the
archive server (see aspect/datacat_client.py.DatacatSession for the port notes
and PHASE_3B_PLAN.md S2 for the design). Seven routes:

  health              -- probe
  dc-init             -- anonymous identify handshake -> session token
  dc-set-token        -- push a client-held token in (e.g. from settings)
  dc-validate         -- is the held token still good
  dc-clear-token      -- drop it
  dc-proxy/{path...}  -- authenticated GET passthrough (allow-listed paths only)
  dc-extract          -- submit a JanitorAI/Saucepan URL to the extraction queue

The browser reaches these through the generated API client
(frontend/src/lib/providers/datacat.ts).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from aspect.sources.datacat_client import DatacatSession, DatacatSessionError

router = APIRouter(prefix="/api/v1/datacat", tags=["datacat"])

session = DatacatSession()


class DcInitRequest(BaseModel):
    force: bool = False


class DcSetTokenRequest(BaseModel):
    token: str = ""


class DcExtractRequest(BaseModel):
    url: str = ""
    publicFeed: bool = True
    alwaysReextract: bool = False


@router.get("/health")
async def health() -> dict[str, Any]:
    # Replaces checkDcPluginAvailable()'s plugin-installed probe: this route
    # answering at all (with ok=True) is the whole check -- there is no
    # separate "installed but broken" state to report anymore.
    return {"ok": True}


@router.post("/dc-init")
async def dc_init(req: DcInitRequest) -> dict[str, Any]:
    return await session.init(force=req.force)


@router.post("/dc-set-token")
async def dc_set_token(req: DcSetTokenRequest) -> Response:
    token = req.token.strip()
    if not token:
        return JSONResponse({"error": "token string is required"}, status_code=400)
    if len(token) > 256:
        return JSONResponse({"error": "Token too long"}, status_code=400)
    session.set_token(token)
    return JSONResponse({"ok": True})


@router.post("/dc-clear-token")
async def dc_clear_token() -> dict[str, Any]:
    session.clear_token()
    return {"ok": True}


@router.get("/dc-validate")
async def dc_validate() -> dict[str, Any]:
    return await session.validate()


@router.get("/dc-proxy/{path:path}")
async def dc_proxy(path: str, request: Request) -> Response:
    try:
        resp = await session.proxy_get(f"/{path}", str(request.url.query))
    except DatacatSessionError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    content_type = resp.headers.get("content-type", "")
    return Response(content=resp.content, status_code=resp.status_code, media_type=content_type or None)


@router.post("/dc-extract")
async def dc_extract(req: DcExtractRequest) -> Response:
    status, body = await session.extract(
        req.url, public_feed=req.publicFeed, always_reextract=req.alwaysReextract
    )
    return JSONResponse(body, status_code=status)
