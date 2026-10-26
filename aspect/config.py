from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# The repo root, so `.env` is found no matter which directory the server or a
# script was launched from -- pydantic-settings would otherwise resolve a
# relative env_file against the current working directory.
ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration, read from (in precedence order) the process
    environment, then `.env` at the repo root, then these defaults. Every key is
    the field name upper-cased behind `ASPECT_` -- see `.env.template`."""

    model_config = SettingsConfigDict(
        env_prefix="ASPECT_",
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The model id `/v1/models` advertises and a reply falls back to. There is
    # no model -- replies come from aspect/runtime/mock_responder.py -- but the sites
    # that call this endpoint want a name to put in their provider settings.
    mock_model: str = "jai-proxy-mock"
    host: str = "127.0.0.1"
    port: int = 8000

    # How cards are foldered under archive_dir:
    #   flat   -- <name>_<id8>.png, everything in one directory. Required by
    #             SillyTavern, which does not recurse into subfolders.
    #   nested -- <creator>/<name>_<id8>.png, the original layout. The creator
    #             is kept on the card either way (extensions.jai.creatorName),
    #             so nothing is lost by going flat; filenames carry the card-id
    #             fragment, so they don't collide across creators.
    card_layout: Literal["flat", "nested"] = "flat"

    # --- The archive ----------------------------------------------------------
    # The character archive this server browses, builds land in, and exports
    # from -- one directory, no second copy to reconcile. Defaults under the
    # repo's own `data/`, which is gitignored, so the layout a developer sees
    # is byte-for-byte the layout the container sees at its volume mount and no
    # code has to branch on environment. To relocate it, mount something else
    # at `data/` (or override ASPECT_ARCHIVE_DIR) -- there is no separate
    # "build elsewhere" knob. Absolute (via ROOT) rather than cwd-relative so
    # `uv run python -m aspect.server` works from any directory.
    archive_dir: Path = ROOT / "data" / "characters"
    galleries_dir: Path = ROOT / "data" / "galleries"
    # A character's expression sprites -- a sibling of galleries_dir, keyed on
    # the same gallery_id (docs/FORKS_AND_EXTRAS_PLAN.md §2). No card format
    # change: a character either has a folder here or does not.
    expressions_dir: Path = ROOT / "data" / "expressions"
    # Thumbnail caches, both inherited at cutover: `avatar/` from SillyTavern's
    # thumbnails/avatar (keyed by the exact card filename) and `gallery/` from
    # CharacterLibrary's cl_thumbs (keyed by <Name>_<gallery_id>). Pure cache --
    # deletable, regenerated on miss.
    thumbs_dir: Path = ROOT / "data" / "cache" / "thumbs"
    # Where deleted cards, gallery folders and gallery files go. Never scanned,
    # indexed or exported -- the same contract as `data/_quarantine/`. A delete
    # is the one archive operation with no undo and no second copy anywhere, so
    # it moves the file rather than unlinking it; emptying this directory is a
    # deliberate act, and the only one that actually destroys anything.
    trash_dir: Path = ROOT / "data" / ".trash"
    # The browser UI's own settings -- provider credentials, followed creators,
    # display preferences. Under `data/` with the cards rather than in `state/`
    # because it is user data, not a cache: it is the only copy of the Chub and
    # DataCat tokens, and it belongs in whatever gets mounted and backed up.
    # Seed it from an existing SillyTavern install with `make settings-import`.
    settings_file: Path = ROOT / "data" / "settings.json"
    # When Discover first saw each Following card, per provider -- what "Recent
    # only" filters on. User data like the settings blob, and beside it for the
    # same reason: the history is not recoverable from anything on disk, and
    # losing it makes every card in the next feed "new". Its own file rather
    # than a settings key because it is the one store whose whole job is to
    # grow, and settings.json is rewritten whole on every save with the provider
    # tokens riding along. See aspect/state/seen.py.
    seen_file: Path = ROOT / "data" / "discover_seen.json"
    # The login gate: on/off, the username, a password hash, and the secret
    # that signs session cookies. Off when the file is absent, so deleting it is
    # the way back in from a forgotten password. Its own file rather than a
    # settings key because the settings blob is served whole to the browser and
    # replaced whole by it. See aspect/state/security.py.
    security_file: Path = ROOT / "data" / "security.json"

    # Server-side working state, kept beside the archive rather than inside it:
    # `data/` is the one directory that has to be mounted (and backed up), and
    # nothing the server writes may land anywhere else -- in a container that
    # would mean writing into the image. ROOT-absolute like the archive paths.
    captures_dir: Path = ROOT / "data" / "state" / "captures"
    # Speed cache of raw lorebook payloads keyed by (source, lorebook id). A
    # lorebook is reused across many characters, and fetching one is the slow
    # part of an export, so we stash it here on first sight and skip the fetch
    # every later time. Purely a cache -- wipe/refresh via POST /clear-lorebooks.
    lorebook_cache_dir: Path = ROOT / "data" / "state" / "lorecache"
    # Cross-character dead-URL ledger for server-side media downloads (Phase
    # 3C). One file, not a directory -- its parent (data/state/) already
    # exists once captures_dir/lorebook_cache_dir are created below. Global
    # because the same dead catbox link shows up on dozens of cards; see
    # aspect/media/manifest.py.
    dead_urls_file: Path = ROOT / "data" / "state" / "dead_urls.json"

    # Draw the live terminal dashboard (aspect/runtime/dashboard.py) instead of a plain
    # scrolling log. Ignored -- and the plain log used -- when stdout is not a
    # TTY, so piping the server or running it under a supervisor still yields
    # ordinary line-by-line output.
    dashboard: bool = True

    request_timeout: float = 120.0
    allowed_origins: list[str] = ["*"]
    user_names: list[str] = ["USER"]

    # Lossily quantize card avatars with pngquant before embedding the card.
    # Fails soft: if the binary is missing or can't shrink the image, the
    # uncompressed PNG is written instead. Default binary is the one vendored
    # next to this package; override with ASPECT_PNGQUANT_BIN or PATH.
    compress: bool = True
    pngquant_bin: Path = Path(__file__).resolve().parent / "pngquant"

    # Fallback outbound proxy for everything this server fetches from the
    # internet. Not read directly by any fetcher -- aspect/runtime/net.py resolves
    # it, preferring `httpProxyUrl` from data/settings.json (the UI's copy, which
    # can be changed without a restart) and falling back to this. httpx's single
    # `proxy=` kwarg, so one URL covers http+https; None means connect directly.
    http_proxy: str | None = None

    # How many media items a download run fetches at once, and how many of
    # those may share one host. The per-host cap is the one that matters:
    # a card's gallery is usually all on a single image host (postimg, catbox),
    # and fetching a hundred images from it as fast as we can is how you get
    # rate-limited. Only the network half of an item is gated -- an item that
    # skips locally never waits behind a fetch. Set either to 1 for the old
    # strictly serial behaviour.
    media_concurrency: int = 6
    media_per_host_concurrency: int = 3

    # A datacat.run anonymous session token, so DatacatImageResolver reuses it
    # instead of hitting /api/liberator/identify on every run. Left unset the
    # first time; the resolver fetches one and writes it back here itself --
    # see aspect/datacat_client.py._persist_session_token.
    datacat_session_token: str | None = None


settings = Settings()

# The directories every consumer -- the server, the scripts, the test suite --
# assumes already exist, created here at import time.
#
# A container is the one place creating them can fail: `data/` is a bind mount,
# and the host directory may be owned by a uid the container does not run as.
# Raising here would surface as a bare PermissionError traceback out of an
# `import`, naming neither the mount nor the uid -- so failures are collected
# instead and reported by aspect.server's startup preflight, which can say which
# path, which uid, and who owns it.
STARTUP_DIR_ERRORS: list[tuple[Path, OSError]] = []

# Also the set the preflight checks for writability: existing is not enough,
# since mkdir(exist_ok=True) happily succeeds on a directory we cannot write to.
REQUIRED_DIRS: tuple[Path, ...] = (
    settings.archive_dir,
    settings.galleries_dir,
    settings.expressions_dir,
    settings.thumbs_dir,
    settings.captures_dir,
    settings.lorebook_cache_dir,
)

def ensure_dir(path: Path) -> Path:
    """mkdir -p that records the failure instead of raising it.

    Anything constructed at import time -- the stores in `aspect.deps` build
    their own directories in `__init__` -- must go through this rather than
    calling `mkdir` directly. A raise there happens during `import aspect.deps`,
    which is before `aspect.server.main()` can run the preflight, so the operator
    gets the very traceback the preflight exists to replace.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        STARTUP_DIR_ERRORS.append((path, exc))
    return path


for _required in REQUIRED_DIRS:
    ensure_dir(_required)
