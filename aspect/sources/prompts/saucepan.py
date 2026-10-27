from __future__ import annotations

import re

from aspect.text.macros import reverse_persona_names
from aspect.cards.models import ParsedDefinition

# A section header on its own line: "[ Background ]", "[ Critical Instructions ]",
# "[ User Description ]". Line-anchored so it can't match a header mentioned
# in prose.
_HEADER_RE = re.compile(r"^\[ (.+?) \]$", re.MULTILINE)

# "The user is roleplaying <HANDLE>." names the account handle saucepan
# substitutes for {{user}} throughout the prompt.
_HANDLE_RE = re.compile(r"The user is roleplaying (.+?)\.")

# "You are roleplaying <NAME>[, <NAME2>...] and any side characters..." names
# the primary character(s); multi-character cards list several comma-separated
# -- take the first.
_NAME_RE = re.compile(r"You are roleplaying (.+?) and any side characters")

_BACKGROUND = "Background"
_CRITICAL_INSTRUCTIONS = "Critical Instructions"
_USER_DESCRIPTION = "User Description"


def _sections(text: str) -> dict[str, str]:
    """Split on `^\\[ Label \\]$` header lines into {label: content}, trimmed.
    Text before the first header (saucepan scaffolding) is discarded here --
    callers never see it."""
    headers = list(_HEADER_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(headers):
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        sections[m.group(1).strip()] = text[start:end].strip()
    return sections


def is_saucepan_prompt(system: str | None) -> bool:
    """Heuristic detector for a saucepan-shaped chat system prompt (as
    opposed to JanitorAI's tag-based format). Not used for routing --
    routing is by endpoint path -- but kept for safety checks/logging."""
    text = system or ""
    return f"[ {_USER_DESCRIPTION} ]" in text or "and any side characters" in text


class SaucepanPromptParser:
    """Extracts character name/description/scenario out of a saucepan chat
    system prompt -- sent verbatim, in plaintext, by saucepan's Custom API
    Provider, the only place a creator-hidden saucepan definition is ever
    visible. Peer of prompt_parser.SystemPromptParser for JanitorAI.

    Section mapping (validated byte-exact against a real open-card capture,
    see docs/SAUCEPAN_HIDDEN_CAPTURE_PLAN.md §4): `[ Background ]` ->
    personality (-> ProfileFields.description downstream), `[ Critical
    Instructions ]` -> scenario. There is no `[ Example Dialogue ]` section in
    the chat prompt, so mes_example always comes back "". `{{user}}` is
    reversed in-place using the account handle named in the preamble, so the
    returned fields are ready to feed straight into CardBuilder like any other
    capture.

    Never raises -- a field it can't find is just left "". The raw prompt is
    always preserved on the returned ParsedDefinition.
    """

    def parse(self, raw_system: str | None) -> ParsedDefinition:
        text = raw_system or ""
        sections = _sections(text)

        name = ""
        name_match = _NAME_RE.search(text)
        if name_match:
            name = name_match.group(1).split(",")[0].strip()

        handle_match = _HANDLE_RE.search(text)
        handle = handle_match.group(1).strip() if handle_match else ""

        personality = sections.get(_BACKGROUND, "")
        scenario = sections.get(_CRITICAL_INSTRUCTIONS, "")

        if handle:
            personality = reverse_persona_names(personality, [handle])
            scenario = reverse_persona_names(scenario, [handle])

        return ParsedDefinition(
            name=name,
            personality=personality,
            scenario=scenario,
            mes_example="",
            first_mes="",
            raw=text,
        )
