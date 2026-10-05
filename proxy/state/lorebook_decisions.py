"""Decisions about lorebooks, persisted at `data/lorebooks.json`.

`proxy.archive.lorebooks` derives everything it can from the cards: which
lorebooks exist, which cards carry each, where copies differ. What it cannot
derive is judgement, and that is what lives here:

* **Lorebook merges** -- two lorebooks the index kept apart (no shared provider
  id, entries not identical) that are in fact the same book.
* **Entry merges** -- two entries of one lorebook the index took for different
  entries (a retitled entry, say) that are versions of the same one.
* **Chosen versions** -- for an entry with several versions, the one every card
  should end up carrying.
* **Attachments** -- a lorebook to put on a card that does not carry it, or to
  take off one that does.

Nothing here touches a card. These are inputs to the index and to the sync
(`proxy.archive.lorebook_sync`) that writes them onto cards; until that runs,
recording or undoing a decision changes only what the Lorebooks pages show. An
attachment is dropped once a sync has carried it out -- the card says it from
then on. A chosen version stays: a card acquired later with an older version
should be offered the same upgrade.

Everything is keyed on content or provider identity -- an entry version's hash,
a lorebook's provider ref or content fingerprint -- never on the index's derived
ids, which shift as merges are applied. A decision whose keys no longer match
anything on disk is simply inert.

User data like `ignored.json`, and beside it for the same reason: a judgement is
not recoverable from anything else on disk.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from proxy.cards.lorebook_split import digest

logger = logging.getLogger("jai_proxy.state.lorebook_decisions")


class DecisionsError(Exception):
    """The stored decisions could not be read or written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Merge:
    """Two keys declared the same thing. For a lorebook merge the keys are
    lorebook anchors and the names are what each side was called when merged,
    kept only so the undo list can say what it would separate."""

    a: str
    b: str
    at: str = ""
    a_name: str = ""
    b_name: str = ""

    @property
    def id(self) -> str:
        return digest("\x00".join(sorted((self.a, self.b))))[:12]


@dataclass(frozen=True, slots=True)
class Attachment:
    """A lorebook to attach to a card, or detach from it, at the next sync.

    The card is named by its filename -- what the rest of the API calls it --
    with its source id alongside for the case where the file was renamed since.
    The lorebook is named by its anchor, like a merge, and the names are kept
    for the same reason: so the pending list can say what it means when one
    side has gone."""

    card: str
    lorebook: str
    op: str  # "attach" or "detach"
    card_id: str = ""
    at: str = ""
    card_name: str = ""
    lorebook_name: str = ""

    @property
    def id(self) -> str:
        return digest("\x00".join((self.card, self.lorebook)))[:12]


@dataclass(slots=True)
class Decisions:
    lorebook_merges: list[Merge] = field(default_factory=list)
    entry_merges: list[Merge] = field(default_factory=list)
    # Entry-version hash -> when it was chosen. Where one entry holds two
    # chosen versions (possible after a merge), the later choice stands.
    chosen: dict[str, str] = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)


def _attachments(value: Any) -> list[Attachment]:
    out: list[Attachment] = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict):
            continue
        card, lorebook, op = row.get("card"), row.get("lorebook"), row.get("op")
        if isinstance(card, str) and card and isinstance(lorebook, str) and lorebook and op in ("attach", "detach"):
            extra = {k: row[k] for k in ("card_id", "at", "card_name", "lorebook_name") if isinstance(row.get(k), str)}
            out.append(Attachment(card=card, lorebook=lorebook, op=op, **extra))
    return out


def _merges(value: Any) -> list[Merge]:
    out: list[Merge] = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict):
            continue
        a, b = row.get("a"), row.get("b")
        if isinstance(a, str) and isinstance(b, str) and a and b and a != b:
            out.append(
                Merge(
                    a=a,
                    b=b,
                    at=row.get("at") if isinstance(row.get("at"), str) else "",
                    a_name=row.get("a_name") if isinstance(row.get("a_name"), str) else "",
                    b_name=row.get("b_name") if isinstance(row.get("b_name"), str) else "",
                )
            )
    return out


class DecisionsStore:
    """Read-modify-write over one JSON file. Uncached, like `IgnoredStore`: a
    hand-edit should take effect without a restart, and the index notices one
    through `key()`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def key(self) -> tuple[int, int]:
        """Changes whenever the file does -- what the index invalidates on."""
        try:
            st = self.path.stat()
        except OSError:
            return (0, 0)
        return (st.st_mtime_ns, st.st_size)

    def read(self) -> Decisions:
        """Empty when the file is missing. Raises when it exists but does not
        parse: reading a damaged file as "no decisions" would let the next
        write replace every decision in it."""
        if not self.path.is_file():
            return Decisions()
        try:
            blob = json.loads(self.path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DecisionsError(f"could not read {self.path}: {exc}") from exc
        if not isinstance(blob, dict):
            raise DecisionsError(f"{self.path} holds a {type(blob).__name__}, expected a JSON object")
        chosen = blob.get("chosen")
        return Decisions(
            lorebook_merges=_merges(blob.get("lorebook_merges")),
            entry_merges=_merges(blob.get("entry_merges")),
            chosen={
                k: v for k, v in (chosen if isinstance(chosen, dict) else {}).items() if isinstance(v, str)
            },
            attachments=_attachments(blob.get("attachments")),
        )

    def _write(self, decisions: Decisions) -> None:
        def rows(merges: list[Merge], names: bool) -> list[dict[str, str]]:
            return [
                {"a": m.a, "b": m.b, "at": m.at, **({"a_name": m.a_name, "b_name": m.b_name} if names else {})}
                for m in merges
            ]

        payload = json.dumps(
            {
                "version": 1,
                "lorebook_merges": rows(decisions.lorebook_merges, True),
                "entry_merges": rows(decisions.entry_merges, False),
                "chosen": decisions.chosen,
                "attachments": [
                    {
                        "card": a.card,
                        "card_id": a.card_id,
                        "lorebook": a.lorebook,
                        "op": a.op,
                        "at": a.at,
                        "card_name": a.card_name,
                        "lorebook_name": a.lorebook_name,
                    }
                    for a in decisions.attachments
                ],
            },
            ensure_ascii=False,
            indent=1,
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(temporary, self.path)
            except OSError:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise DecisionsError(f"could not write {self.path}: {exc}") from exc

    # --- mutations -----------------------------------------------------------

    def merge_lorebooks(self, a: str, b: str, a_name: str = "", b_name: str = "") -> Merge:
        decisions = self.read()
        merge = Merge(a=a, b=b, at=_now(), a_name=a_name, b_name=b_name)
        if all(m.id != merge.id for m in decisions.lorebook_merges):
            decisions.lorebook_merges.append(merge)
            self._write(decisions)
        return merge

    def merge_entries(self, a: str, b: str) -> Merge:
        decisions = self.read()
        merge = Merge(a=a, b=b, at=_now())
        if all(m.id != merge.id for m in decisions.entry_merges):
            decisions.entry_merges.append(merge)
            self._write(decisions)
        return merge

    def unmerge(self, kind: str, merge_id: str) -> bool:
        """Drop one merge. `kind` is "lorebook" or "entry". False when there was
        no such merge."""
        decisions = self.read()
        merges = decisions.lorebook_merges if kind == "lorebook" else decisions.entry_merges
        kept = [m for m in merges if m.id != merge_id]
        if len(kept) == len(merges):
            return False
        if kind == "lorebook":
            decisions.lorebook_merges = kept
        else:
            decisions.entry_merges = kept
        self._write(decisions)
        return True

    def choose(self, hashes: list[str], *, clear: list[str] | None = None) -> None:
        """Mark versions as chosen, and drop any choice among `clear`."""
        decisions = self.read()
        for stale in clear or ():
            decisions.chosen.pop(stale, None)
        stamp = _now()
        for value in hashes:
            decisions.chosen[value] = stamp
        self._write(decisions)

    def set_attachment(
        self,
        card: str,
        lorebook: str,
        op: str | None,
        *,
        card_id: str = "",
        card_name: str = "",
        lorebook_name: str = "",
    ) -> None:
        """Record that `lorebook` should be attached to (or detached from)
        `card` at the next sync, replacing any earlier decision about the pair.
        `op=None` only withdraws one."""
        decisions = self.read()
        attachment = Attachment(
            card=card,
            lorebook=lorebook,
            op=op or "attach",
            card_id=card_id,
            at=_now(),
            card_name=card_name,
            lorebook_name=lorebook_name,
        )
        kept = [a for a in decisions.attachments if a.id != attachment.id]
        if op is not None:
            kept.append(attachment)
        elif len(kept) == len(decisions.attachments):
            return
        decisions.attachments = kept
        self._write(decisions)

    def drop_attachments(self, ids: set[str]) -> None:
        """Forget attachments a sync has carried out, or found moot."""
        if not ids:
            return
        decisions = self.read()
        kept = [a for a in decisions.attachments if a.id not in ids]
        if len(kept) != len(decisions.attachments):
            decisions.attachments = kept
            self._write(decisions)
