"""The archive's HTTP contract -- ours, not SillyTavern's.

The cheap way to reuse CharacterLibrary's frontend would have been to make
FastAPI answer `/characters/all`, `/characters/edit-attribute` and the rest of
SillyTavern's endpoint shape, and change no JavaScript at all. That is the trap
this package exists to avoid: it keeps *host* compatibility, which signs this
server up to impersonate SillyTavern's internals forever and moves the tangle
rather than cutting it. What is worth keeping is *format* compatibility -- V3 PNG
cards, the bundle zip, the gallery_id convention -- and none of that requires
matching anyone else's URLs.

So the contract here is designed for this archive, versioned under `/api/v1`, and
the frontend's `core-api.js` is rewritten against it. Two conventions run through
it:

* **A card is identified by its filename.** `Abbie_0d162f5f.png`, exactly as it
  sits on disk and exactly as the thumbnail cache keys it. Not a surrogate id --
  one name for a card across the API, the disk, the thumb cache and a human's
  directory listing.
* **Every response carries its own URLs.** `png_url` and `thumb_url` come back on
  each card, so the client never builds a path by string-concatenating an id it
  had to encode itself.
"""

from aspect.api.v1 import router as v1_router

__all__ = ["v1_router"]
