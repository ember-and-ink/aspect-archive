from __future__ import annotations

import json
import logging
from typing import Any

from aspect.text.macros import MacroSanitizer
from aspect.cards.models import CharacterBook, LoreEntry

logger = logging.getLogger("aspect.lorebook")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _looks_like_entry(e: Any) -> bool:
    """A real lorebook entry has prose `content` plus at least one keying
    signal. Ported from janitorai-export.user.js's looksLikeEntry (~L732)."""
    if not isinstance(e, dict) or not isinstance(e.get("content"), str):
        return False
    return (
        isinstance(e.get("key"), list)
        or isinstance(e.get("keysRaw"), str)
        or isinstance(e.get("constant"), bool)
    )


def _looks_like_book(arr: Any) -> bool:
    return isinstance(arr, list) and len(arr) > 0 and all(_looks_like_entry(e) for e in arr)


class LorebookMapper:
    """Maps raw JanitorAI /hampter/script responses into a V3 character_book.
    Ported from ~/workspaces/saucepan/janitorai-export.user.js's mapLoreEntry
    (~L648) and buildLorebook (~L708) -- the live "janitorai-export"
    Tampermonkey userscript, NOT the unrelated "saucepan" project that
    happens to share the same ~/workspaces/saucepan/ directory.

    `type === "lorebook"` is necessary but not sufficient -- JanitorAI engine
    "scripts" can carry that same type while their `script` field is code,
    not a JSON array of world-info entries. Those are detected (and skipped,
    logged as a warning rather than an error) via looksLikeEntry/looksLikeBook.
    """

    def __init__(self, sanitizer: MacroSanitizer | None = None) -> None:
        self._sanitizer = sanitizer or MacroSanitizer()

    def _map_entry(
        self,
        e: dict[str, Any],
        entry_id: int,
        display_index: int,
        book_title: str,
        multi_book: bool,
    ) -> tuple[LoreEntry, list[str]]:
        warnings: list[str] = []

        keys = [k for k in e.get("key") or [] if k] if isinstance(e.get("key"), list) else []
        secondary = (
            [k for k in e.get("keysecondary") or [] if k]
            if isinstance(e.get("keysecondary"), list)
            else []
        )

        comment = e.get("comment") or e.get("name") or ""
        if multi_book and book_title:
            comment = f"[{book_title}] {comment}"

        probability = e.get("probability") if _is_number(e.get("probability")) else 100
        order = e.get("insertion_order") if _is_number(e.get("insertion_order")) else 100

        mww = e.get("matchWholeWords")
        if mww is not True and mww is not False:
            mww = None

        content, unknown = self._sanitizer.sanitize(e.get("content") or "")
        for macro_name in unknown:
            warnings.append(f"unresolved macro in lore entry: {{{{{macro_name}}}}}")

        entry = LoreEntry(
            id=entry_id,
            keys=keys,
            secondary_keys=secondary,
            comment=comment,
            content=content,
            constant=bool(e.get("constant")),
            selective=len(secondary) > 0,
            insertion_order=int(order),
            enabled=e.get("enabled") is not False,
            position="before_char",
            use_regex=False,
            name=e.get("name") or "",
            case_sensitive=bool(e.get("case_sensitive")),
            extensions={
                "position": 0,
                "exclude_recursion": False,
                "display_index": display_index,
                "probability": probability,
                "useProbability": probability < 100,
                "depth": 4,
                "selectiveLogic": e.get("selectiveLogic") if _is_number(e.get("selectiveLogic")) else 0,
                "group": e.get("inclusionGroupRaw") or "",
                "group_override": False,
                "group_weight": e.get("groupWeight") if _is_number(e.get("groupWeight")) else 100,
                "prevent_recursion": False,
                "delay_until_recursion": False,
                "scan_depth": None,
                "match_whole_words": mww,
                "use_group_scoring": False,
                "case_sensitive": bool(e.get("case_sensitive")),
                "automation_id": "",
                "role": 0,
                "vectorized": False,
                "sticky": 0,
                "cooldown": 0,
                "delay": 0,
                "jai": {
                    "id": e.get("id"),
                    "category": e.get("category") or "",
                    "tags": e.get("tags") if isinstance(e.get("tags"), list) else [],
                    "priority": e.get("priority"),
                    "activationMode": e.get("activationMode"),
                    "keyMatchPriority": e.get("keyMatchPriority"),
                },
            },
        )
        return entry, warnings

    def map(
        self, raw_scripts: list[dict[str, Any]], character_name: str = ""
    ) -> tuple[CharacterBook | None, list[str]]:
        """Fetch every attached lorebook and merge them into a single V3
        character_book (a card embeds only one). Returns (None, warnings) if
        none of the raw scripts are valid lorebooks."""
        warnings: list[str] = []

        candidates = [s for s in raw_scripts if isinstance(s, dict) and s.get("type") == "lorebook"]
        if not candidates:
            return None, warnings

        # First pass: parse + validate each candidate so we know how many
        # real books there are before deciding whether to prefix entry
        # comments with the title.
        books: list[dict[str, Any]] = []
        for meta in candidates:
            title = meta.get("title") or ""
            script_id = meta.get("id") or ""
            raw_entries = meta.get("script")
            if isinstance(raw_entries, str):
                try:
                    raw_entries = json.loads(raw_entries)
                except (TypeError, ValueError):
                    w = (
                        f'skipping "{title}" ({script_id}) -- script field is not JSON '
                        "(likely a true script, not a lorebook)"
                    )
                    logger.warning(w)
                    warnings.append(w)
                    continue
            if not _looks_like_book(raw_entries):
                w = (
                    f'skipping "{title}" ({script_id}) -- parsed but not lorebook-shaped '
                    "(likely a true script)"
                )
                logger.warning(w)
                warnings.append(w)
                continue
            books.append({"id": script_id, "meta": meta, "entries": raw_entries})

        if not books:
            return None, warnings

        multi_book = len(books) > 1
        entries: list[LoreEntry] = []
        sources: list[dict[str, Any]] = []
        max_depth: int | float | None = None

        for book in books:
            meta = book["meta"]
            title = meta.get("title") or ""

            # Context depth lives in the `settings` JSON string
            # ({"depth":3}); `depth` may also be a top-level field.
            depth = meta.get("depth") if _is_number(meta.get("depth")) else None
            if depth is None and isinstance(meta.get("settings"), str):
                try:
                    settings_obj = json.loads(meta["settings"])
                    if _is_number(settings_obj.get("depth")):
                        depth = settings_obj["depth"]
                except (TypeError, ValueError):
                    pass
            if depth is not None:
                max_depth = depth if max_depth is None else max(max_depth, depth)

            for e in book["entries"]:
                entry, entry_warnings = self._map_entry(
                    e, len(entries), len(entries), title, multi_book
                )
                entries.append(entry)
                warnings.extend(entry_warnings)

            sources.append(
                {"id": book["id"], "title": title, "depth": depth, "entry_count": len(book["entries"])}
            )

        if not entries:
            return None, warnings

        if multi_book:
            name = f"{character_name} — JanitorAI lorebooks" if character_name else "JanitorAI lorebooks"
        else:
            name = books[0]["meta"].get("title") or (
                f"{character_name} lorebook" if character_name else "lorebook"
            )
        description = "\n\n".join(
            d for d in (b["meta"].get("description") or "" for b in books) if d
        )

        book_obj = CharacterBook(
            name=name,
            description=description,
            scan_depth=max_depth,
            token_budget=None,
            recursive_scanning=False,
            extensions={"jai_sources": sources},
            entries=entries,
        )
        return book_obj, warnings

    @staticmethod
    def _dedupe_key(entry: LoreEntry) -> tuple[tuple[str, ...], str]:
        return (tuple(sorted(entry.keys)), entry.content)

    def merge(self, book: CharacterBook | None, accumulated: list[LoreEntry]) -> CharacterBook | None:
        """Union `accumulated` (entries the CaptureStore scraped from
        multi-message system prompts) into `book`, deduped by (keys,
        content). As of M5, CaptureStore never actually populates
        `accumulated` -- see capture_store.py's comment for why -- so in
        practice this is always a no-op passthrough today. It's still real,
        tested logic: the moment a real accumulation source exists, this is
        ready to use it."""
        if not accumulated:
            return book

        entries = list(book.entries) if book else []
        seen = {self._dedupe_key(e) for e in entries}
        for e in accumulated:
            key = self._dedupe_key(e)
            if key in seen:
                continue
            entries.append(e)
            seen.add(key)

        if book is None:
            return CharacterBook(name="", entries=entries)
        if len(entries) == len(book.entries):
            return book
        return book.model_copy(update={"entries": entries})
