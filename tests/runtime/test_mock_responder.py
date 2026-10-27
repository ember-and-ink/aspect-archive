import json
from pathlib import Path

from aspect.config import settings
from aspect.runtime.mock_responder import MockResponder

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _real_request() -> dict:
    """A real captured JanitorAI chat request: [system(hidden def), user ".",
    assistant(rendered greeting), user "USER: hello"]."""
    return json.loads(
        (FIXTURES / "chat_request_hidden_ari.json").read_text(encoding="utf-8")
    )


def _messages(system: str = "<Ari's Persona>cold</Ari's Persona>", user: str = "hello"):
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# The reply itself
# ---------------------------------------------------------------------------


def test_reply_addresses_the_character_by_parsed_name():
    reply = MockResponder(seed=0).reply({"messages": _real_request()["messages"]})
    assert "Ari" in reply


def test_reply_is_roleplay_shaped():
    """An action beat in asterisks and a line in quotes -- the format the
    receiving site renders as italics + speech."""
    reply = MockResponder(seed=3).reply({"messages": _messages()})
    assert reply.startswith("*")
    assert '"' in reply


def test_reply_varies_between_turns():
    responder = MockResponder(seed=1)
    replies = {responder.reply({"messages": _messages()}) for _ in range(12)}
    assert len(replies) > 5


def test_persona_prefix_is_stripped_before_reading_the_user_turn():
    """Real requests carry "USER: hello", not "hello" -- with the prefix left on,
    every greeting reads as a statement about a topic called "USER"."""
    responder = MockResponder(seed=4)
    replies = [
        responder.reply({"messages": _messages(user="USER: hello")}) for _ in range(20)
    ]
    assert not any("USER" in r or "hello" in r.lower() for r in replies)
    assert any(line in r for r in replies for line in ("Hey yourself.", "There you are"))


def test_reply_survives_a_prompt_with_no_parseable_name():
    reply = MockResponder(seed=0).reply({"messages": _messages(system="just prose")})
    assert reply.startswith("*")
    assert "{char}" not in reply


def test_reply_survives_an_empty_request():
    reply = MockResponder(seed=0).reply({})
    assert reply.strip()
    assert "{char}" not in reply and "{topic}" not in reply


def test_every_placeholder_is_filled_across_many_draws():
    """Regression net for a fragment whose {slot} nobody passes -- it would show
    up verbatim in a chat rather than raising."""
    responder = MockResponder(seed=7)
    for text in ("hello there", "what about the greenhouse?", "*reaches out*"):
        for _ in range(60):
            reply = responder.reply({"messages": _messages(user=text)})
            assert "{" not in reply and "}" not in reply


def test_small_max_tokens_drops_the_trailing_beat():
    responder = MockResponder(seed=2)
    for _ in range(20):
        reply = responder.reply({"messages": _messages(), "max_tokens": 60})
        assert reply.count("*") == 2  # opening beat only, no closer


def test_a_user_action_gets_a_reaction_beat():
    responder = MockResponder(seed=5)
    replies = [
        responder.reply({"messages": _messages(user="*waves*")}) for _ in range(10)
    ]
    assert any("without" in r or "watches that happen" in r or "still" in r for r in replies)


# ---------------------------------------------------------------------------
# OpenAI-shaped envelopes
# ---------------------------------------------------------------------------


async def test_complete_returns_an_openai_shaped_completion():
    body = _real_request() | {"stream": False}
    completion = await MockResponder(seed=0).complete(body)

    assert completion["object"] == "chat.completion"
    assert completion["id"].startswith("chatcmpl-")
    assert isinstance(completion["created"], int)
    choice = completion["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"].strip()
    usage = completion["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["prompt_tokens"] > 0


async def test_complete_echoes_the_requested_model():
    """The site puts its own model id in its provider settings; echoing it back
    keeps that consistent. Absent one, the advertised default stands in."""
    body = {"messages": _messages(), "model": "Llama-3.2-3B-Instruct-4bit"}
    assert (await MockResponder().complete(body))["model"] == "Llama-3.2-3B-Instruct-4bit"
    assert (await MockResponder().complete({}))["model"] == settings.mock_model


async def test_stream_emits_valid_sse_that_reassembles_to_the_reply():
    frames = [f async for f in MockResponder(seed=0).stream({"messages": _messages()})]

    assert frames[-1] == b"data: [DONE]\n\n"
    chunks = [json.loads(f.decode().removeprefix("data: ").strip()) for f in frames[:-1]]

    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert len({c["id"] for c in chunks}) == 1, "one id for the whole stream"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert all(c["choices"][0]["finish_reason"] is None for c in chunks[:-1])

    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text.startswith("*") and '"' in text
    # More than one content frame, i.e. it actually streams rather than
    # dumping the whole reply in a single chunk the way the MLX path had to.
    assert sum(1 for c in chunks if c["choices"][0]["delta"].get("content")) > 1
