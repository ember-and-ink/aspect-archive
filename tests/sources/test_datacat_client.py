"""aspect/datacat_client.py -- the datacat.run original-avatar resolver used by
`scripts/import_cards.py --fetch-datacat-images`. Covers the identify/detail
handshake, retry-then-give-up behaviour, and picking the untouched
source-CDN avatar out of a character-detail payload over datacat's own
re-hosted variants."""

import httpx
import pytest

from aspect.sources import datacat_client
from aspect.sources.datacat_client import DatacatImageResolver, _original_avatar_url


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    # These tests exercise the retry loop itself; strip the backoff sleep so
    # they run at full speed instead of a real multi-second wait.
    monkeypatch.setattr(datacat_client, "_RETRY_BACKOFF_SECONDS", 0.0)
    # Isolate from whatever's actually in this machine's .env -- a real
    # persisted token there would silently change these tests' behaviour
    # (e.g. skipping the identify call a test expects to see happen).
    monkeypatch.setattr(datacat_client.settings, "datacat_session_token", None)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _identify_ok(session_token="tok-123"):
    return httpx.Response(200, json={"success": True, "sessionToken": session_token})


def test_resolve_returns_original_avatar_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/liberator/identify":
            return _identify_ok()
        assert request.headers["X-Session-Token"] == "tok-123"
        return httpx.Response(
            200,
            json={
                "success": True,
                "character": {
                    "chara_card_v2_json": {
                        "data": {"avatar": "https://ella.janitorai.com/bot-avatars/abc.webp"}
                    }
                },
            },
        )

    resolver = DatacatImageResolver(client=_client(handler), persist=False)
    assert resolver.resolve("char-id") == "https://ella.janitorai.com/bot-avatars/abc.webp"


def test_resolve_reuses_session_token_across_calls():
    identify_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal identify_calls
        if request.url.path == "/api/liberator/identify":
            identify_calls += 1
            return _identify_ok()
        return httpx.Response(
            200,
            json={
                "success": True,
                "character": {"chara_card_v2_json": {"data": {"avatar": "https://ella.janitorai.com/x.webp"}}},
            },
        )

    resolver = DatacatImageResolver(client=_client(handler), persist=False)
    resolver.resolve("id-1")
    resolver.resolve("id-2")
    assert identify_calls == 1


def test_resolve_falls_back_to_content_variants():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/liberator/identify":
            return _identify_ok()
        return httpx.Response(
            200,
            json={
                "success": True,
                "character": {
                    "chara_card_v2_json": {"data": {"avatar": None}},
                    "content_variants": [
                        {"content": {"chara_card_v2_json": {"data": {"avatar": "https://cdn.saucepan.ai/images/x/card"}}}}
                    ],
                },
            },
        )

    resolver = DatacatImageResolver(client=_client(handler), persist=False)
    assert resolver.resolve("char-id") == "https://cdn.saucepan.ai/images/x/card"


def test_resolve_ignores_datacat_rehosted_variants():
    """avatar_variant_urls/avatar are datacat's own downscaled re-hosting --
    never a substitute for the untouched original."""
    assert _original_avatar_url(
        {
            "avatar": "https://media.datacat.run/media/variants/source-avatar/v2/card.webp",
            "avatar_variant_urls": {"hero": "https://media.datacat.run/media/variants/source-avatar/v1/hero.webp"},
        }
    ) is None


def test_resolve_returns_none_when_identify_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    resolver = DatacatImageResolver(client=_client(handler), persist=False)
    assert resolver.resolve("char-id") is None


def test_resolve_returns_none_when_character_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/liberator/identify":
            return _identify_ok()
        return httpx.Response(404, json={"success": False})

    resolver = DatacatImageResolver(client=_client(handler), persist=False)
    assert resolver.resolve("gone-id") is None


def test_resolve_returns_none_for_empty_character_id():
    resolver = DatacatImageResolver(client=_client(lambda r: httpx.Response(500)), persist=False)
    assert resolver.resolve("") is None


def test_request_retries_transient_failures_then_succeeds():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/api/liberator/identify":
            attempts += 1
            if attempts < 3:
                return httpx.Response(503)
            return _identify_ok()
        return httpx.Response(
            200,
            json={"success": True, "character": {"chara_card_v2_json": {"data": {"avatar": "https://ella.janitorai.com/x.webp"}}}},
        )

    resolver = DatacatImageResolver(client=_client(handler), persist=False)
    assert resolver.resolve("char-id") == "https://ella.janitorai.com/x.webp"
    assert attempts == 3


def test_request_gives_up_after_max_attempts():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    resolver = DatacatImageResolver(client=_client(handler), persist=False)
    assert resolver.resolve("char-id") is None
    assert attempts == 3  # _MAX_ATTEMPTS, no more


def test_request_does_not_retry_hard_4xx():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401)

    resolver = DatacatImageResolver(client=_client(handler), persist=False)
    assert resolver.resolve("char-id") is None
    assert attempts == 1


# ---------------------------------------------------------------------------
# Session token persistence -- preload from settings, write-back, stale retry
# ---------------------------------------------------------------------------


def test_uses_preloaded_token_without_calling_identify(monkeypatch):
    identify_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal identify_calls
        if request.url.path == "/api/liberator/identify":
            identify_calls += 1
            return _identify_ok()
        assert request.headers["X-Session-Token"] == "preloaded-tok"
        return httpx.Response(
            200,
            json={"success": True, "character": {"chara_card_v2_json": {"data": {"avatar": "https://ella.janitorai.com/x.webp"}}}},
        )

    monkeypatch.setattr(datacat_client.settings, "datacat_session_token", "preloaded-tok")
    resolver = DatacatImageResolver(client=_client(handler), persist=False)
    assert resolver.resolve("char-id") == "https://ella.janitorai.com/x.webp"
    assert identify_calls == 0


def test_drops_and_refetches_a_rejected_stored_token(monkeypatch, tmp_path):
    identify_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal identify_calls
        if request.url.path == "/api/liberator/identify":
            identify_calls += 1
            return _identify_ok(session_token="fresh-tok")
        if request.headers["X-Session-Token"] == "stale-tok":
            return httpx.Response(401)
        assert request.headers["X-Session-Token"] == "fresh-tok"
        return httpx.Response(
            200,
            json={"success": True, "character": {"chara_card_v2_json": {"data": {"avatar": "https://ella.janitorai.com/y.webp"}}}},
        )

    monkeypatch.setattr(datacat_client.settings, "datacat_session_token", "stale-tok")
    env_path = tmp_path / ".env"
    resolver = DatacatImageResolver(client=_client(handler), env_path=env_path)
    assert resolver.resolve("char-id") == "https://ella.janitorai.com/y.webp"
    assert identify_calls == 1
    # The freshly-identified replacement got persisted too.
    assert "ASPECT_DATACAT_SESSION_TOKEN=fresh-tok" in env_path.read_text()


def test_persists_freshly_fetched_token_to_env_file(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/liberator/identify":
            return _identify_ok(session_token="brand-new-tok")
        return httpx.Response(
            200,
            json={"success": True, "character": {"chara_card_v2_json": {"data": {"avatar": "https://ella.janitorai.com/z.webp"}}}},
        )

    env_path = tmp_path / ".env"
    env_path.write_text("ASPECT_ARCHIVE_DIR=/somewhere\n")
    resolver = DatacatImageResolver(client=_client(handler), env_path=env_path)
    resolver.resolve("char-id")

    contents = env_path.read_text()
    assert "ASPECT_DATACAT_SESSION_TOKEN=brand-new-tok" in contents
    assert "ASPECT_ARCHIVE_DIR=/somewhere" in contents  # untouched


def test_persist_false_never_touches_the_env_file(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/liberator/identify":
            return _identify_ok()
        return httpx.Response(
            200,
            json={"success": True, "character": {"chara_card_v2_json": {"data": {"avatar": "https://ella.janitorai.com/z.webp"}}}},
        )

    env_path = tmp_path / ".env"
    resolver = DatacatImageResolver(client=_client(handler), persist=False, env_path=env_path)
    resolver.resolve("char-id")
    assert not env_path.exists()


def test_persist_session_token_replaces_existing_line(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("ASPECT_ARCHIVE_DIR=/somewhere\nASPECT_DATACAT_SESSION_TOKEN=old-tok\nASPECT_CARD_LAYOUT=flat\n")

    datacat_client._persist_session_token("new-tok", env_path)

    lines = env_path.read_text().splitlines()
    assert lines == [
        "ASPECT_ARCHIVE_DIR=/somewhere",
        "ASPECT_DATACAT_SESSION_TOKEN=new-tok",
        "ASPECT_CARD_LAYOUT=flat",
    ]


def test_persist_session_token_appends_when_no_existing_line(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("ASPECT_ARCHIVE_DIR=/somewhere\n")

    datacat_client._persist_session_token("new-tok", env_path)

    assert env_path.read_text() == "ASPECT_ARCHIVE_DIR=/somewhere\nASPECT_DATACAT_SESSION_TOKEN=new-tok\n"


def test_persist_session_token_creates_missing_env_file(tmp_path):
    env_path = tmp_path / ".env"
    datacat_client._persist_session_token("new-tok", env_path)
    assert env_path.read_text() == "ASPECT_DATACAT_SESSION_TOKEN=new-tok\n"
