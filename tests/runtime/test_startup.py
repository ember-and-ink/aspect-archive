"""The startup contract for an unusable data mount.

A container's `data/` is a bind mount whose host directory may be owned by a uid
the container does not run as. When that happens the operator must get the
server's one-line preflight report -- not a PermissionError traceback out of an
`import`, which names neither the mount nor the uid. That means nothing
constructed at import time may let a failed mkdir escape.
"""

import os

import pytest

from aspect import config, server
from aspect.state.captures import CaptureStore
from aspect.state.lorebook_cache import LorebookCache

# Permission bits do not stop uid 0, so the whole premise is untestable as root.
pytestmark = pytest.mark.skipif(os.getuid() == 0, reason="root ignores mode bits")


@pytest.fixture
def unwritable(tmp_path, monkeypatch):
    """A directory we cannot create anything inside, with STARTUP_DIR_ERRORS
    isolated so a test's failures do not leak into the next one."""
    monkeypatch.setattr(config, "STARTUP_DIR_ERRORS", [])
    root = tmp_path / "data"
    root.mkdir()
    root.chmod(0o555)
    try:
        yield root
    finally:
        root.chmod(0o755)


def test_capture_store_records_mkdir_failure_instead_of_raising(unwritable):
    store = CaptureStore(captures_dir=unwritable / "state" / "captures")

    assert store.get("anyone") is None
    assert [path for path, _ in config.STARTUP_DIR_ERRORS] == [
        unwritable / "state" / "captures"
    ]


def test_lorebook_cache_records_mkdir_failure_instead_of_raising(unwritable):
    LorebookCache(cache_dir=unwritable / "state" / "lorecache")

    assert [path for path, _ in config.STARTUP_DIR_ERRORS] == [
        unwritable / "state" / "lorecache"
    ]


def test_preflight_reports_each_failed_directory_once(unwritable, monkeypatch):
    # config.py and the store both try the same directory; the operator should
    # see it named once, not once per attempt.
    wanted = unwritable / "state" / "captures"
    config.ensure_dir(wanted)
    CaptureStore(captures_dir=wanted)
    monkeypatch.setattr(server, "STARTUP_DIR_ERRORS", config.STARTUP_DIR_ERRORS)
    monkeypatch.setattr(server, "REQUIRED_DIRS", (wanted,))

    problems = server._preflight()

    assert sum(str(wanted) in line for line in problems) == 1
    assert any(f"uid={os.getuid()}" in line for line in problems)


def test_preflight_silent_when_directories_are_usable(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STARTUP_DIR_ERRORS", [])
    monkeypatch.setattr(server, "REQUIRED_DIRS", (tmp_path,))

    assert server._preflight() == []
