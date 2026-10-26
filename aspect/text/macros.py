from __future__ import annotations

import re

# SillyTavern only understands {{user}} and {{char}}. JanitorAI additionally
# injects pronoun macros derived from the persona's pronouns ({{sub}}, {{obj}},
# {{poss}}, {{poss_p}}, {{ref}}). ST would render these as literal text,
# breaking the injection. The conversation/persona settles pronouns anyway, so
# we don't need the "real" pronoun -- we just fold them all to {{user}} so ST
# sees a macro it can resolve.
_JAI_PRONOUN_RE = re.compile(r"\{\{\s*(sub|obj|poss_p|poss|ref)\s*\}\}", re.IGNORECASE)

# poss_p MUST precede poss in the alternation (ordered match) so "{poss_p}"
# isn't half-eaten as "{poss}".
_KNOWN_MACRO_NAMES = ("user", "char", "sub", "obj", "poss_p", "poss", "ref")

# A lot of creators hand-write macros with the wrong bracket count -- {char},
# {{char} (missing a brace), etc. These don't resolve on JanitorAI either
# (which needs exactly {{name}}), so the cards are genuinely broken at the
# source -- promoting them to canonical double-bracket form is a pure fix, not
# a reinterpretation. A closing brace is required immediately after the name,
# so "{characters}" and "{user_name}" never match -- no greedy corruption of
# real words. We only repair a KNOWN macro name; bare {word} prose is
# untouched.
_BROKEN_MACRO_RE = re.compile(
    r"\{{1,2}\s*(" + "|".join(_KNOWN_MACRO_NAMES) + r")\s*\}{1,2}",
    re.IGNORECASE,
)

_MACRO_TOKEN_RE = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")
# SillyTavern resolves {{user}} and {{char}} everywhere; it also resolves
# {{original}} inside system_prompt / post_history_instructions (it expands to
# the default prompt content). Chub cards routinely carry {{original}} there, so
# it's a legitimate macro, not an "unresolved" one -- don't warn on it.
_ST_KNOWN_MACROS = frozenset({"user", "char", "original", "weekday", "date"})

# Common misspellings of the two macro names creators hand-type. A slipped
# keystroke -- {{usee}}, {{uesr}} -- yields a macro neither JanitorAI nor
# SillyTavern resolves, so it renders as literal text (surfaced as an
# "unresolved macro" warning). These tokens only ever appear inside macro
# braces, never as prose, so mapping each to its intended name is a pure fix,
# same as the broken-bracket repair. Keys must be lowercase.
_MACRO_TYPOS = {
    "use": "user",
    "𝙐𝙨𝙚𝙧": "user",
    "usee": "user",
    "usser": "user",
    "users": "user",
    "uesr": "user",
    "usre": "user",
    "userr": "user",
    "usr": "user",
    "suer": "user",
    "sun": "user",
    "spouse": "user",
    "random_user_1": "user",
    "ob": "user",
    "chr": "char",
    "cahr": "char",
    "chra": "char",
    "charr": "char",
    # "chars" is the plural slip (mirrors "users" -> user); "chair" is a
    # keystroke slip of "char" seen in the wild ("{{chair}} is {{user}}'s
    # classmate"). Both only ever match inside macro braces, never as prose.
    "chars": "char",
    "chair": "char",
    "𝘾𝙝𝙖𝙧": "char",
    "cha": "char",
}

# Same bracket tolerance as _BROKEN_MACRO_RE (1-2 braces each side) so a typo
# with a dropped brace ({usee}) is corrected too.
_MACRO_TYPO_RE = re.compile(
    r"\{{1,2}\s*(" + "|".join(_MACRO_TYPOS) + r")\s*\}{1,2}",
    re.IGNORECASE,
)


def _repair_macros(text: str) -> str:
    return _BROKEN_MACRO_RE.sub(lambda m: "{{" + m.group(1).lower() + "}}", text)


def _correct_typos(text: str) -> str:
    return _MACRO_TYPO_RE.sub(lambda m: "{{" + _MACRO_TYPOS[m.group(1).lower()] + "}}", text)


def _fold_pronouns(text: str) -> str:
    return _JAI_PRONOUN_RE.sub("{{user}}", text)


def _unknown_macros(text: str) -> list[str]:
    found = {
        m.group(1).lower()
        for m in _MACRO_TOKEN_RE.finditer(text)
        if m.group(1).lower() not in _ST_KNOWN_MACROS
    }
    return sorted(found)


def reverse_persona_names(text: str, names: list[str]) -> str:
    """Substitutes literal persona-name strings (e.g. "USER") back to
    {{user}}. Hidden-card captures render the persona name as typed literal
    text rather than a macro, so this repairs the definition/greeting text
    to the macro form ST expects. Word-boundary, case-sensitive, longest
    name first so a shorter name can't half-eat a longer one."""
    if not text or not names:
        return text
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(name)}\b", "{{user}}", text)
    return text


class MacroSanitizer:
    """Repairs broken-bracket JanitorAI macros and folds JanitorAI's pronoun
    macros down to {{user}}/{{char}}, which are the only two SillyTavern
    resolves. Pure function -- no state, no I/O."""

    def __init__(self, user_names: list[str] | None = None) -> None:
        self._user_names = user_names or []

    def sanitize(self, text: str | None) -> tuple[str, list[str]]:
        if not text:
            return text or "", []
        # Repair brackets FIRST (so {obj} -> {{obj}}), correct obvious typos
        # ({{usee}} -> {{user}}), then fold pronouns.
        repaired = _repair_macros(text)
        corrected = _correct_typos(repaired)
        folded = _fold_pronouns(corrected)
        return folded, _unknown_macros(folded)

    def reverse_names(self, text: str) -> str:
        return reverse_persona_names(text, self._user_names)
