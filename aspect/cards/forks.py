"""`extensions.fork` -- the link between a fork and the card it was rewritten
from. See docs/FORKS_AND_EXTRAS_PLAN.md §3.

A fork is a whole, standalone card: nothing lazy-resolves through its parent,
and it stays a valid card if the parent is deleted. What it shares is its
parent's `gallery_id` (so gallery/expression images are never duplicated) and
what it must not share is its parent's identity fragment (the archive's dedupe
key) -- see `root_fragment` and `stamp` below.

Forking a fork is allowed, but flattened: `fork.of` always names the *root*
original's fragment, never an intermediate fork's. A rewrite of a rewrite
becomes a sibling of the existing fork, not a grandchild -- deliberately, for
a single-user library where "every fork of X" is already one lookup and a
multi-hop chain would buy nothing but complexity. See §3's "Do forks fork?".
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from aspect.cards import intake, naming


def generate_card_id() -> str:
    """A fresh id for a fork born here (not adopted from a file).

    Not content-derived: the whole point of a born-here fork is that it
    starts as a byte-for-byte copy of its parent, so hashing its content
    would mint the *same* id twice in a row and the second fork would
    silently overwrite the first. 32 hex characters, of which the filename
    fragment only ever uses the first 8 -- see `aspect.cards.naming.id_fragment`.
    """
    return secrets.token_hex(16)


def root_fragment(parent_data: dict[str, Any], parent_fragment: str) -> str:
    """The fragment a new fork's `fork.of` should carry.

    If the parent is itself a fork, its own `fork.of` already names the root
    -- reuse that rather than the parent's fragment, which is what flattens
    an arbitrarily deep rewrite chain into one level. Otherwise the parent
    *is* the root, and `parent_fragment` is it.
    """
    extensions = parent_data.get("extensions")
    fork = extensions.get("fork") if isinstance(extensions, dict) else None
    if isinstance(fork, dict):
        of = fork.get("of")
        if isinstance(of, str) and of:
            return of
    return parent_fragment


def stamp(
    payload: dict[str, Any],
    *,
    gallery_id: str,
    root: str,
    of_filename: str,
    note: str = "",
) -> None:
    """Write `extensions.gallery_id` and `extensions.fork` onto a card
    envelope (`{spec, spec_version, data, ...v2 mirror}`), forcing the
    gallery link and stamping the fork block.

    Takes the whole envelope, not just `data`, because of a trap in
    `intake._envelope`: `payload = {..., "data": data}; payload.update(data)`
    shallow-copies `data`'s keys onto the envelope root, so right after that
    call `payload["extensions"]` and `payload["data"]["extensions"]` are the
    *same dict object*. Mutating it in place keeps both views correct;
    reassigning `data["extensions"] = {...}` would silently orphan the
    V2-mirror copy at the envelope root. This function only assigns a new
    dict back onto `data` (and mirrors it onto `payload`) in the one case
    where `data` carried no `extensions` object yet.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
        data["extensions"] = extensions
        if data is not payload:
            payload["extensions"] = extensions

    extensions["gallery_id"] = gallery_id
    extensions["fork"] = {
        "of": root,
        "of_filename": of_filename,
        "forkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "note": note,
    }


def reidentify(payload: dict[str, Any], parent_fragment: str) -> str | None:
    """Give an adopted fork an id of its own when it arrived carrying its
    parent's. Returns the new id, or None when the fork already had one.

    A card exported to SillyTavern, rewritten there and handed back to
    `POST /characters` with `fork_of=` still carries the provider block it was
    built from -- and that block holds the *original's* id, so the rewrite's
    `_<id8>` fragment is its parent's. Since the fragment alone is the archive's
    dedupe key, the import is waved through as "already have it" and the
    rewrite silently never lands. `generate_card_id` guards the born-here side
    against the same collision; this is its file-import counterpart, and the
    invariant both serve is the one in this module's docstring: a fork shares
    its parent's gallery, never its identity.

    Content-derived rather than random, unlike `generate_card_id`: dropping the
    same file on the import modal twice must still be caught as a duplicate,
    and an adopted fork -- unlike a born-here one, which starts as a byte-copy
    of its parent -- already differs from its parent in the very text the hash
    reads. The random fallback covers the one case where it does not: a "fork"
    whose name, description and greeting are all still the parent's.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    extensions = data.get("extensions")
    jai = extensions.get("jai") if isinstance(extensions, dict) else None
    if not isinstance(jai, dict):
        return None
    if naming.id_fragment(str(jai.get("id") or "")) != parent_fragment:
        return None

    fresh = intake.synthetic_id(data)
    if naming.id_fragment(fresh) == parent_fragment:
        fresh = generate_card_id()
    # In place, for the same reason `stamp` mutates rather than reassigns: the
    # envelope root and `data` share one `extensions` object.
    jai["id"] = fresh
    return fresh
