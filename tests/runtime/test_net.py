"""The outbound-proxy resolver: where the URL comes from and what it refuses.

Nothing here touches the network. What is worth pinning down is the precedence
(settings.json over the environment), the refusals (they must degrade to "no
proxy", never raise, because a bad value must not be able to fail a media run),
and the memoization -- which is the one piece with a real staleness hazard.
"""

from __future__ import annotations

import json

import httpx
import pytest

from aspect.config import settings
from aspect.runtime import net


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point the settings file at a temp dir and start from a cold cache.

    The cache is module-global; without the reset a test inherits whichever URL
    the previous one resolved, and the developer's own `.env` proxy leaks in.
    """
    monkeypatch.setattr(settings, "settings_file", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "http_proxy", None)
    net.reset_cache()
    yield
    net.reset_cache()


def write_settings(blob: dict) -> None:
    settings.settings_file.write_text(json.dumps(blob))


def test_nothing_configured_is_direct():
    assert net.resolved_proxy() is None


def test_env_var_is_the_fallback(monkeypatch):
    monkeypatch.setattr(settings, "http_proxy", "http://env:3128")
    assert net.resolved_proxy() == "http://env:3128"


def test_settings_file_beats_the_env_var(monkeypatch):
    monkeypatch.setattr(settings, "http_proxy", "http://env:3128")
    write_settings({net.SETTINGS_KEY: "http://ui:8118"})
    assert net.resolved_proxy() == "http://ui:8118"


@pytest.mark.parametrize(
    "stored",
    ["", "   ", None],
    ids=["empty", "whitespace", "null"],
)
def test_a_blank_stored_value_falls_through_to_the_env(monkeypatch, stored):
    """Clearing the field in the UI must not shadow the deployment's own
    setting -- the frontend writes null for "cleared", and empty strings arrive
    from a hand-edited file."""
    monkeypatch.setattr(settings, "http_proxy", "http://env:3128")
    write_settings({net.SETTINGS_KEY: stored})
    assert net.resolved_proxy() == "http://env:3128"


def test_a_wrong_type_is_ignored_not_raised():
    write_settings({net.SETTINGS_KEY: {"host": "nope"}})
    assert net.resolved_proxy() is None


def test_a_damaged_settings_file_degrades_to_direct():
    """`GET /api/v1/settings` raises a 500 on this, deliberately. Here the
    opposite is right: a damaged blob must not stop downloads from happening."""
    settings.settings_file.write_text("{not json")
    assert net.resolved_proxy() is None


@pytest.mark.parametrize(
    "url",
    ["ftp://host:21", "not-a-url", "http://", "://host"],
)
def test_unusable_urls_are_refused(url):
    write_settings({net.SETTINGS_KEY: url})
    assert net.resolved_proxy() is None


def test_socks_is_refused_without_socksio(monkeypatch):
    """socksio is not a dependency, and httpx raises ImportError at *client
    construction* -- i.e. inside a download loop. Refusing up front turns that
    into a logged line and a red dot."""
    monkeypatch.setattr(net, "_socks_available", lambda: False)
    write_settings({net.SETTINGS_KEY: "socks5://host:1080"})
    assert net.resolved_proxy() is None


def test_socks_is_accepted_when_socksio_is_present(monkeypatch):
    monkeypatch.setattr(net, "_socks_available", lambda: True)
    write_settings({net.SETTINGS_KEY: "socks5h://host:1080"})
    assert net.resolved_proxy() == "socks5h://host:1080"


def test_a_rewritten_settings_file_is_picked_up():
    """The memoization keys on mtime+size, so an edit must invalidate it."""
    write_settings({net.SETTINGS_KEY: "http://first:8118"})
    assert net.resolved_proxy() == "http://first:8118"
    write_settings({net.SETTINGS_KEY: "http://second:8118"})
    net.reset_cache()  # what PUT /settings does, since mtime can tie
    assert net.resolved_proxy() == "http://second:8118"


def test_the_lookup_is_memoized(monkeypatch):
    write_settings({net.SETTINGS_KEY: "http://cached:8118"})
    assert net.resolved_proxy() == "http://cached:8118"

    calls = []
    real_read = net.ui_settings.SettingsStore.read

    def counting_read(self):
        calls.append(1)
        return real_read(self)

    monkeypatch.setattr(net.ui_settings.SettingsStore, "read", counting_read)
    for _ in range(5):
        net.resolved_proxy()
    assert calls == []


# --- redaction ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://host:3128", "http://host:3128"),
        ("http://user:hunter2@host:3128", "http://user:***@host:3128"),
        ("http://:hunter2@host:3128", "http://:***@host:3128"),
        (None, None),
    ],
)
def test_redact_removes_the_password_and_keeps_the_user(raw, expected):
    assert net.redact(raw) == expected


# --- client factories --------------------------------------------------------


def test_clients_never_read_the_environment(monkeypatch):
    """The whole point of pinning trust_env off: an ambient HTTPS_PROXY in the
    container environment must not route anything on its own."""
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient:9999")
    monkeypatch.setenv("HTTP_PROXY", "http://ambient:9999")
    with net.sync_client() as client:
        assert client.trust_env is False
    assert net.resolved_proxy() is None


def test_an_explicit_none_forces_a_direct_client():
    """The status check's control leg depends on this override."""
    write_settings({net.SETTINGS_KEY: "http://ui:8118"})
    with net.sync_client(proxy=None) as client:
        assert client.trust_env is False


async def test_holder_rebuilds_when_the_proxy_changes():
    holder = net.AsyncClientHolder()
    write_settings({net.SETTINGS_KEY: "http://first:8118"})
    first = await holder.get()
    assert await holder.get() is first  # unchanged setting -> same client

    write_settings({net.SETTINGS_KEY: "http://second:8118"})
    net.reset_cache()
    second = await holder.get()
    assert second is not first
    assert first.is_closed
    await holder.aclose()


async def test_holder_never_replaces_an_injected_client():
    """Every test in the suite injects a MockTransport client; the holder must
    hand it straight back, or those tests would silently hit the network."""
    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    holder = net.AsyncClientHolder(injected)
    write_settings({net.SETTINGS_KEY: "http://ui:8118"})
    net.reset_cache()
    assert await holder.get() is injected
    await injected.aclose()


# --- every outbound fetcher actually goes through the factory ----------------


def test_no_module_builds_a_bare_httpx_client():
    """The gate that keeps this module the only door.

    Four of the five original client constructions passed the proxy and one did
    not, and that asymmetry is invisible at any single call site -- it only
    shows up as "media downloads were proxied but avatars weren't". A grep is
    the cheapest way to keep the next one from being added the same way.
    """
    import re
    from pathlib import Path

    root = Path(net.__file__).resolve().parent.parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "net.py":
            continue  # the one place allowed to construct a client
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"httpx\.(Async)?Client\s*\(", line):
                offenders.append(f"{path.relative_to(root.parent)}:{lineno}")
    assert offenders == [], (
        "build clients via aspect.runtime.net (async_client / sync_client / the "
        f"holders) so they carry the configured proxy: {offenders}"
    )


async def test_the_avatar_fetcher_uses_the_configured_proxy(monkeypatch):
    """The one fetcher that never had proxy support before this."""
    from aspect.cards.avatar_fetch import AvatarFetcher

    write_settings({net.SETTINGS_KEY: "http://ui:8118"})
    net.reset_cache()

    fetcher = AvatarFetcher()
    # Built lazily, so this is also the assertion that it is not pinned at
    # import time the way `aspect.deps.avatar_fetcher` would otherwise pin it.
    assert isinstance(fetcher._clients, net.AsyncClientHolder)
    await fetcher._clients.get()
    assert fetcher._clients._proxy == "http://ui:8118"
    await fetcher._clients.aclose()
