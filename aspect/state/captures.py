import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from aspect.config import ensure_dir, settings
from aspect.cards.models import CaptureRecord
from aspect.sources.prompts.janitor import SystemPromptParser

logger = logging.getLogger("aspect.state.captures")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize(name: str) -> str:
    """Lookup key for CaptureStore: lowercase, trimmed."""
    return (name or "").strip().lower()


def _slug(key: str) -> str:
    slug = _SLUG_RE.sub("_", key).strip("_")
    return slug or "unnamed"


class CaptureStore:
    """Archives every raw hidden-definition system prompt as a numbered
    .txt file (unconditional format-drift safety net -- see PLAN.md §4,
    always kept even if parsing fails) AND parses each prompt via
    SystemPromptParser into a CaptureRecord upserted by normalize(name),
    persisted as `{captures_dir}/{slug}.json` and held in memory so
    CardBuilder can merge the hidden definition into a `/build-jai` (get()).
    """

    def __init__(
        self,
        captures_dir: Path | None = None,
        parser: SystemPromptParser | None = None,
    ) -> None:
        self._raw_prompts: list[str] = []
        self._captures_dir = captures_dir or settings.captures_dir
        # ensure_dir, not mkdir: this runs at `import aspect.deps`, before the
        # server's preflight -- see aspect.config.ensure_dir.
        ensure_dir(self._captures_dir)
        self._parser = parser or SystemPromptParser()
        self._records: dict[str, CaptureRecord] = {}
        self._load_existing_records()

    def record(self, system_prompt: str, primary_greeting: str | None = None) -> None:
        """Archive + parse a hidden-definition chat system prompt. The same
        chat relay also carries the card's primary greeting as its first
        `assistant` message (the definition is hidden but the greeting is
        rendered into the chat), so the server passes it in here: a single
        chat message now captures BOTH halves a hidden card needs."""
        if not system_prompt:
            return
        self._raw_prompts.append(system_prompt)
        n = len(self._raw_prompts)
        logger.info(
            "captured system prompt (#%d, %d chars):\n%s",
            n,
            len(system_prompt),
            system_prompt,
        )
        path = self._write_raw(n, system_prompt)
        logger.info("wrote capture to %s", path)

        parsed = self._parser.parse(system_prompt)
        key = normalize(parsed.name)
        if not key:
            logger.warning(
                "capture #%d: parser found no character name; raw prompt archived "
                "but no CaptureRecord upserted",
                n,
            )
            return

        existing = self._records.get(key)
        # A non-empty primary greeting from this relay wins; otherwise keep
        # whatever the previous record held so a later definition-only capture
        # (no assistant message yet) doesn't wipe a greeting already captured.
        greeting = (primary_greeting or "").strip()
        if greeting:
            greetings = [greeting]
        else:
            greetings = existing.greetings if existing else []
        # lore_entries deliberately stays whatever the previous record had
        # (empty, today) rather than trying to mine lore out of raw prompt
        # text. Real open-card captures showed JanitorAI appends an activated
        # lore entry's content completely undelimited -- no tags, no keys, no
        # entry boundary -- indistinguishable from more scenario prose.
        # SystemPromptParser
        # already folds that trailing text into `scenario` rather than
        # fabricate a structured LoreEntry with invented keys (see its
        # comment). The real, keyed source of lore is the /hampter/script
        # fetch path (lorebook.LorebookMapper.map(), wired in via /build-jai's
        # `lorebooks` payload) -- that's what populates character_book.
        record = CaptureRecord(
            name=parsed.name,
            personality=parsed.personality,
            scenario=parsed.scenario,
            mes_example=parsed.mes_example,
            raw_system_prompt=system_prompt,
            lore_entries=existing.lore_entries if existing else [],
            greetings=greetings,
        )
        self._records[key] = record
        self._persist(key, record)

    def get(self, name: str) -> CaptureRecord | None:
        return self._records.get(normalize(name))

    def status(self, name: str) -> dict[str, bool]:
        rec = self.get(name)
        system = rec is not None and bool(rec.personality or rec.scenario or rec.mes_example)
        greetings = rec is not None and bool(rec.greetings)
        return {"system": system, "greetings": greetings}

    def _write_raw(self, n: int, system_prompt: str) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self._captures_dir / f"system_prompt_{n:03d}_{timestamp}.txt"
        path.write_text(system_prompt, encoding="utf-8")
        return path

    def _persist(self, key: str, record: CaptureRecord) -> None:
        path = self._captures_dir / f"{_slug(key)}.json"
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")

    def _load_existing_records(self) -> None:
        if not self._captures_dir.is_dir():
            return
        for path in sorted(self._captures_dir.glob("*.json")):
            try:
                record = CaptureRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("failed to load capture record from %s", path)
                continue
            self._records[normalize(record.name)] = record

    def clear(self) -> int:
        """Wipe all captured state: in-memory records/raw prompts and every
        file under captures_dir (.txt raw prompts + .json records). PNGs
        live under archive_dir, a sibling directory, so they're untouched.
        """
        removed = 0
        for path in self._captures_dir.glob("*"):
            if path.is_file():
                path.unlink()
                removed += 1
        self._records.clear()
        self._raw_prompts.clear()
        return removed

    @property
    def count(self) -> int:
        return len(self._raw_prompts)
