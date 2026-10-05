"""Each card's lorebook as it was before the first sync rewrote it.

A sync overwrites a card's `character_book` in place -- by design, it is an
edit, not a fork -- so the book the card arrived with would otherwise be gone
the moment a wrong version was chosen or the wrong lorebook attached. Before
the first write to a card its book is copied here, whole and verbatim, and it
is never replaced afterwards: however many syncs follow, "the original" stays
the book the card had before any of them.

One JSON file per card under `data/lorebook_snapshots/`, named by a token the
card itself carries (`extensions.lorebook_snapshot`). The pointer is on the card
rather than derived from its filename or source id because neither is safe to
key on: a card can be renamed on disk, and an adopted fork shares its parent's
source id.

User data, not a cache: nothing else on disk can reproduce a book that has been
overwritten.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("jai_proxy.state.lorebook_snapshots")

# The card extension holding its snapshot's token.
POINTER_KEY = "lorebook_snapshot"

_TOKEN = re.compile(r"^[0-9a-f]{8,64}$")


class SnapshotError(Exception):
    """A snapshot could not be read or written."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    token: str
    taken_at: str
    filename: str
    name: str
    # The card's `character_book` exactly as it was, or None when it had none.
    book: dict[str, Any] | None


def pointer(extensions: Any) -> str:
    """The snapshot token a card's `extensions` carry, or ""."""
    token = extensions.get(POINTER_KEY) if isinstance(extensions, dict) else None
    return token if isinstance(token, str) and _TOKEN.match(token) else ""


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, token: str) -> Path:
        if not _TOKEN.match(token):
            raise SnapshotError(f"{token!r} is not a snapshot token")
        return self.root / f"{token}.json"

    def get(self, token: str) -> Snapshot | None:
        """The snapshot `token` names, or None when there is no such file.
        Raises when the file is there but unreadable -- "no snapshot" would
        tell a caller the card was never synced."""
        if not token:
            return None
        path = self._path(token)
        if not path.is_file():
            return None
        try:
            blob = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"could not read {path}: {exc}") from exc
        if not isinstance(blob, dict):
            raise SnapshotError(f"{path} holds a {type(blob).__name__}, expected a JSON object")
        book = blob.get("character_book")
        return Snapshot(
            token=token,
            taken_at=blob.get("taken_at") if isinstance(blob.get("taken_at"), str) else "",
            filename=blob.get("filename") if isinstance(blob.get("filename"), str) else "",
            name=blob.get("name") if isinstance(blob.get("name"), str) else "",
            book=book if isinstance(book, dict) else None,
        )

    def take(self, *, filename: str, name: str, book: Any) -> str:
        """Store `book` and return the token to stamp on the card. Called
        before the card is written, so a crash in between leaves an unclaimed
        snapshot rather than a rewritten card with none."""
        token = secrets.token_hex(8)
        payload = json.dumps(
            {
                "version": 1,
                "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "filename": filename,
                "name": name,
                "character_book": book if isinstance(book, dict) else None,
            },
            ensure_ascii=False,
        )
        path = self._path(token)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self.root, prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(temporary, path)
            except OSError:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise SnapshotError(f"could not write {path}: {exc}") from exc
        return token
