"""The OpenAI-shaped endpoints the two sites are pointed at as a "custom API
provider": `/v1/models`, `/v1/chat/completions` and the `/health` probe.

There is no model behind any of this. The reply is a side effect -- what the
proxy is actually after is the *system prompt*, because a site that lets you
plug in your own model has to send the character's definition to it, and for a
hidden card that is the only place the definition is ever visible in plaintext.
The capture happens here; the answer comes from `aspect.runtime.mock_responder`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aspect import deps

logger = logging.getLogger("aspect.api.chat")

router = APIRouter()

def _first_message_of_role(messages: list[dict[str, Any]], role: str) -> str:
    for message in messages:
        if message.get("role") == role:
            content = message.get("content", "")
            return content if isinstance(content, str) else str(content)
    return ""


@router.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "captures": deps.capture_store.count,
        "lorebooks": deps.lorebook_cache.count,
        "model": deps.responder.model,
    }


@router.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": deps.responder.model, "object": "model"}],
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body = await request.json()

    try:
        messages = body.get("messages", [])
        deps.capture_store.record(
            _first_message_of_role(messages, "system"),
            primary_greeting=_first_message_of_role(messages, "assistant"),
        )
    except Exception:
        logger.exception("capture failed; continuing to forward")

    # The reply is generated locally (aspect/runtime/mock_responder.py) and has no
    # upstream to fail, so there is no error branch left here -- the capture
    # above is the part that matters, and it already swallows its own failures.
    if body.get("stream"):
        return StreamingResponse(
            deps.responder.stream(body), media_type="text/event-stream"
        )
    return JSONResponse(await deps.responder.complete(body))
