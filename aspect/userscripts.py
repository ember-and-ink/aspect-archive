"""Compile the Tampermonkey bridges from their source modules, with the two
constants a user actually has to change baked in.

The bridges are authored as small fragments under `userscript/src_jai/` and
`userscript/src_saucepan/` and concatenated, in an explicit order, inside a
single IIFE. That concatenation used to live in two near-identical scripts under
`scripts/`; it lives here so the *server* can do it too -- the settings UI asks
for a configured script and gets one back, which is the only practical install
path when the archive is a container on another machine and the user is nowhere
near a checkout.

Two constants are substituted, both in `config.js`:

  * `DEFAULT_SERVER` -- where the bridge posts. Tampermonkey storage still wins
    at runtime (`GM_setValue("serverUrl", ...)`), so this only sets the fallback,
    which for a generated script is exactly the value the user was told to use.
  * `BULK_TAG_FILTER` -- the include/exclude tag filter the bulk sweep and the
    "hide saved" toggle apply. Only the JanitorAI bridge has one.

A third, `DEFAULT_TOKEN`, is not the user's to type: it is the login gate's API
token (Settings -> Security), which the server fills in when it has one.

Substitution is anchored on the declarations themselves and raises if an anchor
is missing, so renaming a constant in the source breaks the build loudly instead
of silently emitting an unconfigured script.

With no overrides the output is byte-identical to the source concatenation --
that is what `make compile` writes into the repo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from aspect.config import ROOT

USERSCRIPT_DIR = ROOT / "userscript"

# The Tampermonkey `==UserScript==` banner. It sits ABOVE the IIFE; every other
# module is authored already indented two spaces (as it appears inside the IIFE)
# and is emitted verbatim, so template literals (e.g. the overlay CSS) are never
# re-indented.
BANNER = "00-banner.js"


@dataclass(frozen=True)
class ScriptSpec:
    """One bridge: where its modules are, in what order they concatenate, and
    which of the two knobs apply to it."""

    key: str
    label: str
    site: str
    filename: str
    src_dir: Path
    modules: list[str]
    description: str
    supports_tag_filter: bool = False
    # Set by the caller when a generated script's config differs from source.
    default_server: str = field(default="http://127.0.0.1:8000", compare=False)


SPECS: dict[str, ScriptSpec] = {
    "jai": ScriptSpec(
        key="jai",
        label="JanitorAI bridge",
        site="janitorai.com",
        filename="jai-proxy-bridge.user.js",
        src_dir=USERSCRIPT_DIR / "src_jai",
        modules=[
            "config.js",
            "client-server.js",
            "client-janitor.js",
            "overlay.js",
            "scheduler.js",
            "export.js",
            "bulk.js",
            "hide-captured.js",
            "bootstrap.js",
        ],
        description=(
            "Exports a JanitorAI character as a V3 card, captures hidden definitions through "
            "the chat relay, and adds the bulk sweep and the 'hide saved' toggle."
        ),
        supports_tag_filter=True,
    ),
    "saucepan": ScriptSpec(
        key="saucepan",
        label="Saucepan bridge",
        site="saucepan.ai",
        filename="saucepan-proxy-bridge.user.js",
        src_dir=USERSCRIPT_DIR / "src_saucepan",
        modules=[
            "config.js",
            "client-server.js",
            "client-saucepan.js",
            "export.js",
            "overlay.js",
            "scheduler.js",
            "bootstrap.js",
        ],
        description="Exports a Saucepan companion as a V3 card. No bulk sweep, so no tag filter.",
    ),
}


class UserscriptError(Exception):
    """A script could not be compiled: a missing module or a missing anchor."""


# `const DEFAULT_SERVER = "...";` -- the string is the only capture that moves.
_SERVER_RE = re.compile(r'(?P<head>const DEFAULT_SERVER = )"[^"]*";')

_TOKEN_RE = re.compile(r'(?P<head>const DEFAULT_TOKEN = )"[^"]*";')

# The whole `const BULK_TAG_FILTER = { ... };` block, closed by a `};` at the
# declaration's own indentation. Matching on that rather than counting braces
# keeps this readable and still safe: nothing else in the block is dedented that
# far.
_TAG_FILTER_RE = re.compile(
    r"^(?P<indent>[ \t]*)const BULK_TAG_FILTER = \{.*?^(?P=indent)\};",
    re.DOTALL | re.MULTILINE,
)


def normalize_server_url(url: str) -> str:
    """The server URL as the bridge wants it: `scheme://host[:port]`, no trailing
    slash (every call is `SERVER + "/some/path"`).

    Rejects anything that is not an http(s) URL. That matters more than it looks:
    the value is interpolated into JavaScript source, and a quote or a newline in
    it would end the string literal and produce a script that fails to parse --
    or worse, one that runs something else on janitorai.com.
    """
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        raise UserscriptError("server URL is empty")
    if not re.fullmatch(r"https?://[^\s\"'`\\<>]+", cleaned):
        raise UserscriptError(f"not a usable http(s) server URL: {url!r}")
    return cleaned


def normalize_tags(tags: list[str] | None) -> list[str]:
    """Trimmed, de-duplicated, order-preserving. Empty entries drop out, so a
    trailing comma in the UI's comma-separated field is harmless."""
    seen: dict[str, None] = {}
    for tag in tags or []:
        cleaned = str(tag).strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def _read(spec: ScriptSpec, name: str) -> str:
    path = spec.src_dir / name
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except OSError as exc:
        raise UserscriptError(f"missing source module {path}: {exc}") from exc


def _apply_server(source: str, server_url: str) -> str:
    replacement = rf"\g<head>{json.dumps(server_url)};"
    patched, count = _SERVER_RE.subn(replacement, source)
    if count != 1:
        raise UserscriptError(
            f"expected exactly one `const DEFAULT_SERVER = \"...\";` in config.js, found {count}"
        )
    return patched


def _apply_token(source: str, api_token: str) -> str:
    # Interpolated into JavaScript, and read from a file that can be hand-edited.
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", api_token):
        raise UserscriptError("the API token holds characters a token never has; regenerate it")
    patched, count = _TOKEN_RE.subn(rf"\g<head>{json.dumps(api_token)};", source)
    if count != 1:
        raise UserscriptError(f"expected exactly one `const DEFAULT_TOKEN = \"...\";` in config.js, found {count}")
    return patched


def _apply_tag_filter(source: str, include: list[str], exclude: list[str]) -> str:
    def render(match: re.Match[str]) -> str:
        indent = match.group("indent")
        inner = indent + "  "
        return (
            f"{indent}const BULK_TAG_FILTER = {{\n"
            f"{inner}include: {json.dumps(include)},\n"
            f"{inner}exclude: {json.dumps(exclude)},\n"
            f"{indent}}};"
        )

    patched, count = _TAG_FILTER_RE.subn(render, source)
    if count != 1:
        raise UserscriptError(
            f"expected exactly one `const BULK_TAG_FILTER = {{...}};` in config.js, found {count}"
        )
    return patched


def _config_note(
    spec: ScriptSpec, server_url: str, include: list[str], exclude: list[str], has_token: bool
) -> str:
    """A short comment recording what was baked in, so a script pasted into
    Tampermonkey months ago can still be read back."""
    lines = [
        "//",
        "// GENERATED by the archive server (Settings -> Userscripts). The values below",
        "// were substituted into config.js; everything else is the repo source.",
        f"//   server:  {server_url}",
    ]
    if spec.supports_tag_filter:
        lines.append(f"//   include: {', '.join(include) if include else '(all cards)'}")
        lines.append(f"//   exclude: {', '.join(exclude) if exclude else '(nothing)'}")
    if has_token:
        lines.append("//   login:   API token included -- treat this file like a password")
    return "\n".join(lines)


def compile_userscript(
    spec: ScriptSpec | str,
    *,
    server_url: str | None = None,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    api_token: str | None = None,
) -> str:
    """The full `.user.js` for one bridge.

    With every override left as None the result is the plain source
    concatenation -- what `make compile` writes back into the repo. Pass any of
    them and the corresponding constant in `config.js` is replaced and a short
    `GENERATED` note is added under the banner. `api_token` is the login gate's
    token (Settings -> Security), sent as a Bearer header on every request.

    `include_tags`/`exclude_tags` are ignored for a bridge with no bulk sweep
    (saucepan); passing them is not an error, since the UI keeps one saved filter
    for both.
    """
    if isinstance(spec, str):
        try:
            spec = SPECS[spec]
        except KeyError:
            raise UserscriptError(f"unknown userscript {spec!r}") from None

    configured = (
        server_url is not None
        or api_token is not None
        or (spec.supports_tag_filter and (include_tags is not None or exclude_tags is not None))
    )
    resolved_server = normalize_server_url(server_url) if server_url is not None else spec.default_server
    include = normalize_tags(include_tags)
    exclude = normalize_tags(exclude_tags)

    banner = _read(spec, BANNER)
    if configured:
        banner = f"{banner}\n{_config_note(spec, resolved_server, include, exclude, api_token is not None)}"

    parts: list[str] = []
    for name in spec.modules:
        module = _read(spec, name)
        if name == "config.js":
            if server_url is not None:
                module = _apply_server(module, resolved_server)
            if api_token is not None:
                module = _apply_token(module, api_token)
            if spec.supports_tag_filter and (include_tags is not None or exclude_tags is not None):
                module = _apply_tag_filter(module, include, exclude)
        parts.append(module)

    body = "\n\n".join(parts)
    return f'{banner}\n\n(function () {{\n  "use strict";\n\n{body}\n}})();\n'


def write_userscript(spec: ScriptSpec | str) -> Path:
    """Compile the unconfigured script and write it next to its sources. What
    `make compile` does; nothing at runtime calls this."""
    if isinstance(spec, str):
        spec = SPECS[spec]
    out = USERSCRIPT_DIR / spec.filename
    out.write_text(compile_userscript(spec), encoding="utf-8")
    return out
