"""`/api/v1` -- browse, download and edit over the card archive.

Every endpoint here is a plain `def`, not `async def`, on purpose: they all touch
the filesystem, and FastAPI runs sync handlers in a threadpool where blocking is
harmless. Declaring them `async` would block the event loop for the duration of a
stat sweep or a 1.2 MB read, which is precisely the workload this API is made of.

This is the archive's own contract, deliberately not SillyTavern's. Teaching it
to answer `/characters/edit-attribute` would have relocated the compatibility
burden rather than ended it, so the translation lived on the client, in one
deletable file (`web/archive-api.js`) -- and was duly deleted with it. The
browser client calls these routes as they are.

The routes are grouped by resource across `characters`, `galleries` (which
also serves `expressions` -- the two share one parameterised implementation,
see `aspect.api.v1.galleries`), `media`, `system`, `network`, `userscripts`,
`discover`, `duplicates` and `auth`; anything two of them need lives in `_shared`.
They are assembled onto one router here, in the order they were registered
when this was a single module.
"""

from fastapi import APIRouter

from aspect.api.v1 import _shared  # noqa: F401  -- re-exported for tests and the server's startup hook
from aspect.api.v1 import auth, characters, discover, duplicates, galleries, media, network, system, userscripts

router = APIRouter(prefix=_shared.PREFIX, tags=["archive"])
router.include_router(characters.router)
router.include_router(system.router)
router.include_router(galleries.router)
router.include_router(galleries.expressions_router)
router.include_router(media.router)
router.include_router(network.router)
router.include_router(userscripts.router)
# Discover's read-only provider previews (docs/UI_REWRITE_PLAN.md §3.8).
router.include_router(discover.router)
# Same-creator duplicate detection (Tools -> Duplicates).
router.include_router(duplicates.router)
# The login gate: session/login/logout, and Settings -> Security.
router.include_router(auth.router)
