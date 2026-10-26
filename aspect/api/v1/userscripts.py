"""`/api/v1/userscripts` -- hand the browser a ready-to-paste Tampermonkey bridge.

The bridges have exactly two things a user has to set: which server to post to,
and (JanitorAI only) the include/exclude tag filter the bulk sweep applies. A
third, the login gate's API token, is filled in from the server's own state. Both
used to be edited in a checkout and recompiled with `make compile`, which is
fine on the machine that holds the repo and useless everywhere else -- once the
archive is a container on a NAS, the person installing the userscript has no
sources, no Python, and no reason to have either.

So the server compiles it: same module concatenation as `make compile`
(`aspect/userscripts.py` is the single implementation), with those two constants
substituted, returned as text the settings UI shows in a copy/paste block.

Nothing is written to disk and nothing is stored here -- the chosen values live
in the UI's own settings document like every other preference.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from aspect import userscripts
from aspect.api.schemas import UserscriptOut, UserscriptRequest, UserscriptSpecOut
from aspect.state import security

router = APIRouter()


@router.get(
    "/userscripts",
    response_model=list[UserscriptSpecOut],
    summary="The bridges this server can generate",
)
def list_userscripts() -> list[UserscriptSpecOut]:
    return [
        UserscriptSpecOut(
            key=spec.key,
            label=spec.label,
            site=spec.site,
            filename=spec.filename,
            description=spec.description,
            supports_tag_filter=spec.supports_tag_filter,
        )
        for spec in userscripts.SPECS.values()
    ]


@router.post(
    "/userscripts/{key}",
    response_model=UserscriptOut,
    summary="Compile one bridge with the given server URL and tag filter",
)
def generate_userscript(key: str, body: UserscriptRequest) -> UserscriptOut:
    spec = userscripts.SPECS.get(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown userscript {key!r}")
    # The login gate's API token, if one has been generated: baked in so the
    # installed bridge passes the gate with nothing to set by hand.
    api_token = security.store().config().api_token or None
    try:
        source = userscripts.compile_userscript(
            spec,
            server_url=body.server_url,
            # Always passed for a bridge that has a filter, so "cleared both
            # lists" is honoured rather than falling through to the source
            # defaults. compile_userscript ignores them for saucepan.
            include_tags=body.include_tags,
            exclude_tags=body.exclude_tags,
            api_token=api_token,
        )
    except userscripts.UserscriptError as exc:
        # A bad server URL is the user's typo (400); a missing module or a
        # renamed constant is ours (500). Both arrive as the same exception, and
        # the difference is worth keeping visible in the status code.
        status = 400 if "server URL" in str(exc) else 500
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return UserscriptOut(
        key=spec.key,
        filename=spec.filename,
        source=source,
        bytes=len(source.encode("utf-8")),
        includes_token=api_token is not None,
    )
