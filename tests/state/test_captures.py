from pathlib import Path

from aspect.state.captures import CaptureStore, normalize

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_record_writes_numbered_file_per_capture(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)

    store.record("first prompt")
    store.record("second prompt")

    written = sorted(tmp_path.glob("system_prompt_*.txt"))
    assert len(written) == 2
    assert written[0].name.startswith("system_prompt_001_")
    assert written[1].name.startswith("system_prompt_002_")
    assert written[0].read_text() == "first prompt"
    assert written[1].read_text() == "second prompt"


def test_record_ignores_empty_prompt(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)

    store.record("")

    assert store.count == 0
    assert list(tmp_path.glob("*.txt")) == []


def test_count_reflects_number_of_records(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)
    assert store.count == 0

    store.record("a")
    store.record("b")

    assert store.count == 2


def test_captures_dir_is_created_if_missing(tmp_path):
    target = tmp_path / "nested" / "captures"
    assert not target.exists()

    CaptureStore(captures_dir=target)

    assert target.exists()


# ---------------------------------------------------------------------------
# M4: record() parses + upserts a CaptureRecord, get() retrieves it -- real
# hidden-card fixtures, not hand-written text (see tests/fixtures/README.md).
# ---------------------------------------------------------------------------


def test_record_parses_and_upserts_capture_record(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)

    store.record(_load("system_prompt_hidden_lyra.txt"))

    record = store.get("Lyra")
    assert record is not None
    assert record.name == "Lyra"
    assert "Full Name: Lyra Amarok" in record.personality
    assert record.scenario.startswith("Important settings for Roleplay")
    assert record.raw_system_prompt == _load("system_prompt_hidden_lyra.txt")


def test_get_is_case_and_whitespace_normalized(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)
    store.record(_load("system_prompt_hidden_aubrey_evans.txt"))

    assert store.get("  AUBREY EVANS  ") is not None
    assert store.get("aubrey evans") is not None
    assert store.get("Aubrey Evans") is not None


def test_get_returns_none_for_unknown_character(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)
    store.record(_load("system_prompt_hidden_ari.txt"))

    assert store.get("Someone Else") is None


def test_record_still_archives_raw_prompt_even_when_name_unparseable(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)

    store.record("no recognizable tags here at all")

    written = list(tmp_path.glob("system_prompt_*.txt"))
    assert len(written) == 1
    assert list(tmp_path.glob("*.json")) == []


def test_re_recording_same_character_overwrites_the_record(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)
    store.record(_load("system_prompt_hidden_lyra.txt"))
    store.record(_load("system_prompt_hidden_lyra_2.txt"))

    record = store.get("Lyra")
    assert record.raw_system_prompt == _load("system_prompt_hidden_lyra_2.txt")


def test_record_persists_one_json_file_per_character(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)

    store.record(_load("system_prompt_hidden_lyra.txt"))
    store.record(_load("system_prompt_hidden_ari.txt"))

    written = sorted(p.name for p in tmp_path.glob("*.json"))
    assert written == ["ari.json", "lyra.json"]


def test_records_reload_from_disk_on_restart(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)
    store.record(_load("system_prompt_hidden_aubrey_evans.txt"))

    reloaded = CaptureStore(captures_dir=tmp_path)
    record = reloaded.get("Aubrey Evans")

    assert record is not None
    assert record.name == "Aubrey Evans"
    assert "Her nickname is Ace" in record.personality
    # Reload doesn't replay the .txt archive -- count reflects records() calls
    # made against *this* instance only, not the persisted-record history.
    assert reloaded.count == 0


def test_normalize_lowercases_and_trims():
    assert normalize("  Aubrey Evans  ") == "aubrey evans"
    assert normalize("") == ""
    assert normalize(None) == ""


# ---------------------------------------------------------------------------
# Primary-greeting capture: the same chat relay that carries a hidden card's
# system prompt also carries its primary greeting (the first assistant
# message). record() takes both, so one chat message captures both halves.
# ---------------------------------------------------------------------------


def test_record_captures_primary_greeting_alongside_definition(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)

    store.record(_load("system_prompt_hidden_ari.txt"), primary_greeting="Hello there, USER")

    record = store.get("Ari")
    assert record.greetings == ["Hello there, USER"]
    assert "Location: USA" in record.personality


def test_record_without_greeting_leaves_greetings_empty(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)

    store.record(_load("system_prompt_hidden_ari.txt"))

    assert store.get("Ari").greetings == []


def test_record_strips_greeting_whitespace_and_treats_blank_as_none(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)

    store.record(_load("system_prompt_hidden_ari.txt"), primary_greeting="  Hi USER  ")
    assert store.get("Ari").greetings == ["Hi USER"]

    # A blank greeting on a later capture doesn't overwrite the good one.
    store.record(_load("system_prompt_hidden_ari.txt"), primary_greeting="   ")
    assert store.get("Ari").greetings == ["Hi USER"]


def test_re_record_with_new_greeting_replaces_prior(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)
    store.record(_load("system_prompt_hidden_ari.txt"), primary_greeting="First greeting")

    store.record(_load("system_prompt_hidden_ari.txt"), primary_greeting="Second greeting")

    assert store.get("Ari").greetings == ["Second greeting"]


def test_definition_only_recapture_preserves_prior_greeting(tmp_path):
    # A later definition-only capture (no assistant message in that request)
    # must not wipe a primary greeting captured earlier.
    store = CaptureStore(captures_dir=tmp_path)
    store.record(_load("system_prompt_hidden_ari.txt"), primary_greeting="Kept greeting")

    store.record(_load("system_prompt_hidden_ari.txt"))

    record = store.get("Ari")
    assert record.greetings == ["Kept greeting"]
    assert "Location: USA" in record.personality


def test_status_reflects_all_four_combinations(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)

    assert store.status("Nobody") == {"system": False, "greetings": False}

    # Definition captured, no greeting yet.
    store.record(_load("system_prompt_hidden_ari.txt"))
    assert store.status("Ari") == {"system": True, "greetings": False}

    # Greeting arrives on the next relay.
    store.record(_load("system_prompt_hidden_ari.txt"), primary_greeting="Hi")
    assert store.status("Ari") == {"system": True, "greetings": True}


def test_clear_removes_all_capture_files_and_in_memory_records(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)
    store.record(_load("system_prompt_hidden_ari.txt"), primary_greeting="Hi")

    assert any(tmp_path.iterdir())

    removed = store.clear()

    assert removed > 0
    assert list(tmp_path.iterdir()) == []
    assert store.get("Ari") is None
    assert store.count == 0
    assert store.status("Ari") == {"system": False, "greetings": False}


def test_clear_on_empty_store_removes_nothing(tmp_path):
    store = CaptureStore(captures_dir=tmp_path)
    assert store.clear() == 0
