"""`extensions.gallery_id` -- the per-character handle SillyTavern's
CharacterLibrary keys an image gallery on.

CharacterLibrary gives each character its own gallery folder named from the
character name *plus* a `gallery_id`, so two characters sharing a name don't
share (and overwrite) one gallery. It mints that id itself when a character
lacks one -- a random 12-character alphanumeric string stored in
`data.extensions.gallery_id`:

    const chars = 'ABC...XYZabc...xyz0123456789';   // 62
    for (let i = 0; i < 12; i++) result += chars.charAt(...)

We stamp the same thing at write time so our cards work there out of the box.
Deliberately the *same naive scheme*, not a stronger one (a content hash would
be the obvious upgrade): an id CharacterLibrary didn't mint has to be
indistinguishable from one it did, and since the id is only ever used alongside
the character name, a bare 62^12 collision isn't the real risk anyway.

An id already on a card is never regenerated -- it is the link to an existing
gallery folder. So an import keeps whatever id its source carried, and
re-exporting a character keeps the id the card already on disk carries.
"""

from __future__ import annotations

import os
import re
import secrets
import string
import threading
from pathlib import Path
from typing import Any

# CharacterLibrary's alphabet and length, verbatim.
_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits
_LENGTH = 12

# The characters CharacterLibrary replaces in a gallery folder name -- Windows'
# reserved set, and nothing more. Notably *not* the same sanitizer the card
# filename uses: a card named `A.D.A` is the file `A_D_A_<id8>.png` but the
# gallery folder `A.D.A_<gallery_id>`, so the two must not share a helper.
_FOLDER_UNSAFE_RE = re.compile(r'[<>:"/\\|?*]')


def generate_id() -> str:
    """A fresh 12-character alphanumeric gallery id, e.g. 'aB3xY9kLmN2p'."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def folder_name(name: str, gallery_id: Any) -> str:
    """The gallery folder for a character: `<sanitized name>_<gallery_id>`.

    CharacterLibrary derives this from the character's *current* name every time
    it needs it, which is why a rename orphans a gallery -- the folder keeps the
    old name and nothing looks for it there (`scripts/repair_galleries.py` exists
    to clean up after exactly that). Reimplemented here, verbatim, because the
    archive has to keep computing it the same way to find the 3,804 folders that
    already exist and to stay drop-in compatible on export.

    Verified against the live archive: 3,785 of 3,839 cards resolve to a folder
    that exists, and the remainder are cards whose images were never downloaded.

    A card with no gallery_id has no gallery folder, and gets "" rather than a
    name that would collide with every other id-less card.
    """
    if gallery_id in (None, ""):
        return ""
    return f"{_FOLDER_UNSAFE_RE.sub('_', name.strip())}_{gallery_id}"


# A folder tail shorter than this is a word, not an id. Gallery ids are 12
# characters on CharacterLibrary's scheme (see generate_id) and every one in this
# archive is, so the floor only ever excludes accidents -- a legacy folder named
# `Marcus_Wright` must not register itself under the id "Wright".
_MIN_ID_LENGTH = 8


def id_of(folder: str) -> str:
    """The gallery id a folder name ends with, or "" when it has no plausible
    `_<id>` tail. The inverse of the suffix `folder_name` appends. Deliberately
    not validated against the alphabet -- a folder that came from somewhere else
    still has whatever it has, and matching it is the point -- but short tails
    are rejected, since those are words from the name rather than ids."""
    _, sep, tail = folder.rpartition("_")
    if not sep or len(tail) < _MIN_ID_LENGTH:
        return ""
    return tail


# The `<gallery_id> -> folder name` map, rebuilt when the galleries directory
# itself changes. Its mtime moves whenever a subdirectory is created, removed or
# renamed, which is exactly when the map can go wrong -- files appearing *inside*
# a gallery do not touch it and do not need to.
_folder_index: dict[str, str] = {}
_folder_index_key: tuple[int, int] | None = None
_folder_index_lock = threading.Lock()


def _scan(root: Path) -> dict[str, str] | None:
    mapping: dict[str, str] = {}
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                gid = id_of(entry.name)
                # First one wins, and directory order is arbitrary, so a
                # duplicate id is left to the orphan view rather than silently
                # resolved to whichever came back first.
                if gid and gid not in mapping:
                    mapping[gid] = entry.name
    except OSError:
        return None
    return mapping


def _folder_map(root: Path, *, fresh: bool = False) -> dict[str, str]:
    """The id map, from cache unless `fresh`.

    Cached on the galleries directory's own mtime, which moves whenever a
    subdirectory is created, removed or renamed -- files appearing *inside* a
    gallery do not touch it and do not need to. `fresh` exists because mtime
    granularity is not a guarantee: a stale *hit* is harmless (the folder is
    still there), but a stale *miss* would report a card as having no images
    moments after its first image was downloaded, so lookups rescan before
    answering None rather than trusting the cache to say no.
    """
    global _folder_index, _folder_index_key
    try:
        st = root.stat()
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_ino)
    if not fresh and key == _folder_index_key:
        return _folder_index
    with _folder_index_lock:
        mapping = _scan(root)
        if mapping is None:
            return {}
        _folder_index = mapping
        _folder_index_key = key
        return mapping


def resolve_folder(root: Path, folder: str) -> str | None:
    """The folder on disk that `folder` names, or None when there is none.

    Exact match first, then -- and this is the point of the function -- any
    directory carrying the same `_<gallery_id>` tail. A card's folder name is
    derived from its *current* name every time it is needed, so renaming a card
    used to orphan its images: the folder kept the old name and nothing looked
    there any more. That is what produced the 262 orphans
    `scripts/repair_galleries.py` was written to clean up.

    Matching on the id alone makes a rename free and an orphan structurally
    impossible, and it is the same rule the archive already uses for duplicate
    and already-saved checks: **the id, never the name**. The cost is cosmetic --
    a renamed card's folder keeps the old name on disk until something has a
    reason to write it again.
    """
    if not folder:
        return None
    if (root / folder).is_dir():
        return folder
    gid = id_of(folder)
    if not gid:
        return None
    found = _folder_map(root).get(gid)
    # Only a miss is worth a rescan -- see `_folder_map`. A hit cannot be wrong
    # in a way that matters, and rescanning on every hit would put a scandir of
    # 3,800 directories on the detail-view path.
    return found if found is not None else _folder_map(root, fresh=True).get(gid)


def read_id(data: dict[str, Any]) -> Any | None:
    """The `extensions.gallery_id` a card's `data` object carries, or None when
    it has none -- absent, empty, or an `extensions` that isn't an object. Reads
    the same from any source's card, since gallery_id is a plain sibling of the
    per-source blocks (jai/chub/datacat)."""
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        return None
    gid = extensions.get("gallery_id")
    return gid if gid not in (None, "") else None


def ensure_id(data: dict[str, Any], preferred: Any | None = None) -> Any | None:
    """Give a card's `data` object a gallery_id if it lacks one, creating the
    `extensions` block if needed. `preferred` -- an id recovered from elsewhere,
    e.g. off the card already on disk at the write target -- is adopted instead
    of a fresh one when given, so an existing gallery link survives.

    Returns the id assigned, or None if the card already carried one (nothing
    changed), which is also the "did this mutate `data`?" signal callers use."""
    if read_id(data) is not None:
        return None
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
        data["extensions"] = extensions
    gid = generate_id() if preferred in (None, "") else preferred
    extensions["gallery_id"] = gid
    return gid
