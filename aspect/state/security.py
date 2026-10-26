"""The login gate's configuration, persisted at `data/security.json`.

WHY THIS EXISTS
The archive has no accounts and does not want any. What it sometimes wants is a
door: one username and one password in front of the whole server, off by
default, switched on from Settings -> Security. This module is that door's
state -- whether it is on, what opens it, and how a browser that already opened
it proves so on the next request.

WHY ITS OWN FILE
`data/settings.json` is an opaque blob the client owns: `GET /settings` hands
the whole thing to the browser and `PUT /settings` replaces the whole thing from
the browser's copy. A password hash stored there would be served to every page
load, and a stale tab saving an unrelated preference would write its old copy of
the credentials back over a changed password. The gate is also the one piece of
state the *server* decides on, per request, so unlike that blob it has a schema
and the server is what enforces it.

WHAT IS STORED
Never the password. A scrypt hash with a per-password salt, and a random
`session_secret` that signs session cookies. Sessions are stateless -- a cookie
is an expiry time plus an HMAC over it -- so there is no session table to
persist or sweep; replacing the secret is how every session is ended at once,
which happens whenever the credentials change or the gate is switched off.

And, optionally, one API token: a second way through the gate for the clients
that cannot log in -- the two userscripts, which post from another site's page.
That one *is* stored as issued, because Settings -> Userscripts bakes it into
the script it generates and a hash could not be baked into anything. It sits
beside `session_secret`, which is the same kind of thing: whoever can read this
file can already mint a session. It outlives a password change on purpose (a
new password should not mean reinstalling two userscripts) and ends when it is
regenerated or revoked.

LOCKED OUT
Delete `data/security.json` (or set `"enabled": false` in it). The gate is off
when the file is absent. A file that exists but cannot be parsed keeps the gate
*closed* rather than open -- a damaged file must not be a way in -- and the log
line says which file to remove.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from aspect.cards import edit
from aspect.config import settings

logger = logging.getLogger("aspect.state.security")

# scrypt cost. ~50 ms and 16 MB a check on this class of machine, which is paid
# once per login and once per distinct Basic header (see `check_basic`), not
# once per request.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

# How long a login lasts. Long, because this is a gate on a personal archive and
# not a bank: re-typing a password every day would only teach it to be short.
SESSION_SECONDS = 30 * 24 * 60 * 60

MAX_USERNAME = 128
# Marks an API token as this server's, for whoever finds one in a script later.
TOKEN_PREFIX = "jai_"
MAX_PASSWORD = 1024


class SecurityError(Exception):
    """The given gate settings are unusable."""


@dataclass(frozen=True)
class GateConfig:
    enabled: bool = False
    username: str = ""
    password_hash: str = ""
    session_secret: str = ""
    api_token: str = ""
    # The file exists but could not be read as a gate config. Closed, not open.
    damaged: bool = False

    @property
    def locked(self) -> bool:
        """Whether requests have to prove themselves."""
        return self.enabled or self.damaged


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _password_matches(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return hmac.compare_digest(actual, expected)


class SecurityStore:
    """Read/write access to the gate config, and the checks made against it.

    Reads are memoized against the file's mtime and size, the same trade
    `aspect.runtime.net` makes: the middleware asks on *every* request -- a page
    of thumbnails is hundreds of them -- and a stat is cheap where a read and a
    parse are not, while a hand-edit of the file is still picked up without a
    restart.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cached: tuple[tuple[Any, ...], GateConfig] | None = None
        # Basic headers already verified against the current config, so a
        # userscript's bulk run pays for scrypt once rather than per card.
        self._basic_ok: set[bytes] = set()

    # -- reading ---------------------------------------------------------------

    def _stamp(self) -> tuple[Any, ...]:
        try:
            stat = self.path.stat()
        except OSError:
            return ("absent",)
        return (stat.st_mtime_ns, stat.st_size)

    def config(self) -> GateConfig:
        stamp = self._stamp()
        if self._cached is not None and self._cached[0] == stamp:
            return self._cached[1]
        config = self._load()
        self._cached = (stamp, config)
        self._basic_ok.clear()
        return config

    def _load(self) -> GateConfig:
        if not self.path.is_file():
            return GateConfig()
        try:
            blob = json.loads(self.path.read_bytes())
            if not isinstance(blob, dict):
                raise ValueError(f"holds a {type(blob).__name__}, expected a JSON object")
            return GateConfig(
                enabled=bool(blob.get("enabled", False)),
                username=str(blob.get("username") or ""),
                password_hash=str(blob.get("password_hash") or ""),
                session_secret=str(blob.get("session_secret") or ""),
                api_token=str(blob.get("api_token") or ""),
            )
        except (OSError, ValueError) as exc:
            logger.error(
                "%s is unreadable (%s); the login gate is CLOSED until it is "
                "fixed or deleted -- deleting it switches the gate off",
                self.path,
                exc,
            )
            return GateConfig(damaged=True)

    # -- writing ---------------------------------------------------------------

    def update(self, *, enabled: bool, username: str, password: str | None) -> GateConfig:
        """Store new gate settings. `password=None` keeps the current one.

        Refuses to switch the gate on without both a username and a password:
        an enabled gate nothing can open is a lockout, and the only way back
        from it is the filesystem.
        """
        current = self.config()
        if current.damaged:
            current = GateConfig()
        username = username.strip()
        if len(username) > MAX_USERNAME:
            raise SecurityError(f"username is over {MAX_USERNAME} characters")
        if password is not None and len(password) > MAX_PASSWORD:
            raise SecurityError(f"password is over {MAX_PASSWORD} characters")

        password_hash = _hash_password(password) if password else current.password_hash
        if enabled and not (username and password_hash):
            raise SecurityError("set a username and a password before enabling login")

        # A new secret whenever what the gate checks has changed, so sessions
        # opened under the old credentials end with them.
        unchanged = (
            enabled == current.enabled
            and username == current.username
            and password_hash == current.password_hash
        )
        config = GateConfig(
            enabled=enabled,
            username=username,
            password_hash=password_hash,
            session_secret=current.session_secret
            if unchanged and current.session_secret
            else secrets.token_hex(32),
            api_token=current.api_token,
        )
        self._write(config)
        return config

    def set_api_token(self, *, revoke: bool = False) -> GateConfig:
        """Issue a fresh API token, replacing any current one -- or, with
        `revoke`, drop it. Nothing else about the gate changes, so browser
        sessions carry on."""
        current = self.config()
        if current.damaged:
            current = GateConfig()
        config = replace(current, api_token="" if revoke else TOKEN_PREFIX + secrets.token_urlsafe(32))
        self._write(config)
        return config

    def _write(self, config: GateConfig) -> None:
        payload = {
            "enabled": config.enabled,
            "username": config.username,
            "password_hash": config.password_hash,
            "session_secret": config.session_secret,
            "api_token": config.api_token,
        }
        edit.write_atomic(self.path, json.dumps(payload, indent=2).encode("utf-8"))
        # Dropped rather than replaced: an mtime is only trustworthy when it
        # changed, and here the file is known to have.
        self._cached = None
        self._basic_ok.clear()

    # -- checking --------------------------------------------------------------

    def check_credentials(self, username: str, password: str) -> bool:
        config = self.config()
        if not config.enabled or not config.password_hash:
            return False
        # Both halves are always evaluated, so a wrong username costs the same
        # scrypt as a wrong password and the two cannot be told apart by timing.
        name_ok = hmac.compare_digest(username.encode("utf-8"), config.username.encode("utf-8"))
        password_ok = _password_matches(password, config.password_hash)
        return name_ok and password_ok

    def check_token(self, header: str) -> bool:
        """An `Authorization: Bearer <api token>` header -- the userscripts' way
        in. No scrypt and no memo: the token is 256 random bits, not a password
        someone chose."""
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        config = self.config()
        if not config.enabled or not config.api_token:
            return False
        return hmac.compare_digest(token.strip().encode("utf-8"), config.api_token.encode("utf-8"))

    def check_basic(self, header: str) -> bool:
        """An `Authorization: Basic ...` header: the username and password
        themselves, for anything scripted that would rather not hold a token."""
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        self.config()  # refreshes, and clears the cache below if the file moved
        key = hashlib.sha256(header.encode("utf-8", "replace")).digest()
        if key in self._basic_ok:
            return True
        try:
            username, sep, password = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        if not sep or not self.check_credentials(username, password):
            return False
        self._basic_ok.add(key)
        return True

    def issue_session(self) -> str:
        """A session cookie value: `<expiry>.<hmac of expiry>`."""
        expires = str(int(time.time()) + SESSION_SECONDS)
        return f"{expires}.{self._sign(expires, self.config().session_secret)}"

    def check_session(self, token: str) -> bool:
        config = self.config()
        if not config.enabled or not config.session_secret:
            return False
        expires, _, signature = token.partition(".")
        if not hmac.compare_digest(signature, self._sign(expires, config.session_secret)):
            return False
        try:
            return int(expires) > time.time()
        except ValueError:
            return False

    @staticmethod
    def _sign(message: str, secret: str) -> str:
        return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


_store: SecurityStore | None = None


def store() -> SecurityStore:
    """The process-wide store, rebuilt if `settings.security_file` has been
    repointed -- which is what the test suite does. One instance rather than
    one per call, unlike the settings blob's, because the memo lives on it."""
    global _store
    if _store is None or _store.path != settings.security_file:
        _store = SecurityStore(settings.security_file)
    return _store
