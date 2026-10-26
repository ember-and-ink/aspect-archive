from __future__ import annotations

import re

from aspect.cards.models import ParsedDefinition

# Tags never picked as the character-name tag (ported from scrapitor's
# DEFAULT_SKIP_TAGS_FOR_NAME). Real JanitorAI captures only ever produce
# "<Name's Persona>", "<Scenario>", "<example_dialogs>" (verified against
# every fixture in tests/fixtures/system_prompt_*.txt) -- "persona"/
# "userpersona" are kept for forward-compatibility with prompt formats seen
# in other JanitorAI export tooling.
_SKIP_TAGS_FOR_NAME = {"system", "scenario", "example_dialogs", "persona", "userpersona"}

# An opening tag: `<name>` where `name` may itself contain `/` (real chat_names
# like "Celeste // Brightstar" produce `<Celeste // Brightstar's Persona>`). The
# `(?!/)` lookahead is what keeps this from also matching *closing* tags
# (`</name>`) -- we reject a `/` only as the first character, not inside the name.
# (Excluding `/` outright, as an earlier version did, meant a slash in the
# character name hid the persona tag, so the parser latched onto an inner tag and
# keyed the capture under the wrong name -- see test_slash_in_name_* .)
#
# The group takes the name with its surrounding whitespace and the caller strips
# it. Trimming inside the pattern (`<\s*(...+?)\s*>`) gave three adjacent
# quantifiers that all match a space, so `<` followed by a long run of spaces
# and no `>` backtracked quadratically -- and this runs on a captured prompt.
_OPEN_TAG_RE = re.compile(r"<(?!/)([^<>]+)>")

# Support the straight apostrophe plus common curly/typographic variants a
# creator's name might carry in from copy-paste.
_PERSONA_SUFFIX_RE = re.compile(r"^(.+?)[\'’ʼʻʽ]s\s+persona$", re.IGNORECASE)


def _compile_tag_pair(name: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    open_re = re.compile(rf"<\s*{re.escape(name)}\b[^>]*>", re.IGNORECASE)
    close_re = re.compile(rf"</\s*{re.escape(name)}\s*>", re.IGNORECASE)
    return open_re, close_re


def _find_first_non_skipped_tag(text: str) -> tuple[str, int, int] | None:
    """First opening tag <...> whose name isn't in the skip set. Allows
    spaces/apostrophes in the tag name (e.g. "Aubrey Evans's Persona").
    Returns (name, open_start, open_end)."""
    for m in _OPEN_TAG_RE.finditer(text):
        name = m.group(1).strip()
        if name.lower() in _SKIP_TAGS_FOR_NAME:
            continue
        return name, m.start(), m.end()
    return None


def _extract_tag_content(text: str, name: str, open_end: int) -> tuple[int, str]:
    """Given <name> ending at open_end, nesting-aware walk to its matching
    close tag. Returns (block_end, inner_text). If the tag is never properly
    closed, takes everything through the end of the string rather than
    losing data."""
    open_re, close_re = _compile_tag_pair(name)
    depth = 1
    scan = open_end
    block_end = len(text)
    inner_end = block_end
    while depth > 0:
        m_open = open_re.search(text, scan)
        m_close = close_re.search(text, scan)
        if not m_close:
            break
        if m_open and m_open.start() < m_close.start():
            depth += 1
            scan = m_open.end()
            continue
        depth -= 1
        if depth == 0:
            inner_end = m_close.start()
        scan = m_close.end()
        block_end = scan
    return block_end, text[open_end:inner_end]


def _strip_persona_suffix(name: str) -> str:
    m = _PERSONA_SUFFIX_RE.match(name)
    return m.group(1).strip() if m else name


class SystemPromptParser:
    """Extracts character name/personality/scenario/mes_example out of a
    JanitorAI chat system prompt (the only place a creator-hidden definition
    is ever visible). Ported from scrapitor's app/parser/parser.py tag logic.

    Never raises -- a field it can't find is just left "". The raw prompt is
    always preserved on the returned ParsedDefinition so a future parser fix
    can re-parse a capture without having lost anything.
    """

    def parse(self, raw_system: str | None) -> ParsedDefinition:
        text = (raw_system or "").replace("\\n", "\n")

        name = ""
        personality = ""
        char_start = char_end = -1

        first = _find_first_non_skipped_tag(text)
        if first:
            tag_name, open_start, open_end = first
            char_start = open_start
            char_end, inner = _extract_tag_content(text, tag_name, open_end)
            name = _strip_persona_suffix(tag_name.strip())
            personality = inner.strip()

        scenario_parts: list[str] = []
        scenario_block_end = -1
        scenario_open_re, _ = _compile_tag_pair("scenario")
        m_scenario = scenario_open_re.search(text)
        if m_scenario:
            # Only take <Scenario> as a sibling block -- if it's nested
            # inside the character block it's already covered by
            # `personality` and pulling it out again would duplicate it.
            nested_in_char = char_start != -1 and char_start <= m_scenario.start() < char_end
            if not nested_in_char:
                scenario_block_end, scenario_inner = _extract_tag_content(
                    text, "scenario", m_scenario.end()
                )
                if scenario_inner.strip():
                    scenario_parts.append(scenario_inner.strip())

        mes_example = ""
        example_block_end = -1
        example_open_re, _ = _compile_tag_pair("example_dialogs")
        m_example = example_open_re.search(text)
        if m_example:
            example_block_end, example_inner = _extract_tag_content(
                text, "example_dialogs", m_example.end()
            )
            mes_example = example_inner.strip()

        # Real captures show JanitorAI sometimes trails genuine
        # creator-authored content (extra world-info / behavior notes) after
        # the last recognized closing tag rather than inside one -- e.g. an
        # "Ravenwood Academy" world-info block after </example_dialogs>, or a
        # "Chat Behavior" / "Sex and Intimacy" section after </Scenario>.
        # Fold it into scenario instead of silently dropping real content;
        # `raw` always has the untouched original regardless.
        last_recognized_end = max(char_end, scenario_block_end, example_block_end)
        if last_recognized_end > 0:
            trailing = text[last_recognized_end:].strip()
            if trailing:
                scenario_parts.append(trailing)

        return ParsedDefinition(
            name=name,
            personality=personality,
            scenario="\n\n".join(scenario_parts),
            mes_example=mes_example,
            first_mes="",
            raw=raw_system or "",
        )
