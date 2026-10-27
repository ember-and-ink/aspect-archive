"""How the React client at the root is served (aspect/server.py).

The mount is conditional on `frontend/dist` existing, so these tests skip in a
checkout that has not run `make frontend-build` -- and in CI, where the Python
job never builds it. What they pin down is the behaviour that is easy to break
and invisible until a browser hits it: the SPA fallback, the registration order
that keeps the root catch-all from swallowing the API, and the prefix guard that
keeps a mistyped API path a 404 rather than a 200 full of HTML.
"""

from __future__ import annotations

import pytest

from aspect.server import FRONTEND_DIST

pytestmark = pytest.mark.skipif(
    not FRONTEND_DIST.is_dir(),
    reason="frontend/dist is not built (make frontend-build)",
)


def test_serves_the_shell_at_the_mount_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_client_side_routes_fall_back_to_the_shell(client):
    """A deep link is a real address, not a 404.

    This is the whole reason the client is a route rather than a StaticFiles
    mount: `/characters/<id>` exists only in the client's router, so the server
    has to answer it with the shell and let the client route from there. The
    old `web/` mount could not do this, which is why it had no deep links.
    """
    response = client.get("/characters/Aurora_1a2b3c4d")
    assert response.status_code == 200
    assert b"<div id=\"root\">" in response.content
    # The shell names this build's content-hashed assets, so a cached copy
    # would keep pointing at the previous build's JavaScript.
    assert "no-store" in response.headers["cache-control"]


def test_the_shell_forbids_inline_script(client):
    """Creator notes are third-party HTML rendered into the page itself; this
    is the boundary behind DOMPurify (`SHELL_CSP` in aspect/server.py)."""
    csp = client.get("/characters/Aurora_1a2b3c4d").headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp


def test_real_files_are_served_as_themselves(client):
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"


def test_hashed_assets_are_cacheable_forever(client):
    asset = next((FRONTEND_DIST / "assets").glob("*.js"))
    response = client.get(f"/assets/{asset.name}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_the_api_still_wins_over_the_frontend(client):
    """Registration order is load-bearing and worth a test, not a comment.

    The client's catch-all matches every path there is. If it is ever registered
    ahead of the routers, this is the test that says so.
    """
    assert client.get("/api/v1/stats").status_code == 200


def test_traversal_out_of_dist_is_refused(client):
    """`../` in the path must not reach outside the built output.

    It arrives from the URL unvalidated, and the handler joins it onto a
    directory. A traversal falls back to the shell rather than reading the file.

    Percent-encoded, and that is the whole point of the test: a literal
    `/../../pyproject.toml` is normalised away by the client before it is ever
    sent, so it never reaches this handler and would pass whether or not the
    guard existed. `%2e%2e` survives to the path parameter intact, which is what
    an attacker would send.
    """
    response = client.get("/%2e%2e/%2e%2e/pyproject.toml")
    assert b"[project]" not in response.content
    assert b"<div id=\"root\">" in response.content


@pytest.mark.parametrize(
    "path",
    ["/api/v1/charcters", "/build-chub/typo", "/health/nope", "/proxy"],
)
def test_unknown_server_paths_404_instead_of_the_shell(client, path):
    """A wrong API path must not come back as a 200 with an HTML body.

    The catch-all answers for everything the routers did not claim, which is
    what makes deep links work and would otherwise make `/api/v1/charcters` a
    successful request the client's JSON parser then chokes on. The prefix guard
    (`SERVER_OWNED_PREFIXES`, derived from the routers themselves) is what keeps
    "no such route" saying so.
    """
    assert client.get(path).status_code == 404


def test_the_client_owns_everything_else(client):
    """...and a path outside those prefixes is the client's, not a 404."""
    response = client.get("/tags")
    assert response.status_code == 200
    assert b'<div id="root">' in response.content
