"""When a card came into existence, as the card itself records it.

SillyTavern stamps a root-level `create_date` on every card it writes, and both
its own sort and CharacterLibrary's "Date Created" read that field. Our builders
never wrote it -- an omission from the original ingest path, not a decision -- so
every card this archive produced sorted as 0 and displayed "(not available)".

The value is recoverable without inventing anything. Each importer stamps
`linkedAt` into its own `extensions.<provider>` block at the moment it acquires
the card, and those stamps are never rewritten. The *earliest* of them is when
this archive first saw the character, which is exactly what SillyTavern's
`create_date` means for the same card: measured against a stock SillyTavern
export of 2,019 untouched cards, the earliest `linkedAt` reproduces its
`create_date` to within one second on 85% of the overlap and within a day on 89%
(median delta: 0.2s).

`extensions.jai.linkedAt` is emphatically *not* the field to use, even though it
is the one block present on every card. It is stamped by CardBuilder rather than
by the source, so the bulk repair passes rewrote it: 95% of the archive carries a
`jai.linkedAt` inside a single month, and against that same export it is off by a
median of 69 days and never lands within one. It remains the right value for
"last modified", which is what the archive already serves it as.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# The one root-level field this module owns. Root-level because that is where
# SillyTavern puts it -- inside `data` it would be invisible to ST's own sort.
CREATE_DATE_KEY = "create_date"


def _parse(value: Any) -> datetime | None:
    """`value` as a datetime, or None if it is not an ISO-8601 stamp.

    Only used to *order* stamps, never to reformat one: whatever string a
    provider wrote is what gets stored, so a card's create_date is traceable
    back to the linkedAt it came from.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def earliest_linked_at(data: dict[str, Any]) -> str:
    """The earliest `linkedAt` across the card's provider extension blocks.

    Every block is considered, `jai` included: on a card that only this tool ever
    touched, `jai` is the only stamp there is and it is the honest answer. On a
    card that came through Chub or DataCat, the provider's own (earlier) stamp
    wins on its own merits, without needing a table of which keys to trust.
    """
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        return ""
    stamps = []
    for block in extensions.values():
        if not isinstance(block, dict):
            continue
        raw = block.get("linkedAt")
        parsed = _parse(raw)
        if parsed is not None:
            stamps.append((parsed, raw.strip()))
    if not stamps:
        return ""
    return min(stamps)[1]


def resolve_create_date(envelope: dict[str, Any], data: dict[str, Any]) -> str:
    """The `create_date` a card should carry, given its envelope and data.

    An existing stamp always wins -- a card that arrived from SillyTavern (or
    from an earlier run of this code) already knows when it was created, and
    recomputing would replace a recorded fact with a derived one. Returns ""
    when there is nothing to go on, and callers leave the field off entirely
    rather than writing a blank or an invented "now": a card with no provenance
    is better described as undated than as created the day it was scanned.
    """
    existing = envelope.get(CREATE_DATE_KEY)
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    return earliest_linked_at(data)


def stamp(envelope: dict[str, Any], data: dict[str, Any] | None = None) -> None:
    """Set `create_date` on `envelope` in place, if one can be resolved.

    Called from the two write funnels -- `PngWriter.write_payload` for cards
    being created and `pngtools.embed_card` for cards being re-embedded -- so
    every path out of this codebase stamps, and no path drops the field.
    """
    if data is None:
        inner = envelope.get("data")
        data = inner if isinstance(inner, dict) else envelope
    resolved = resolve_create_date(envelope, data)
    if resolved:
        envelope[CREATE_DATE_KEY] = resolved
