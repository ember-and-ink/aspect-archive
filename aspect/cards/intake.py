"""Adopting a card PNG that arrived as a file rather than from a provider.

The `/build-*` routes each own a *site*: the browser captures that site's JSON,
a mapper in `aspect.sources` turns it into neutral fields, and the shared tail
writes the card. This module owns the other door -- a PNG handed over whole, by
a drag onto the import modal or out of a Character Library bundle -- where there
is no site to ask and the card is already embedded in the file.

Two kinds of file come through it, and the difference decides everything:

  * **A card this archive already made.** Every card written here carries an
    `extensions.jai` provenance block and nothing else does (the same test
    `scripts/import_cards.py` uses to spot an unprocessed orphan). Its text has
    already been sanitized and its pixels have already been cropped, resized and
    quantized on the way in, so it is adopted *verbatim* -- no second clean, no
    second re-encode. Re-running the image pipeline over an already-cropped
    portrait is how a bundle round-trip quietly degrades a card it was only
    meant to copy.

  * **Anything else.** A foreign card gets the full intake treatment: macros
    sanitized, creator notes tamed, tags normalized, provenance stamped, image
    normalized/cropped/quantized -- so a card that arrives this way is shaped
    exactly like one the build routes wrote.

What this module does *not* do is decide about duplicates or touch the disk;
that is the route's job (`aspect.api.v1.characters.import_character`), which owns
the archive index the fragment is checked against.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aspect import deps
from aspect.cards import pngtools
from aspect.cards.naming import id_fragment
from aspect.sources import chub, datacat, jannyai


class IntakeError(ValueError):
    """The uploaded file is not a card this archive can adopt."""


@dataclass
class PreparedCard:
    """A card ready to be written, and how to write it."""

    payload: dict[str, Any]
    """The full `{spec, spec_version, data, ...V2 mirror}` envelope to embed."""
    name: str
    creator: str
    card_id: str
    fragment: str
    source: str
    """Where the card was judged to come from -- `archive` for one of our own,
    otherwise the source that claimed it (`chub`/`datacat`/`jannyai`/`png`)."""
    normalize: bool
    """Whether the uploaded pixels still need the intake image pipeline. False
    for a card of ours, whose image has already been through it."""
    warnings: list[str]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _is_ours(data: dict[str, Any]) -> bool:
    """Whether this card came out of our pipeline. Same test as
    `scripts/import_cards.py::_is_processed` -- every card written here carries
    an `extensions.jai` stamp, from any source, and nothing else does."""
    return isinstance((data.get("extensions") or {}).get("jai"), dict)


def synthetic_id(data: dict[str, Any]) -> str:
    """An id for a card that carries none of the ids we recognise.

    Every card in the archive is `<name>_<id8>.png`, and the `_<id8>` fragment
    alone is the dedupe key -- a card with no fragment is one the duplicate
    check can never see again. So an unrecognised PNG gets an id derived from
    its own definition rather than a random one: dropping the same file on the
    import modal twice then lands on the same fragment and is caught as the
    duplicate it is.
    """
    basis = "\n\x00".join(
        str(data.get(field) or "") for field in ("name", "description", "first_mes")
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _provenance(data: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    """`(source, card_id, creator, extensions)` for a foreign card.

    The three sources with an `extensions.<site>` block are recognised and
    stamped the way `scripts/import_cards.py` stamps them, so a card dropped in
    here is linkable in the frontend without a second lookup. Anything else --
    a hand-made card, an export from a site we have no mapper for -- is taken at
    face value and stamped `png_import`.
    """
    if chub.is_chub(data):
        module, source, kind = chub, "chub", "chub_import"
    elif datacat.is_datacat(data):
        module, source, kind = datacat, "datacat", "datacat_import"
    elif jannyai.is_jannyai(data):
        module, source, kind = jannyai, "jannyai", "jannyai_import"
    else:
        name = str(data.get("name") or "")
        creator = str(data.get("creator") or "")
        extensions = dict(data.get("extensions") or {})
        extensions["jai"] = {
            "source_url": None,
            "id": synthetic_id(data),
            "sourceKind": "png_import",
            "creatorName": creator,
            "pageName": name,
            "linkedAt": _utc_now_iso(),
        }
        return "png", extensions["jai"]["id"], creator, extensions

    card_id = module.card_id(data)
    creator = module.creator(data)
    extensions = dict(data.get("extensions") or {})
    extensions["jai"] = {
        "source_url": module.source_url(data),
        "id": card_id or None,
        "sourceKind": kind,
        "creatorName": creator,
        "pageName": module.page_name(data),
        "linkedAt": _utc_now_iso(),
    }
    # A recognised source with no id of its own would land without a fragment,
    # i.e. outside the dedupe key -- fall back to the content hash rather than
    # writing a card the duplicate check cannot see.
    if not card_id:
        card_id = synthetic_id(data)
        extensions["jai"]["id"] = card_id
    return source, card_id, creator, extensions


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    """The canonical `chara_card_v3` envelope around a card's `data` object --
    spec header plus the top-level V2 mirror, the same structure
    `CharacterCardV3.to_dict` and `pngtools.embed_card` produce."""
    payload = {"spec": "chara_card_v3", "spec_version": "3.0", "data": data}
    payload.update(data)  # V2-compat top-level mirror
    return payload


def adopt(raw: bytes) -> PreparedCard:
    """Read the card out of an uploaded PNG and prepare it for the writer."""
    try:
        parsed = pngtools.read_envelope(raw)
    except ValueError as exc:  # not a PNG stream at all
        raise IntakeError(str(exc)) from exc
    if parsed is None:
        raise IntakeError("the file carries no character card")
    _outer, data = parsed
    if not isinstance(data, dict):
        raise IntakeError("the embedded card is not an object")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        # Same rule the write route enforces: a nameless card is an unfindable
        # blank in the grid.
        raise IntakeError("the embedded card has no `name`")
    name = name.strip()

    if _is_ours(data):
        jai = data["extensions"]["jai"]
        card_id = str(jai.get("id") or "")
        return PreparedCard(
            payload=_envelope(data),
            name=name,
            creator=str(data.get("creator") or jai.get("creatorName") or ""),
            card_id=card_id,
            fragment=id_fragment(card_id),
            source="archive",
            normalize=False,
            warnings=[],
        )

    # `chub.clean_card` / the envelope builder next to it are source-neutral
    # despite living in the Chub module: raw-dict cleaning that leaves the
    # lorebook's extras and int positions alone is exactly what a foreign PNG
    # needs, and is the reason that rule exists at all (see sources.chub).
    cleaned, warnings = chub.clean_card(data, deps.chub_sanitizer)
    source, card_id, creator, extensions = _provenance(cleaned)
    cleaned["extensions"] = extensions
    return PreparedCard(
        payload=_envelope(cleaned),
        name=str(cleaned.get("name") or name),
        creator=creator,
        card_id=card_id,
        fragment=id_fragment(card_id),
        source=source,
        normalize=True,
        warnings=warnings,
    )
