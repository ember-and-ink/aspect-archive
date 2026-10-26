from aspect.text.macros import MacroSanitizer, reverse_persona_names


def test_empty_and_none_are_untouched():
    s = MacroSanitizer()
    assert s.sanitize("") == ("", [])
    assert s.sanitize(None) == ("", [])


def test_user_and_char_pass_through_untouched():
    s = MacroSanitizer()
    text, warnings = s.sanitize("Hello {{user}}, meet {{char}}.")
    assert text == "Hello {{user}}, meet {{char}}."
    assert warnings == []


def test_repairs_single_brace_macros():
    s = MacroSanitizer()
    text, _ = s.sanitize("{char} said hi to {user}")
    assert text == "{{char}} said hi to {{user}}"


def test_repairs_mismatched_brace_counts():
    s = MacroSanitizer()
    assert s.sanitize("{{char}")[0] == "{{char}}"
    assert s.sanitize("{char}}")[0] == "{{char}}"


def test_repair_is_case_insensitive_and_normalizes_lowercase():
    s = MacroSanitizer()
    assert s.sanitize("{CHAR}")[0] == "{{char}}"
    assert s.sanitize("{ char }")[0] == "{{char}}"


def test_folds_pronoun_macros_to_user():
    s = MacroSanitizer()
    for name in ("sub", "obj", "poss_p", "poss", "ref"):
        text, _ = s.sanitize(f"{{{{{name}}}}}")
        assert text == "{{user}}", f"{name} should fold to {{{{user}}}}"


def test_repair_then_fold_pipeline_order():
    # A broken-bracket pronoun macro must be repaired to {{obj}} first, then
    # folded to {{user}} -- repair-before-fold, not the other way around.
    s = MacroSanitizer()
    text, _ = s.sanitize("{obj} looked at {{poss}} reflection")
    assert text == "{{user}} looked at {{user}} reflection"


def test_poss_p_matches_before_poss_is_not_half_eaten():
    s = MacroSanitizer()
    text, _ = s.sanitize("that is {{poss_p}}")
    assert text == "that is {{user}}"


def test_corrects_user_typo_and_clears_the_warning():
    # The reported case: {{usee}} is a typo of {{user}} -- correct it silently
    # rather than surfacing an "unresolved macro" warning.
    s = MacroSanitizer()
    text, warnings = s.sanitize("Hello {{usee}}, how are you?")
    assert text == "Hello {{user}}, how are you?"
    assert warnings == []


def test_corrects_assorted_user_and_char_typos():
    s = MacroSanitizer()
    for typo in ("usee", "uesr", "usre", "userr", "usr"):
        assert s.sanitize(f"{{{{{typo}}}}}") == ("{{user}}", [])
    for typo in ("chr", "cahr", "chra", "charr", "chars", "chair"):
        assert s.sanitize(f"{{{{{typo}}}}}") == ("{{char}}", [])


def test_corrects_typo_with_dropped_brace():
    s = MacroSanitizer()
    assert s.sanitize("{usee} waved")[0] == "{{user}} waved"


def test_typo_correction_is_case_insensitive():
    s = MacroSanitizer()
    assert s.sanitize("{{USEE}}")[0] == "{{user}}"


def test_bare_word_braces_in_prose_are_untouched():
    s = MacroSanitizer()
    text, warnings = s.sanitize("some {characters} and {user_name} stay as-is")
    assert text == "some {characters} and {user_name} stay as-is"
    assert warnings == []


def test_unknown_macros_are_collected_as_warnings():
    s = MacroSanitizer()
    text, warnings = s.sanitize("{{time}} then {{weather}} then {{user}}")
    assert text == "{{time}} then {{weather}} then {{user}}"
    assert warnings == ["time", "weather"]


def test_original_macro_is_known_and_not_warned():
    # {{original}} is a real ST macro (post_history_instructions / system_prompt)
    # that Chub cards carry; it resolves in ST, so it isn't "unresolved".
    s = MacroSanitizer()
    text, warnings = s.sanitize("{{original}}\nNever speak as {{user}}.")
    assert text == "{{original}}\nNever speak as {{user}}."
    assert warnings == []


def test_unknown_macros_deduped_and_sorted():
    s = MacroSanitizer()
    _, warnings = s.sanitize("{{zeta}} {{alpha}} {{zeta}}")
    assert warnings == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# reverse_persona_names -- M7: reverse-substitute a captured persona name
# (e.g. "USER") back to {{user}}.
# ---------------------------------------------------------------------------


def test_reverse_persona_names_replaces_bare_name():
    assert reverse_persona_names("Ari looked at USER.", ["USER"]) == "Ari looked at {{user}}."


def test_reverse_persona_names_handles_possessive():
    assert reverse_persona_names("USER's trust matters.", ["USER"]) == "{{user}}'s trust matters."


def test_reverse_persona_names_is_word_boundary_safe():
    text = "USERNAME and SUPERUSER should stay untouched, but USER should not."
    result = reverse_persona_names(text, ["USER"])
    assert "USERNAME" in result
    assert "SUPERUSER" in result
    assert result.endswith("but {{user}} should not.")


def test_reverse_persona_names_leaves_char_macro_untouched():
    assert reverse_persona_names("{{char}} greeted USER", ["USER"]) == "{{char}} greeted {{user}}"


def test_reverse_persona_names_longest_name_first():
    text = "Ari and Ari Vale both appear"
    result = reverse_persona_names(text, ["Ari", "Ari Vale"])
    assert result == "{{user}} and {{user}} both appear"


def test_reverse_persona_names_empty_names_is_noop():
    text = "USER stays USER"
    assert reverse_persona_names(text, []) == text


def test_reverse_persona_names_empty_text_is_noop():
    assert reverse_persona_names("", ["USER"]) == ""


def test_macro_sanitizer_reverse_names_delegates():
    s = MacroSanitizer(user_names=["USER"])
    assert s.reverse_names("Hello USER") == "Hello {{user}}"
