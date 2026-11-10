"""Mapping shared by the sources that hand us a finished SillyTavern card
rather than a provider payload -- datacat and JannyAI. Both keep the whole
definition in `description` (their `personality` field is unused), so it maps
straight across; each source supplies only its own idea of the creator."""

from __future__ import annotations

from typing import Any

from aspect.cards.models import ProfileFields
from aspect.text.notes_html import clean_creator_notes


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def profile_fields(data: dict[str, Any], *, creator: str) -> ProfileFields:
    """Map the card's `data` object onto ProfileFields; `creator_notes` is de-HTML'd."""
    return ProfileFields(
        name=_s(data.get("name")),
        creator=creator,
        tags=[t for t in (data.get("tags") or []) if isinstance(t, str) and t.strip()],
        description=_s(data.get("description")),
        scenario=_s(data.get("scenario")),
        mes_example=_s(data.get("mes_example")),
        creator_notes=clean_creator_notes(data.get("creator_notes") or ""),
    )


def greetings(data: dict[str, Any]) -> list[str]:
    """The card's greetings: the primary `first_mes` first, then any
    `alternate_greetings`. Blank/dupe entries are dropped; the CardBuilder makes
    element 0 the first_mes."""
    out: list[str] = []
    for g in [data.get("first_mes"), *(data.get("alternate_greetings") or [])]:
        text = g if isinstance(g, str) else ""
        if text.strip() and text not in out:
            out.append(text)
    return out
