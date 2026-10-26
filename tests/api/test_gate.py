"""The login gate (aspect/api/gate.py, Settings -> Security).

What is pinned here is the boundary: off by default, everything the server
answers closed once it is on, and exactly three things left open -- the auth
routes, the health probe, and the browser client that has to load in order to
ask for the password.
"""

from __future__ import annotations

import base64
import json

import pytest

from aspect.server import FRONTEND_DIST
from aspect.state import security

CREDS = {"username": "matt", "password": "correct horse"}


def _basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def gated(client):
    """The app with the gate switched on, and a client holding no session."""
    response = client.put("/api/v1/security", json={"enabled": True, **CREDS})
    assert response.status_code == 200
    client.cookies.clear()
    return client


def test_off_by_default(client):
    assert client.get("/api/v1/auth/session").json() == {"enabled": False, "authenticated": True}
    assert client.get("/api/v1/characters").status_code == 200
    assert client.get("/api/v1/security").json() == {
        "enabled": False,
        "username": "",
        "has_password": False,
        "api_token": None,
    }


def test_enabling_needs_a_username_and_a_password(client):
    """An enabled gate nothing can open is a lockout."""
    assert client.put("/api/v1/security", json={"enabled": True, "username": "matt"}).status_code == 400
    assert client.put("/api/v1/security", json={"enabled": True, "password": "x"}).status_code == 400
    assert client.get("/api/v1/auth/session").json()["enabled"] is False


def test_enabling_keeps_the_browser_that_did_it_logged_in(client):
    assert client.put("/api/v1/security", json={"enabled": True, **CREDS}).status_code == 200
    assert client.get("/api/v1/characters").status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/characters"),
        ("GET", "/api/v1/settings"),
        ("GET", "/api/v1/security"),
        ("PUT", "/api/v1/security"),
        ("POST", "/build-chub"),
        ("POST", "/existing"),
        ("GET", "/capture-status"),
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
        ("GET", "/proxy/https://example.com"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
    ],
)
def test_everything_the_server_answers_is_closed(gated, method, path):
    response = gated.request(method, path)
    assert response.status_code == 401
    assert response.json() == {"detail": "login required"}
    # The browser's own credentials dialog must not open over the login screen.
    assert "www-authenticate" not in response.headers


def test_what_stays_open(gated):
    assert gated.get("/health").status_code == 200
    assert gated.get("/api/v1/auth/session").json() == {"enabled": True, "authenticated": False}
    assert gated.options(
        "/api/v1/characters",
        headers={"Origin": "https://janitorai.com", "Access-Control-Request-Method": "GET"},
    ).status_code == 200


@pytest.mark.skipif(not FRONTEND_DIST.is_dir(), reason="frontend/dist is not built (make frontend-build)")
def test_the_client_still_loads_so_it_can_ask_for_the_password(gated):
    assert gated.get("/").status_code == 200
    assert gated.get("/settings/security").status_code == 200


def test_login_opens_the_gate_and_logout_closes_it(gated):
    assert gated.post("/api/v1/auth/login", json={**CREDS, "password": "wrong"}).status_code == 401
    assert gated.post("/api/v1/auth/login", json={**CREDS, "username": "nobody"}).status_code == 401
    assert gated.get("/api/v1/characters").status_code == 401

    response = gated.post("/api/v1/auth/login", json=CREDS)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert gated.get("/api/v1/characters").status_code == 200
    assert gated.get("/api/v1/auth/session").json() == {"enabled": True, "authenticated": True}

    assert gated.post("/api/v1/auth/logout").status_code == 204
    assert gated.get("/api/v1/characters").status_code == 401


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _issue_token(gated) -> str:
    """Generate the API token the way Settings -> Security does, then drop the
    session so only the token is left to open the gate."""
    gated.post("/api/v1/auth/login", json=CREDS)
    token = gated.post("/api/v1/security/token").json()["api_token"]
    gated.cookies.clear()
    return token


def test_the_api_token_opens_the_gate_like_a_login(gated):
    """The userscripts' way in."""
    assert gated.post("/api/v1/security/token").status_code == 401  # issuing one is itself gated
    token = _issue_token(gated)
    assert token.startswith("jai_")

    assert gated.post("/existing", json={"ids": []}, headers=_bearer(token)).status_code == 200
    assert gated.get("/api/v1/characters", headers=_bearer(token)).status_code == 200
    assert gated.get("/api/v1/security", headers=_bearer(token)).json()["api_token"] == token
    assert gated.post("/existing", json={"ids": []}, headers=_bearer(token + "x")).status_code == 401
    assert gated.post("/existing", json={"ids": []}, headers=_bearer("")).status_code == 401


def test_regenerating_or_revoking_the_token_ends_the_old_one(gated):
    first = _issue_token(gated)
    second = _issue_token(gated)
    assert second != first
    assert gated.post("/existing", json={"ids": []}, headers=_bearer(first)).status_code == 401
    assert gated.post("/existing", json={"ids": []}, headers=_bearer(second)).status_code == 200

    assert gated.delete("/api/v1/security/token", headers=_bearer(second)).json()["api_token"] is None
    assert gated.post("/existing", json={"ids": []}, headers=_bearer(second)).status_code == 401


def test_the_token_outlives_a_password_change_and_leaves_sessions_alone(gated):
    """A new password must not mean reinstalling two userscripts, and issuing a
    token must not log the browser that asked for it out."""
    gated.post("/api/v1/auth/login", json=CREDS)
    token = gated.post("/api/v1/security/token").json()["api_token"]
    assert gated.get("/api/v1/characters").status_code == 200  # same session, still good

    gated.put("/api/v1/security", json={"enabled": True, "username": "matt", "password": "new one"})
    gated.cookies.clear()
    assert gated.post("/existing", json={"ids": []}, headers=_bearer(token)).status_code == 200


def test_a_generated_userscript_carries_the_token(gated):
    token = _issue_token(gated)
    body = gated.post(
        "/api/v1/userscripts/jai", json={"server_url": "http://nas:8000"}, headers=_bearer(token)
    ).json()
    assert body["includes_token"] is True
    assert f'const DEFAULT_TOKEN = "{token}";' in body["source"]


def test_basic_auth_is_accepted_for_clients_without_a_cookie(gated):
    """Anything scripted that would rather send the credentials themselves."""
    assert gated.post("/existing", json={"ids": []}, headers=_basic(**CREDS)).status_code == 200
    assert gated.post("/existing", json={"ids": []}, headers=_basic("matt", "wrong")).status_code == 401
    assert gated.post("/existing", json={"ids": []}, headers={"Authorization": "Basic !!!"}).status_code == 401
    assert gated.post("/existing", json={"ids": []}, headers={"Authorization": "Bearer x"}).status_code == 401


def test_changing_the_password_ends_existing_sessions(gated):
    gated.post("/api/v1/auth/login", json=CREDS)
    stale = gated.cookies.get("aspect_session")

    changed = gated.put("/api/v1/security", json={"enabled": True, "username": "matt", "password": "new one"})
    assert changed.status_code == 200
    assert gated.get("/api/v1/characters").status_code == 200  # re-issued to the caller

    gated.cookies.clear()
    gated.cookies.set("aspect_session", stale)
    assert gated.get("/api/v1/characters").status_code == 401
    assert gated.post("/existing", json={"ids": []}, headers=_basic(**CREDS)).status_code == 401


def test_saving_without_a_password_keeps_the_current_one(gated):
    gated.post("/api/v1/auth/login", json=CREDS)
    assert gated.put("/api/v1/security", json={"enabled": True, "username": "matt"}).status_code == 200
    gated.cookies.clear()
    assert gated.post("/api/v1/auth/login", json=CREDS).status_code == 200


def test_disabling_reopens_the_server_and_keeps_the_credentials(gated):
    gated.post("/api/v1/auth/login", json=CREDS)
    response = gated.put("/api/v1/security", json={"enabled": False, "username": "matt"})
    assert response.json() == {"enabled": False, "username": "matt", "has_password": True, "api_token": None}
    gated.cookies.clear()
    assert gated.get("/api/v1/characters").status_code == 200


def test_the_password_is_never_stored_or_returned(gated, archive_dirs):
    on_disk = archive_dirs["security"].read_text()
    assert CREDS["password"] not in on_disk
    assert json.loads(on_disk)["password_hash"].startswith("scrypt$")
    gated.post("/api/v1/auth/login", json=CREDS)
    assert "password_hash" not in gated.get("/api/v1/security").text


def test_an_expired_or_forged_session_is_refused(gated):
    store = security.store()
    assert store.check_session(store.issue_session())
    secret = store.config().session_secret
    assert not store.check_session(f"1.{store._sign('1', secret)}")  # signed, but long past
    assert not store.check_session("9999999999.deadbeef")
    assert not store.check_session("")


def test_deleting_the_file_is_the_way_back_in(gated, archive_dirs):
    archive_dirs["security"].unlink()
    assert gated.get("/api/v1/characters").status_code == 200


def test_a_damaged_file_keeps_the_gate_closed(gated, archive_dirs):
    """Corrupting the file must not be a way in."""
    archive_dirs["security"].write_text("{not json")
    assert gated.get("/api/v1/characters").status_code == 401
    assert gated.get("/api/v1/auth/session").json() == {"enabled": True, "authenticated": False}
    assert gated.post("/api/v1/auth/login", json=CREDS).status_code == 401
