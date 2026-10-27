"""The application: what gets mounted, in what order, and how it is run.

No request handling happens here. The routes live in `aspect.api` -- the archive's
own contract under `/api/v1`, the `/build-*` acquisition endpoints the two
userscripts post to, and the OpenAI-shaped chat surface the sites are pointed at
as a custom provider. This module assembles them onto one app and starts it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from aspect import deps
from aspect.api import v1_router
from aspect.api.build import router as build_router
from aspect.api.capture import router as capture_router
from aspect.api.chat import router as chat_router
from aspect.api.cors_proxy import router as cors_proxy_router
from aspect.api.datacat import router as datacat_router
from aspect.api.gate import GateMiddleware
from aspect.api.v1 import _shared as v1_shared
from aspect.archive import catalog
from aspect.config import REQUIRED_DIRS, ROOT, STARTUP_DIR_ERRORS, settings
from aspect.runtime import dashboard as dashboard_mod

logger = logging.getLogger("aspect.server")

# The browser client (docs/UI_REWRITE_PLAN.md). Built by `make frontend-build`
# into frontend/dist -- present in the container image and in a checkout that
# has run the build, absent otherwise, which is why everything below is
# guarded. In dev it is served by Vite on :5173 instead, with /api proxied back
# here.
#
# It replaced the vendored CharacterLibrary frontend that used to live in
# `web/`, deleted at the Stage 7 cut-over. That directory is preserved on the
# `legacy-web` branch; the "ported from web/..." comments across this package
# resolve against it.
FRONTEND_DIST = ROOT / "frontend" / "dist"


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Binds the media job runner to the loop uvicorn is actually running on
    # (docs/PHASE_3C_PLAN.md §7) -- module import happens before that loop
    # exists, so this can't be done at module scope.
    v1_shared.job_store.bind(asyncio.get_running_loop())
    # Build the archive index before serving: reads never rescan (see
    # aspect.archive.catalog), so this is the sweep that would otherwise land on
    # whoever opens the page first. Off the loop, since a cold build parses every
    # card on the share.
    await asyncio.to_thread(catalog.index)
    yield


app = FastAPI(title="Aspect Archive", lifespan=_lifespan)

# Every router the server answers, as one tuple rather than a run of
# `include_router` calls: the client's catch-all below derives the set of paths
# it must NOT swallow from exactly this list, so a router added here cannot be
# shadowed by the frontend without anyone noticing.
#
# - v1_router: the archive's own contract -- browse, download, export.
#   Deliberately namespaced under /api/v1 and deliberately not shaped like
#   SillyTavern's /api; see aspect/api/__init__.py for why that is the point.
# - datacat_router: DataCat's session transport (Phase 3B S2).
# - build/capture/chat: what the two userscripts talk to -- the per-source
#   build endpoints, the bookkeeping around a bulk run, and the
#   custom-provider chat surface that is how a hidden definition gets captured
#   at all.
# - cors_proxy_router: the passthrough a provider fetch falls back to when the
#   browser cannot reach it directly.
ROUTERS = (
    v1_router,
    datacat_router,
    build_router,
    capture_router,
    chat_router,
    cors_proxy_router,
)

for _router in ROUTERS:
    app.include_router(_router)


def _server_owned_prefixes() -> frozenset[str]:
    """The first path segment of every route the server answers itself.

    The catch-all at the bottom of this module returns the client shell for
    anything unmatched, which is what makes deep links work -- and would also
    turn a mistyped
    `/api/v1/charcters` into a 200 with an HTML body, where the client's JSON
    parser reports something unrecognisable instead of "no such route". So the
    handler 404s inside these prefixes rather than answering for them.

    The login gate uses the same set as "everything that needs a login" -- the
    client's own files are what is left over, and are what serves the login
    screen.

    Derived from `ROUTERS` rather than written out, so adding a router is enough
    to protect it; the three FastAPI adds for itself are the only literals.
    """
    prefixes = {"docs", "redoc", "openapi.json"}
    for router in ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", "")
            if path.startswith("/") and path != "/":
                prefixes.add(path.lstrip("/").split("/", 1)[0])
    return frozenset(prefixes)


SERVER_OWNED_PREFIXES = _server_owned_prefixes()

# The login gate (Settings -> Security), off by default. Added first so it is
# the innermost of the three: CORS answers preflights before the gate sees them
# and puts its headers on the gate's 401, so a cross-origin page reads that
# status rather than an opaque network error.
app.add_middleware(GateMiddleware, protected=SERVER_OWNED_PREFIXES)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The list payloads and the client's own JavaScript are both overwhelmingly
# repeated text, and gzip takes them down by roughly a factor of five. The
# threshold keeps thumbnails and card PNGs out of it: those are already
# compressed, and re-compressing them costs CPU to add bytes.
app.add_middleware(GZipMiddleware, minimum_size=1024)


class QuietAccessFilter(logging.Filter):
    """Drop uvicorn access-log lines for successful (2xx) requests.

    Errors and redirects still print; only routine 200/204/etc noise is
    suppressed. record.args is (client_addr, method, path, http_version,
    status_code) per uvicorn's access logger call.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            status_code = record.args[-1]  # type: ignore[index]
            return not (200 <= int(status_code) < 300)
        except (TypeError, IndexError, ValueError):
            return True


logging.getLogger("uvicorn.access").addFilter(QuietAccessFilter())

# The client, mounted last and mounted at the root.
#
# Last because Starlette matches routes in registration order and the catch-all
# below matches everything: every API route above therefore wins, and the client
# picks up only what is left. Registering it earlier would swallow /api/v1 and
# the userscript endpoints whole.
#
# At the root rather than under /library or /next because the archive *is* this
# application now. There is no host page to be a subsection of, and no second
# frontend to share the root with.


# Creator notes are third-party HTML rendered straight into the page (in a
# shadow root, for style isolation only), so DOMPurify is the content boundary
# and this is the one behind it: markup that slipped past the sanitizer still
# cannot run inline script or an event handler. The built shell loads only
# `<script type="module" src=...>` from its own origin, so 'self' costs nothing.
# Script only -- images, styles and fonts are deliberately left alone.
SHELL_CSP = "script-src 'self'; object-src 'none'; base-uri 'none'"


class ImmutableStaticFiles(StaticFiles):
    """Serve Vite's content-hashed assets with a cache header safe to keep forever.

    Every file under frontend/dist/assets carries a content hash in its name, so
    a changed file is a different URL and a cached one can never be stale. This
    is what retires the hand-bumped `MODULE_VERSION` the old frontend needed --
    and, with it, the `NoCacheStaticFiles` that worked around the cache trap
    that hand-bumping kept losing to.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


if FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        ImmutableStaticFiles(directory=FRONTEND_DIST / "assets"),
        name="frontend-assets",
    )

    @app.get("/", include_in_schema=False)
    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str = "") -> FileResponse:
        """Serve the built client, falling back to index.html for its routes.

        The fallback is the point: this app has client-side routing, so
        /characters/<id> is a real address that must return the shell rather
        than a 404, and let the client route from there. A StaticFiles mount
        cannot do that -- it only rewrites directory requests -- which is why
        this is a route and why deep links work here and never did in the old
        UI.
        """
        head = full_path.split("/", 1)[0]
        if head in SERVER_OWNED_PREFIXES:
            raise HTTPException(status_code=404, detail="Not Found")
        # The path arrives from the URL, unvalidated. Resolve it first and only
        # touch the filesystem once it is known to sit under dist/, so a
        # traversal (`/../../etc/passwd`) can neither be read nor probed for.
        root = os.path.realpath(FRONTEND_DIST)
        try:
            candidate = os.path.realpath(os.path.join(root, full_path))
        except ValueError:  # an embedded NUL -- no such file, serve the shell
            candidate = ""
        if full_path and candidate.startswith(root + os.sep) and os.path.isfile(candidate):
            return FileResponse(candidate)
        # no-store on the shell only: it names the hashed assets, so a cached
        # copy would keep pointing at the previous build's JavaScript.
        return FileResponse(
            FRONTEND_DIST / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate", "Content-Security-Policy": SHELL_CSP},
        )
else:  # pragma: no cover -- a checkout that has not run `make frontend-build`
    logger.warning(
        "frontend/dist is missing at %s; the browser UI will not be served "
        "(run `make frontend-build`)",
        FRONTEND_DIST,
    )


def _owner_of(path: Path) -> tuple[Path, int, int] | None:
    """Who owns the nearest *existing* ancestor of `path`, or None.

    The nearest existing one because a path we could not create is by
    definition absent -- what the operator needs to see is who owns the
    directory the creation was attempted in.
    """
    for candidate in (path, *path.parents):
        try:
            info = candidate.stat()
        except OSError:
            continue
        return candidate, info.st_uid, info.st_gid
    return None


def _preflight() -> list[str]:
    """Problems with the data mount, as lines to print. Empty means all good.

    This is the failure a remote deployment actually hits: `data/` is a bind
    mount, and on unraid its host directory is owned by `nobody:users` (99:100)
    while the image's default user is uid 1000. Without this the symptom is a
    PermissionError traceback from an import, which names neither the mount nor
    the uid -- see docs/DEPLOY.md.
    """
    problems: list[str] = []
    unwritable: list[Path] = []

    # Deduplicated by path: the same directory is attempted twice when a store
    # in aspect.deps re-ensures one config.py already tried (captures, lorecache).
    for path, exc in STARTUP_DIR_ERRORS:
        if path in unwritable:
            continue
        unwritable.append(path)
        problems.append(f"could not create {path}: {exc.strerror or exc}")

    for path in REQUIRED_DIRS:
        if path in unwritable or not path.is_dir():
            continue
        # An actual write, not os.access: access() answers from the permission
        # bits alone and reports success on a read-only mount.
        probe = path / ".aspect-archive-write-test"
        try:
            probe.touch()
            probe.unlink()
        except OSError as exc:
            problems.append(f"cannot write to {path}: {exc.strerror or exc}")
            unwritable.append(path)

    if problems:
        # Deduplicated by owner: these directories are normally all inside one
        # bind mount, so without this every failure repeats the same
        # "owned by uid=99 gid=100" and buries the one fact that matters.
        owners: dict[tuple[int, int], Path] = {}
        for path in unwritable:
            if found := _owner_of(path):
                owner_path, uid, gid = found
                owners.setdefault((uid, gid), owner_path)
        problems.append(
            f"the server runs as uid={os.getuid()} gid={os.getgid()}; "
            + "; ".join(
                f"{owner_path} is owned by uid={uid} gid={gid}"
                for (uid, gid), owner_path in owners.items()
            )
        )
        problems.append(
            "in a container, chown the host directory behind the /app/data mount "
            "to that uid/gid, or set PUID/PGID to the directory's owner"
        )
    return problems


def _stats_line() -> str:
    return (
        f"{deps.capture_store.count} captures · "
        f"{deps.lorebook_cache.count} lorebooks cached · "
        f"model {deps.responder.model}"
    )


def _serve() -> None:
    # log_config=None keeps uvicorn from installing its own stdout handlers, so
    # its records propagate to the root logger we configured instead.
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


def main() -> None:
    # Before anything else, and before the dashboard takes over the terminal:
    # an unusable data mount is fatal, and the message has to be readable.
    if problems := _preflight():
        print("Aspect Archive cannot start -- the data directory is not usable:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        raise SystemExit(1)

    # The handle lives on the dashboard module rather than being a global here,
    # so the route module that reports into it (aspect/api/build.py) can read it
    # without importing this one -- see the note beside its definition.
    if not (settings.dashboard and sys.stdout.isatty()):
        logging.basicConfig(level=logging.INFO)
        _serve()
        return

    dashboard_mod.DASHBOARD = dashboard_mod.Dashboard(
        title="Aspect Archive",
        address=f"http://{settings.host}:{settings.port}",
        stats=_stats_line,
    )
    dashboard_mod.install_logging(dashboard_mod.DASHBOARD)
    sink = dashboard_mod.StdoutSink(dashboard_mod.DASHBOARD.log)
    try:
        with dashboard_mod.live(dashboard_mod.DASHBOARD), contextlib.redirect_stdout(sink):
            _serve()
    except KeyboardInterrupt:  # pragma: no cover -- Ctrl-C is a clean exit
        pass
    finally:
        dashboard_mod.replay_problems(dashboard_mod.DASHBOARD, sys.stderr)
        dashboard_mod.DASHBOARD = None


if __name__ == "__main__":
    main()
