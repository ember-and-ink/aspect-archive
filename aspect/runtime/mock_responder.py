"""The chat upstream, replacing the MLX client.

Nothing in this project needs a language model. The `/v1/chat/completions`
route exists because pointing JanitorAI (or saucepan) at it as a custom API
provider is what makes those sites hand over a creator-hidden definition in the
system prompt -- the reply is a side effect, not the product. Forwarding to a
real MLX server for that side effect cost a macOS-only dependency and a second
process, which is the last thing that kept the server off a container.

So the reply is assembled locally out of pre-baked fragments. It is not smart
and does not read the definition it was given: it picks a beat, a line, and a
closer at random, addresses the character by the name parsed from the system
prompt, and reflects one word back from whatever was last said. That is enough
to look like a small, distracted model on the other end rather than a stub --
which matters only so a chat session doesn't visibly break mid-capture.
"""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from aspect.config import settings
from aspect.sources.prompts.janitor import SystemPromptParser

# Every fragment below is written without a pronoun for the character: real
# cards are of every gender and the responder has no way to know which, so it
# leans on the name and on second person for the user instead of guessing.

_BEATS = [
    "{char} looks up, unhurried, and lets the pause stretch a beat too long.",
    "{char} leans back against the nearest solid thing, arms folded.",
    "There's a slow breath from {char} -- the kind that means a thought is "
    "being turned over.",
    "{char} tilts a glance your way, mouth curving into something that isn't "
    "quite a smile.",
    "{char} turns something small over in one hand, not really looking at it.",
    "{char} closes the distance by half a step and stops there, deliberate.",
    "{char} makes a noncommittal sound and shifts to face you properly.",
    "{char} brushes something invisible off one sleeve before answering.",
    "The corner of {char}'s expression shifts -- amused, maybe, or just tired.",
    "{char} taps two fingers against the surface nearby, thinking.",
    "{char} watches you for a moment longer than is comfortable.",
    "Whatever {char} was doing stops, unfinished, and gets set aside.",
]

_LINES = [
    "That's one way to put it.",
    "Hm. You're going to have to give me more than that.",
    "You always say things like that when you want something.",
    "Fine. I'll bite.",
    "Careful. That's the sort of thing people get talked about for.",
    "You're not as subtle as you think you are.",
    "I heard you the first time.",
    "That depends entirely on how you ask.",
    "Is that what we're doing tonight?",
    "Sure. Let's pretend I believe that.",
    "You could have led with that.",
    "Don't look so pleased with yourself.",
]

_GREETING_LINES = [
    "Well. Look who decided to show up.",
    "You're late. Or early. I've stopped keeping track.",
    "Hey yourself.",
    "There you are. I was starting to think you'd changed your mind.",
]

_QUESTION_LINES = [
    "That's a bigger question than you meant it to be.",
    "Ask me again in a minute, you might get a different answer.",
    "Do you want the honest version or the polite one?",
    "Maybe. Why -- does it matter?",
]

# Used when the last user message was itself an action (*...*) rather than
# speech, so the reply answers the gesture instead of the words.
_REACTION_BEATS = [
    "{char} watches that happen and doesn't stop it.",
    "{char} lets it land, then answers in kind.",
    "That earns a raised eyebrow from {char}, and not much else.",
    "{char} goes very still for a second, then recovers.",
]

# Slotted in when a content word can be lifted out of the last user message,
# so the reply at least appears to be about something.
_TOPIC_LINES = [
    "The {topic} can wait.",
    "You brought up the {topic}. That was your first mistake.",
    "{topic_title}. Right. Of course that's where you'd start.",
    "Ask me about the {topic} again when you're being serious.",
]

_CLOSERS = [
    "{char} waits, and doesn't fill the silence for you.",
    "A glance flicks to you, then away again.",
    "{char} lets the question sit there, unhurried.",
    "Whatever comes next, {char} isn't going to be the one to say it first.",
]

_GREETING_RE = re.compile(
    r"^\W*(hi|hey|hello|yo|sup|good (morning|evening|afternoon)|greetings)\b",
    re.IGNORECASE,
)

# Too common to be worth reflecting back as "the topic" -- a reply built around
# "the about" reads as broken in a way a bland reply does not.
_STOPWORDS = frozenset(
    """about after again against because before being between could doesn't
    during every first found their there these thing think those under where
    which while would you're your yours what when with that this from have
    just like really something someone anything nothing
    hello greetings morning evening afternoon please thanks thank
    """.split()
)

# JanitorAI prefixes the user's turn with the persona name ("USER: hello"), and
# saucepan does the same. Left in, the prefix hides the greeting from the regex
# below and offers the persona name up as a topic word.
_PERSONA_PREFIX_RE = re.compile(r"^\s*[A-Za-z0-9 _'-]{1,32}:\s*")

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{4,}")


class MockResponder:
    """Local stand-in for a chat model, keeping the `complete` / `stream` pair
    the route already called on the MLX client so nothing else had to move."""

    def __init__(
        self,
        model: str | None = None,
        parser: SystemPromptParser | None = None,
        seed: int | None = None,
    ) -> None:
        self.model = model or settings.mock_model
        self._parser = parser or SystemPromptParser()
        # Seeded only by tests; left None the responder draws from the shared
        # global source, which is what makes repeated turns differ.
        self._rng = random.Random(seed) if seed is not None else random.Random()
        # The last few fragments used, across all banks. Independent draws from
        # a twelve-item list repeat inside three turns often enough to notice,
        # and a reply repeating the previous one verbatim is the single most
        # obvious tell that there's no model here.
        self._recent: deque[str] = deque(maxlen=6)

    def _pick(self, bank: list[str]) -> str:
        fresh = [item for item in bank if item not in self._recent] or bank
        choice = self._rng.choice(fresh)
        self._recent.append(choice)
        return choice

    # -- generation ---------------------------------------------------------

    def _char_name(self, messages: list[dict[str, Any]]) -> str:
        """The character's name off the hidden-definition system prompt. The
        same parser the capture path uses, so a prompt shape it can't read
        degrades here exactly where it degrades there."""
        system = _content_of_role(messages, "system")
        try:
            name = self._parser.parse(system).name.strip()
        except Exception:
            name = ""
        # Real captures show names carrying a trailing role/blurb after a
        # separator ("Ari // Queen Bee"); the first part is the one to speak to.
        name = re.split(r"\s*(?://|[|·—-]{1,2})\s*", name)[0].strip()
        return name or "They"

    def _topic(self, last_user: str) -> str | None:
        """One content word to reflect back. Drawn from the longest few rather
        than uniformly: length is a decent free proxy for which word carried the
        meaning ("greenhouse" over "night"), and a reply built around the
        specific noun is the whole reason this exists."""
        words = [w.lower() for w in _WORD_RE.findall(last_user)]
        candidates = sorted(
            {w for w in words if w not in _STOPWORDS}, key=len, reverse=True
        )
        return self._rng.choice(candidates[:3]) if candidates else None

    def _dialogue(self, last_user: str) -> str:
        topic = self._topic(last_user)
        # A topic line only sometimes, so consecutive turns don't all read as
        # the same trick.
        if topic and self._rng.random() < 0.45:
            return self._pick(_TOPIC_LINES).format(
                topic=topic, topic_title=topic.capitalize()
            )
        if _GREETING_RE.match(last_user.strip()):
            return self._pick(_GREETING_LINES)
        if "?" in last_user:
            return self._pick(_QUESTION_LINES)
        return self._pick(_LINES)

    def reply(self, req: dict[str, Any]) -> str:
        messages = req.get("messages") or []
        char = self._char_name(messages)
        last_user = _PERSONA_PREFIX_RE.sub(
            "", _content_of_role(messages, "user", last=True)
        )
        is_action = last_user.strip().startswith("*")

        beats = _REACTION_BEATS if is_action else _BEATS
        parts = [
            "*" + self._pick(beats).format(char=char) + "*",
            '"' + self._dialogue(last_user) + '"',
        ]
        # max_tokens is the site's cap, not ours, but a 60-token budget with a
        # three-part reply would get cut mid-sentence by the caller and look
        # like a truncation bug. Drop the closer instead.
        budget = _int_or_none(req.get("max_tokens"))
        if (budget is None or budget >= 120) and self._rng.random() < 0.7:
            parts.append("*" + self._pick(_CLOSERS).format(char=char) + "*")
        return " ".join(parts)

    # -- MLXClient-shaped surface -------------------------------------------

    async def complete(self, req: dict[str, Any]) -> dict[str, Any]:
        text = self.reply(req)
        prompt_tokens = sum(
            _approx_tokens(str(m.get("content", ""))) for m in req.get("messages") or []
        )
        completion_tokens = _approx_tokens(text)
        return {
            "id": _completion_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model") or self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    async def stream(self, req: dict[str, Any]) -> AsyncIterator[bytes]:
        """Real token-by-token SSE, unlike the MLX path -- which could only ever
        emit one chunk because it had to wait for the upstream's whole reply.
        Generating the text here means it can be handed over a few words at a
        time, which is what the receiving site expects to see."""
        completion_id = _completion_id()
        model = req.get("model") or self.model
        text = self.reply(req)

        def frame(delta: dict[str, Any], finish: str | None = None) -> bytes:
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(chunk)}\n\n".encode()

        yield frame({"role": "assistant", "content": ""})
        for piece in _chunk_words(text):
            yield frame({"content": piece})
        yield frame({}, finish="stop")
        yield b"data: [DONE]\n\n"


def _content_of_role(
    messages: list[dict[str, Any]], role: str, last: bool = False
) -> str:
    matches = [m for m in messages if m.get("role") == role]
    if not matches:
        return ""
    return str((matches[-1] if last else matches[0]).get("content", "") or "")


def _chunk_words(text: str, per_chunk: int = 3) -> list[str]:
    """Split into SSE-sized pieces, keeping the spaces so a naive client that
    concatenates deltas gets the original string back."""
    words = text.split(" ")
    return [
        " ".join(words[i : i + per_chunk]) + ("" if i + per_chunk >= len(words) else " ")
        for i in range(0, len(words), per_chunk)
    ]


def _approx_tokens(text: str) -> int:
    """Rough enough for a usage block nothing bills against: ~4 chars/token."""
    return max(1, len(text) // 4) if text else 0


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
