"""Tests for aspect.text.name_repair.

The cases here are drawn from real archive names -- every literal name string
below is one an actual imported card carried.
"""

from aspect.text.name_repair import (
    GENERIC,
    JUNK,
    OK,
    TITLE,
    diagnose,
    name_score,
    roster,
    split_name,
    strip_parentheticals,
)


def card(name, description="", **kw):
    base = {
        "name": name,
        "description": description,
        "personality": "",
        "scenario": "",
        "first_mes": "",
        "alternate_greetings": [],
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# roster
# --------------------------------------------------------------------------


def test_roster_finds_the_dominant_character():
    body = (
        "Nareh 'Rae' Sarkisian is an elite hero academy student. Nareh is 5'5\" "
        "with an athletic build. Nareh's power is an inferno core. Nareh trains "
        "daily, and Nareh's rival Cassian watches her closely. Cassian is ranked "
        "first. Nareh resents Cassian."
    )
    top = roster(description=body)
    assert top[0].token == "nareh"
    assert "cassian" in {c.token for c in top}


def test_roster_rejects_words_also_used_lowercase():
    """A capitalized common noun is given away by its lowercase twin."""
    body = (
        "Fiona is the Fairy Court's lead tooth fairy. Fiona takes pride in her "
        "work as a fairy. The court trusts Fiona. Fiona's wings are bright. "
        "Every fairy in the court knows Fiona."
    )
    tokens = [c.token for c in roster(description=body)]
    assert tokens[0] == "fiona"
    assert "fairy" not in tokens
    assert "court" not in tokens


def test_roster_ignores_user_and_char_macros():
    body = "{{char}} greets {{user}}. {{user}} smiles at {{char}}. {{user}} leaves."
    assert [c.token for c in roster(description=body)] == []


def test_roster_joins_a_confident_surname():
    body = (
        "Damian Thorne is a porn director. Damian Thorne manipulates everyone. "
        "Damian Thorne's studio is famous. Damian Thorne smiles."
    )
    assert roster(description=body)[0].display == "Damian Thorne"


def test_roster_prefers_the_character_defined_first():
    body = (
        "Yasmin is the captive. " + ("Yasmin waits. " * 12)
        + "Later in the file: Cassian is her captor. " + ("Cassian watches. " * 12)
    )
    assert roster(description=body)[0].token == "yasmin"


# --------------------------------------------------------------------------
# name_score / segmentation
# --------------------------------------------------------------------------


def test_name_score_rejects_second_person_taglines():
    assert name_score("your desperate roommate") < 0
    assert name_score("Your Devoted House Maid") < 0
    assert name_score("Best Friend Gets Walked In On") < 0


def test_name_score_accepts_plain_names():
    assert name_score("Malinda") >= 1.0
    assert name_score("Ana Volkova") >= 1.0
    assert name_score("Aria Calysa") >= 1.0


def test_name_score_accepts_joined_cast_lists():
    """Joiners must not count toward the length penalty."""
    assert name_score("Aiko and Willow") >= 1.0
    assert name_score("Kara & Valeria & Elizabeth") >= 1.0


def test_name_score_allows_a_lone_role_noun_as_a_name():
    """People really are named Angel; only next to other words does it read as a role."""
    assert name_score("Angel") >= 1.0
    assert name_score("Jealous Wolfgirl") < 1.0


def test_name_score_keeps_quoted_epithets_intact():
    assert name_score('Cayla "Crimson Witch" Wise') >= 1.0


def test_split_name_handles_every_separator_flavour():
    assert split_name("At Your Mercy | Cynthia") == ["At Your Mercy", "Cynthia"]
    assert split_name("Airi - Between Hope and Regret") == ["Airi", "Between Hope and Regret"]
    assert split_name("Mia, your desperate roommate") == ["Mia", "your desperate roommate"]
    # A lost glyph leaves a bare double space that is really a separator.
    assert split_name("Naomi  your broke roommate") == ["Naomi", "your broke roommate"]


def test_strip_parentheticals_drops_taglines_keeps_aliases():
    assert strip_parentheticals("Malinda (Your Adopted Wolfgirl)") == "Malinda"
    assert strip_parentheticals("Maia (Blackmailed Assailant)") == "Maia"
    # A quoted nickname, a single-token alias and an acronym all survive.
    assert strip_parentheticals("Adeline (“Addie”)") == "Adeline (“Addie”)"
    assert strip_parentheticals("Hana (Aiko)") == "Hana (Aiko)"


# --------------------------------------------------------------------------
# diagnose -- junk suffixes
# --------------------------------------------------------------------------


def test_diagnose_strips_a_parenthetical_tagline():
    dg = diagnose(card("Malinda (Your Adopted Wolfgirl)", "Malinda is a wolfgirl. Malinda waits."))
    assert dg.verdict == JUNK
    assert dg.suggestion == "Malinda"


def test_diagnose_strips_a_comma_descriptor():
    dg = diagnose(card("Mia, your desperate roommate", "Mia is 19. Mia is shy. Mia's room is small."))
    assert dg.verdict == JUNK
    assert dg.suggestion == "Mia"


def test_diagnose_strips_a_run_on_descriptor():
    dg = diagnose(
        card(
            "Naomi  your broke roommate started an Onlyfans",
            "Naomi is broke. Naomi starts an OnlyFans. Naomi's rent is due. OnlyFans pays Naomi.",
        )
    )
    assert dg.suggestion == "Naomi"


def test_diagnose_picks_the_name_side_regardless_of_order():
    """JanitorAI creators write both `Name | Tagline` and `Tagline | Name`."""
    body = "Cynthia holds the leash. Cynthia smirks. Cynthia's grip tightens."
    assert diagnose(card("At Your Mercy | Cynthia", body)).suggestion == "Cynthia"
    assert diagnose(card("Cynthia | At Your Mercy", body)).suggestion == "Cynthia"


def test_diagnose_keeps_a_genuine_cast_list():
    body = "Ahri leads. Ahri fights. Senna shoots. Senna aims. Paul drives. Paul waits."
    dg = diagnose(card("Ahri  Senna  Paul", body))
    assert dg.suggestion == "Ahri, Senna, Paul"


def test_diagnose_never_emits_an_unbalanced_bracket():
    dg = diagnose(card("Maia (Blackmailed Assailant)", "Maia is 20. Maia is frail. Maia's eyes are blue."))
    assert dg.suggestion == "Maia"
    assert "(" not in dg.suggestion


# --------------------------------------------------------------------------
# diagnose -- generic names
# --------------------------------------------------------------------------


def test_diagnose_recovers_a_real_name_from_a_narrator_card():
    body = (
        "Nareh 'Rae' Sarkisian is an elite hero student. Nareh is 18. Nareh's power "
        "is fire. Nareh trains daily. Nareh smiles rarely. Nareh's sister worries."
    )
    dg = diagnose(card("Narrator", body))
    assert dg.verdict == GENERIC
    assert dg.suggestion == "Nareh"


def test_diagnose_leaves_a_genuine_narrator_card_alone():
    body = (
        "Bot type: GM/narrator. The narrator voices the ship, the crew, and the "
        "world -- not a single person. It plays all the background characters."
    )
    dg = diagnose(card("Narrator", body))
    assert dg.verdict == GENERIC
    assert dg.ensemble is True
    assert dg.suggestion is None


def test_diagnose_spots_a_world_card_that_declares_its_own_nature():
    """Real archive miss: this card was renamed to "Chief" before the rule existed."""
    body = (
        "# Character Info - Name: The White Stag Confederation (Narrator represents "
        "the living world, its people, and its inhabitants) - Role: The realm itself "
        "-- every clan chief, warrior, druid, maiden and merchant. Narrator is not a "
        "single person but the entire world of the White Stag Confederation, a tribal "
        "alliance of 15,000 souls. Chief Brennos leads. Cathal guards the ford."
    )
    dg = diagnose(card("Narrator", body))
    assert dg.ensemble is True


def test_diagnose_spots_a_multi_character_engine():
    body = (
        "CORE ENGINE: {{char}} is a multi-character narrative engine, not a single "
        "speaker. {{char}} portrays: Azerai Velnaris (The Matriarch), Sylthiel "
        "Velnaris (The Princess), and the Elven Court of nobles, guards and servants."
    )
    dg = diagnose(card("A Mother's Claim, A Daughter's Hunger", body))
    assert dg.ensemble is True


def test_ordinary_prose_about_the_world_is_not_an_ensemble():
    """"He represents the world of duty" is a person, not a world card."""
    body = (
        "Anastasia 'Ana' Volkova is heiress to the Volkov casino empire. Ana is 23. "
        "Ana's father is a burden of legacy -- he represents the world of duty she "
        "cannot fully escape. Ana works the floor nightly. Ana's temper is short."
    )
    dg = diagnose(card("Ana Volkova | Bianca", body))
    assert dg.ensemble is False


def test_ampersand_between_kept_names_becomes_a_comma():
    """Learned from the decision log: every hand-typed `&` override did this."""
    body = (
        "Katya leads the crew. Felicity drives. Sam picks locks. April watches the door. "
        "Katya's plan is simple. Felicity's car is fast. Sam's hands are steady."
    )
    dg = diagnose(card("Heist Night | Katya, Felicity, Sam & April", body))
    assert dg.suggestion == "Katya, Felicity, Sam, April"


def test_and_between_kept_names_becomes_a_comma():
    """`Bill, Bob and Jim` -> `Bill, Bob, Jim`, same as the `&` form."""
    body = (
        "Bill runs the shop. Bob does the books. Jim drives the van. Bill's temper "
        "is short. Bob's ledger is neat. Jim's van is older than all of them."
    )
    dg = diagnose(card("Hardware Store Trio | Bill, Bob and Jim", body))
    assert dg.suggestion == "Bill, Bob, Jim"


def test_and_followed_by_a_lowercase_word_is_left_alone():
    """`Girlfriend and the Ex` / `Kira and others` are prose, not a roster."""
    body = (
        "Kira is the one you knew first. Kira's friends orbit her. The others "
        "drift in and out of the house without knocking. Kira never locks up."
    )
    dg = diagnose(card("Messy Housemates | Kira and others", body))
    assert dg.suggestion is None or " and others" in dg.suggestion


def test_an_ampersand_alone_is_not_a_defect():
    """`Zoe & Lily` is a fine name -- punctuation tidying must not flag a card."""
    body = "Zoe is the loud one. Lily is quiet. Zoe's laugh carries. Lily's does not."
    dg = diagnose(card("Zoe & Lily", body))
    assert dg.verdict == OK
    assert dg.suggestion is None


def test_ampersand_already_preceded_by_a_comma_does_not_double_up():
    body = (
        "Aria sings. Lana writes. Erin plays bass. Aria's voice carries. "
        "Lana's lyrics bite. Erin's bass rumbles under it all."
    )
    dg = diagnose(card("The Band | Aria, Lana, & Erin", body))
    assert dg.suggestion == "Aria, Lana, Erin"


def test_diagnose_flags_a_scenario_title():
    body = "Yumi is your best friend. Yumi blushes. Yumi's hands shake. Yumi looks away."
    dg = diagnose(card("Best Friend Gets Walked In On", body))
    assert dg.verdict == TITLE
    assert dg.suggestion == "Yumi"


# --------------------------------------------------------------------------
# diagnose -- the definition's own `Name:` field
# --------------------------------------------------------------------------


def test_a_declared_name_rescues_an_unusual_one():
    """Real false positives: all three of these say so in their first line."""
    cases = (
        (
            "Two of Five",
            "Name: Two of Five\nSpecies: Human assimilated by the Borg\n"
            "Borg Designation: Two of Five, Unit 73. The Borg took her. The Borg "
            "rebuilt her. Elira remembers nothing. Elira's implants ache.",
        ),
        (
            "The Seduction Game",
            "Name: “The Seduction Game” TV show\nGenre: Game Show\n"
            "Julian Cavendish greets the couple. Julian collects data. "
            "Julian's smile never reaches his eyes. Julian escalates.",
        ),
        (
            "Lt. K’Lira vestai-Drexar",
            "Name: K’Lira vestai-Drexar\nSpecies: Half-Klingon\nRank: Lieutenant\n"
            "Drexar serves aboard the Kur'thak. Drexar's blade is honed. Drexar spars.",
        ),
    )
    for name, body in cases:
        assert diagnose(card(name, body)).verdict == OK, name


def test_a_declared_name_is_matched_case_insensitively():
    """`Kara swift` against `Name: Kara Swift` is a lowercase surname, not a title."""
    body = (
        "Name: Kara Swift\n- Age: 28\n- Eyes: Hazel\nKara Swift runs the gallery. "
        "Kara Swift's laugh is loud. Kara Swift never apologises."
    )
    assert diagnose(card("Kara swift", body)).verdict == OK


def test_a_declared_name_does_not_excuse_a_lowercase_first_word():
    """`kate` and `crybby.exe` both sit beside a field that declares them."""
    for name in ("kate", "crybby.exe"):
        body = f"Name: {name}\nShe waits by the door. Kate pours the tea. Kate's cup is chipped."
        assert diagnose(card(name, body)).verdict == TITLE, name


def test_a_character_field_holding_a_sentence_vouches_for_nothing():
    """The near-miss the contiguity test exists for: a bag of words would match."""
    body = (
        "Character: a shy girl who gets walked in on by her best friend\n"
        "Yumi blushes. Yumi's hands shake. Yumi looks away."
    )
    assert diagnose(card("Best Friend Gets Walked In On", body)).verdict == TITLE


# --------------------------------------------------------------------------
# diagnose -- ranks, particles and shouted capitals
# --------------------------------------------------------------------------


def test_a_leading_rank_is_part_of_the_name():
    body = (
        "Ilya guards the prince. Ilya is 21. Ilya's oath is absolute. Ilya kneels. "
        "Ilya speaks formally. Ilya never wavers."
    )
    assert diagnose(card("Knight Ilya", body)).verdict == OK


def test_a_role_word_that_is_not_leading_is_still_a_descriptor():
    """`Lewd Witch Liz` must keep resolving to `Liz`, rank list or no rank list."""
    body = "Liz hexes her exes. Liz's cauldron bubbles. Liz cackles. Liz brews."
    dg = diagnose(card("Lewd Witch Liz", body))
    assert dg.suggestion == "Liz"


def test_a_leading_role_word_before_a_common_noun_is_not_a_name():
    """`Catgirl Clan` is a scenario title; only a *name* may follow a rank."""
    body = "Sabre leads the raid. Sabre's ears twitch. Sabre hisses. Sabre wins."
    assert diagnose(card("Catgirl Clan", body)).verdict == TITLE


def test_a_lowercase_particle_carrying_a_capital_is_a_surname():
    """`vestai-Drexar` -- no ordinary lowercase word has a capital after a hyphen."""
    assert name_score("Lt. K’Lira vestai-Drexar") >= 1.0
    assert name_score("Nadia d’Artagnan") >= 1.0
    # The near-miss: a plainly lowercase word is still a defect.
    assert name_score("Kara swift") < 1.0


def test_a_shouted_name_is_proposed_the_way_the_body_writes_it():
    """A header shouting `AYAKO` once must not outvote 122 uses of `Ayako`."""
    body = "**AYAKO**\nAyako is a cosplayer. " + ("Ayako smiles. " * 8) + "Ayako's wig is pink."
    assert diagnose(card("Cosplay MILF", body)).suggestion == "Ayako"


def test_a_shouted_segment_is_recased_from_the_body():
    body = "Riley is your best friend. Riley's patience is gone. Riley confesses. Riley waits."
    dg = diagnose(card("RILEY: Your Best Friend Can't Pretend Anymore | Gamer Girl", body))
    assert dg.suggestion == "Riley"


def test_an_acronym_the_body_never_softens_keeps_shouting():
    body = (
        "The MSSS dispatches its staff nightly. MSSS operators are screened. "
        "MSSS clients pay in cash. The MSSS logo is discreet."
    )
    dg = diagnose(card("Milf Sex Satisfaction Service ~ MSSS", body))
    assert dg.suggestion == "MSSS"


def test_a_shouted_name_alone_is_not_a_defect():
    """Re-casing rides along with a repair; it must not create one (`ARIA`, `3M1LY`)."""
    for name in ("ARIA", "3M1LY", "AIko", "T3SS4"):
        body = f"{name} sings at the club. {name}'s voice carries. {name} bows."
        assert diagnose(card(name, body)).verdict == OK, name


# --------------------------------------------------------------------------
# diagnose -- two-hander names
# --------------------------------------------------------------------------


def test_a_grounded_lowercase_name_after_a_joiner_is_shifted_and_split():
    body = (
        "Himari is the older sister. Rikka is the younger. Himari's temper is short. "
        "Rikka's is shorter. Himari cooks. Rikka burns things."
    )
    assert diagnose(card("Himari and rikka", body)).suggestion == "Himari, Rikka"


def test_an_ungrounded_lowercase_word_after_a_joiner_is_left_alone():
    """`Kira and others` / `Girlfriend and the Ex` -- prose, not a second name."""
    body = (
        "Kira is the one you knew first. Kira's friends orbit her. The others drift "
        "in and out. Kira never locks up. Kira waits."
    )
    dg = diagnose(card("Kira and others", body))
    assert dg.suggestion == "Kira"


def test_vs_is_a_separator():
    body = "Dani is the underdog. Goliath is the champion. Dani's jab is quick. Goliath's reach is long."
    assert diagnose(card("Dani vs. Goliath", body)).suggestion == "Dani, Goliath"


def test_a_narration_voice_card_is_an_ensemble():
    body = (
        "You are simply a narration voice that sets the scene and describes the "
        "actions of the characters involved. Progress the story as they act."
    )
    assert diagnose(card("Last Man on Earth", body)).ensemble is True


def test_a_character_who_is_called_a_storyteller_is_not_an_ensemble():
    """Rejected on measurement: `storyteller` hits 13 archive cards, most of them people."""
    body = (
        "Hilda Henwood is the village storyteller. Hilda is 63. Hilda's tales are "
        "long. Hilda smokes a pipe. Hilda remembers everything."
    )
    assert diagnose(card("Hilda Henwood", body)).ensemble is False


# --------------------------------------------------------------------------
# diagnose -- the cards that must be left alone
# --------------------------------------------------------------------------


def test_diagnose_passes_a_clean_name():
    dg = diagnose(card("Malinda", "Malinda is a wolfgirl. Malinda waits. Malinda's tail wags."))
    assert dg.verdict == OK
    assert dg.needs_review is False


def test_diagnose_keeps_a_real_name_the_body_never_spells_out():
    """An all-{{char}} definition offers no evidence; that must not condemn the name."""
    body = "{{char}} is a bounty hunter. {{char}} carries a rifle. {{char}} distrusts {{user}}."
    for name in ("Aria Calysa", "Claire Rosewood"):
        assert diagnose(card(name, body)).verdict == OK, name


def test_diagnose_keeps_a_quoted_epithet_name():
    body = "Cayla hexes her foes. Cayla's cauldron bubbles. Cayla laughs."
    assert diagnose(card('Cayla "Crimson Witch" Wise', body)).verdict == OK
