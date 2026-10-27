"""The login gate: one check in front of every route the server answers.

Off unless Settings -> Security has switched it on (`aspect.state.security`), in
which case a request has to carry the session cookie a login hands out, the API
token as `Authorization: Bearer`, or the username and password as HTTP Basic.
The token is for the clients that cannot hold a cookie: the two userscripts,
which post from another site's page and get it baked in when Settings ->
Userscripts generates them.

WHAT IS NOT GATED
The browser client's own files. The shell and its hashed assets hold no archive
data, and the login screen is part of that client -- gating them would mean
serving a second, hand-built page just to ask for a password. So the client
always loads, asks `/api/v1/auth/session` where it stands, and shows its login
screen instead of the app when the answer is "locked". Everything it would then
fetch is behind the gate.

Besides that, only `/health` (the container's healthcheck, which has no
credentials to send) and the auth routes themselves.

WHY A MIDDLEWARE
Because the alternative is a dependency on every router, and the routers are
five files that do not share a parent: one added later without the dependency
would be an open door nobody notices. This sits in front of all of them, and
takes the set of protected prefixes from the same `ROUTERS`-derived list the
frontend catch-all uses, so a new router is covered by being registered.

Pure ASGI rather than `BaseHTTPMiddleware`, which buffers through a task group
and gets in the way of the chat route's streamed replies.
"""

from __future__ import annotations

from collections.abc import Collection

from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from aspect.state import security

COOKIE = "aspect_session"

# Answerable without credentials even when the gate is on.
OPEN_PATHS = frozenset({"/health"})
OPEN_PREFIXES = ("/api/v1/auth/",)


def is_authenticated(connection: HTTPConnection) -> bool:
    """Whether this request may pass. True for everything when the gate is off.

    Blocking when it has to verify a Basic header for the first time (scrypt),
    so the middleware calls it off the event loop.
    """
    store = security.store()
    if not store.config().locked:
        return True
    token = connection.cookies.get(COOKIE)
    if token and store.check_session(token):
        return True
    header = connection.headers.get("authorization")
    return bool(header) and (store.check_token(header) or store.check_basic(header))


class GateMiddleware:
    def __init__(self, app: ASGIApp, protected: Collection[str]) -> None:
        self.app = app
        # First path segments, as `aspect.server.SERVER_OWNED_PREFIXES` has them.
        self.protected = frozenset(protected)

    def _needs_login(self, scope: Scope) -> bool:
        if scope["type"] != "http":
            return False
        # A CORS preflight carries no credentials by design; the request it is
        # asking permission for will.
        if scope.get("method") == "OPTIONS":
            return False
        path: str = scope["path"]
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return False
        return path.lstrip("/").split("/", 1)[0] in self.protected

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self._needs_login(scope)
            # Checked before leaving the event loop: with the gate off, which is
            # the default, a request costs one stat and no thread hop.
            and security.store().config().locked
            and not await run_in_threadpool(is_authenticated, HTTPConnection(scope))
        ):
            # No `WWW-Authenticate`: it would make the browser raise its own
            # credentials dialog over the client's login screen.
            response = JSONResponse({"detail": "login required"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
