"""The card domain's types.

What a character card is, and the neutral shapes the source mappers produce on
the way to building one: `ProfileFields` (visible fields, mapped per site),
`ParsedDefinition` (a hidden definition recovered from a chat system prompt),
`CaptureRecord` (what the relay captured for one character), and the lorebook
pair. `CharacterCardV3` is the output format -- the V3 spec's `data` block.

The request/response shapes of the endpoints that consume these live in
`aspect.api.build_schemas`; nothing here knows about HTTP.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Lorebook / character_book
# ---------------------------------------------------------------------------


class LoreEntry(BaseModel):
    """A single V3 character_book entry. Field set mirrors what JanitorAI's
    janitorai-export userscript's mapLoreEntry produces -- JanitorAI-only
    fields (priority, activationMode, keyMatchPriority, category, tags, the
    original JAI entry id) are stashed under extensions.jai so nothing is
    lost, everything else sits at V3's expected top level."""

    id: int = 0
    keys: list[str] = Field(default_factory=list)
    secondary_keys: list[str] = Field(default_factory=list)
    comment: str = ""
    content: str = ""
    constant: bool = False
    selective: bool = False
    insertion_order: int = 100
    enabled: bool = True
    position: str = "before_char"
    use_regex: bool = False
    name: str = ""
    case_sensitive: bool = False
    extensions: dict[str, Any] = Field(default_factory=dict)


class CharacterBook(BaseModel):
    name: str = ""
    description: str = ""
    scan_depth: int | None = None
    token_budget: int | None = None
    recursive_scanning: bool = False
    extensions: dict[str, Any] = Field(default_factory=dict)
    entries: list[LoreEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsed hidden-definition (from the chat system prompt)
# ---------------------------------------------------------------------------


class ParsedDefinition(BaseModel):
    name: str = ""
    personality: str = ""
    scenario: str = ""
    mes_example: str = ""
    first_mes: str = ""
    raw: str = ""


# ---------------------------------------------------------------------------
# Visible profile fields (mapped from the JanitorAI character JSON by
# sources.janitor; consumed by CardBuilder).
# ---------------------------------------------------------------------------


class ProfileFields(BaseModel):
    name: str = ""
    creator: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    scenario: str = ""
    mes_example: str = ""
    creator_notes: str = ""


# ---------------------------------------------------------------------------
# CaptureStore record
# ---------------------------------------------------------------------------


class CaptureRecord(BaseModel):
    name: str = ""
    personality: str = ""
    scenario: str = ""
    mes_example: str = ""
    raw_system_prompt: str = ""
    lore_entries: list[LoreEntry] = Field(default_factory=list)
    greetings: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Character Card V3
# ---------------------------------------------------------------------------


class CharacterCardV3(BaseModel):
    name: str = ""
    description: str = ""
    personality: str = ""
    scenario: str = ""
    mes_example: str = ""
    first_mes: str = ""
    alternate_greetings: list[str] = Field(default_factory=list)
    creator: str = ""
    creator_notes: str = ""
    tags: list[str] = Field(default_factory=list)
    character_book: CharacterBook | None = None
    system_prompt: str = ""
    post_history_instructions: str = ""
    character_version: str = "aspect-archive"
    extensions: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=True)
        card = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": data,
        }
        # V2-compat top-level mirror for tools that don't understand V3.
        card.update(data)
        return card
