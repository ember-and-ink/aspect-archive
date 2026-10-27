from pathlib import Path

import aspect
from aspect.config import ROOT, Settings
from aspect.cards.models import CharacterCardV3


def test_package_imports():
    assert aspect is not None


def test_settings_defaults():
    # Built without the repo's .env, so this asserts the shipped defaults rather
    # than whatever this machine happens to be configured to.
    defaults = Settings(_env_file=None)
    assert defaults.mock_model == "jai-proxy-mock"
    assert defaults.port == 8000
    assert defaults.card_layout == "flat"
    # Every writable path lives under ROOT/data/ -- the single directory a
    # container mounts -- so nothing the server writes can land inside an image.
    assert defaults.captures_dir == ROOT / "data" / "state" / "captures"
    assert defaults.lorebook_cache_dir == ROOT / "data" / "state" / "lorecache"


def test_settings_read_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "ASPECT_ARCHIVE_DIR=/tmp/characters\nASPECT_CARD_LAYOUT=nested\n",
        encoding="utf-8",
    )
    configured = Settings(_env_file=env)
    assert configured.archive_dir == Path("/tmp/characters")
    assert configured.card_layout == "nested"


def test_character_card_v3_to_dict_shape():
    card = CharacterCardV3(name="Ada")
    d = card.to_dict()
    assert d["spec"] == "chara_card_v3"
    assert d["spec_version"] == "3.0"
    assert d["data"]["name"] == "Ada"
    assert d["name"] == "Ada"
