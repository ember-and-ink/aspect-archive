# jai-proxy

Archives JanitorAI character cards as SillyTavern-compatible **Character Card V3**
PNGs, for personal use. It captures both **public** cards and **creator-hidden**
definitions, and saves each as a self-contained PNG (definition + all greetings +
lorebook + avatar) straight into the archive it also browses and serves —
`data/characters/` (see [Configuration](#configuration-env)).

## How it works

A **thin userscript bridge** does only what must happen inside JanitorAI's
authenticated page — relaying the chat request, and reading a character straight
from JanitorAI's own JSON API (`GET /hampter/characters/<id>` for the card,
`/hampter/script/<id>` for public lorebooks), both bearer-authenticated in-page.
A local **FastAPI server** does everything else: maps the JSON onto Character
Card V3, repairs macros, converts the creator-notes blurb HTML→markdown,
assembles the V3 JSON, fetches the avatar, and embeds it into the PNG `tEXt`
chunks (via Pillow). Because the mapping lives in Python, a JanitorAI schema
change is fixed by editing the server and restarting — no userscript reinstall.

Hidden definitions are never served by the API; they only exist in the chat
**system prompt** sent to the model, and the card's primary greeting only in the
chat's first message. The server captures both from the chat request it answers
itself, so nothing leaves your machine, and merges them with the JSON (alternate
greetings, tags, creator notes, avatar) at export.

## Running it

```bash
make docker-build     # docker compose build
make docker-up        # docker compose up -d  -> http://127.0.0.1:8000
```

One image, one mount (`./data`), one port. The image carries the **server only**:
everything writable already defaults under `data/` — the archive, galleries,
thumbnail cache, `settings.json` and the server's working state — so that single
bind mount is the whole of the container's state, and `JAI_PROXY_HOST=0.0.0.0`
is the only variable it needs. Nothing is architecture-specific, so it builds
natively on arm64 and amd64 alike (the *published* image is amd64 only — see
below — because the deployment target is a NAS and development runs from
source).

⚠️ **SillyTavern's stock port is also 8000**, so `docker compose up` fails to
bind while it is running — stop it, or publish the archive on another host port.
The container itself always listens on 8000; the userscripts read the server's
address from Tampermonkey storage (`serverUrl`), so the host port is free to
move as long as you set the same one there.

### Remote (unraid)

Pushing to `main` publishes `ghcr.io/ember-and-ink/jai-proxy` — `linux/amd64`,
`:latest` plus `:sha-<short>` and the release version, e.g. `:0.1.4` — and cuts
a matching GitHub release (`.github/workflows/publish.yml`). The patch number is
assigned automatically; `make bump-minor` / `make bump-major` start a new
series. A server pulls that image rather than building:

```bash
make docker-pull      # docker compose -f compose.prod.yaml pull
make docker-up-prod
```

`compose.prod.yaml` and `unraid-template.xml` are the two ways to run it there;
both default to `/mnt/user/appdata/jai-proxy` for the mount and uid `99:100`
(unraid's `nobody:users`) for the process. Acquisition keeps working against a
remote archive with no TLS — the userscripts reach it through
`GM_xmlhttpRequest`, which is exempt from the page's mixed-content rules. The
image carries the userscript *sources* so a remote server can generate a
configured bridge for you (Settings → Userscripts); nothing else about them is
in there.

**[docs/DEPLOY.md](docs/DEPLOY.md)** is the runbook: publishing, making the GHCR
package public, seeding the mount, and repointing the userscripts. Note that the
archive has **no authentication** by default — keep it on the LAN. Settings →
Security can put a single username and password in front of the whole server
(generate an API token there too, and the userscripts built by Settings →
Userscripts carry it); it is a gate for a shared LAN, sent over plain http, not a reason to expose the
server. Locked out: delete `data/security.json`.

The maintenance scripts (`make import` / `check` / `names` / `thumbs` /
`gallery-ids`) stay on the host and run against the same `./data`. They are not
in the image on purpose: they are leftovers from stitching several repos
together, and the intended flow is *retrieve → cleaned once on the way in →
archived*, with a clean card exported into SillyTavern when you actually want to
play it.

For development, run it directly instead:

```bash
uv sync
uv run python -m proxy.server        # serves http://127.0.0.1:8000
```

In a terminal the server draws a live dashboard (`proxy/runtime/dashboard.py`): a header
band with the address, the server log on the left, and every character exported
this run -- source, name, creator -- on the right. A card that was **already in
the cards folder** (see [Re-exporting](#re-exporting)) is drawn in yellow and
marked `dup`, and counted apart in the panel title (`downloads · 3 new · 1 dup`),
so a wasted click is obvious at a glance. Piping the output, or setting
`JAI_PROXY_DASHBOARD=false`, falls back to plain line-by-line logging -- which
reports the same thing, naming the file either way (`saved janitor card: Emma by
Theodrax (Emma_123456.png)` / `already have janitor card: …`).

### Configuration (`.env`)

Defaults live in `proxy/config.py`; override them per-machine in a git-ignored
`.env` at the repo root, which the server, every script under `scripts/`, and the
test suite all read:

```bash
cp .env.template .env
```

The two keys that matter most:

| key | default | what it does |
| --- | --- | --- |
| `JAI_PROXY_ARCHIVE_DIR` | `./data/characters` | the archive: what the browse API reads and what a build writes into |
| `JAI_PROXY_CARD_LAYOUT` | `flat` | `flat` = `<name>_<id8>.png`; `nested` = `<creator>/<name>_<id8>.png` |

A build writes into `ARCHIVE_DIR` directly, so a retrieved card is in the
archive the moment it's written — one directory, no sync step, no second copy
to reconcile. `flat` is what keeps that workable: names stay unique across
creators via the `_<id8>` fragment, and nothing is lost, since the creator is
on the card (`extensions.jai.creatorName`). To relocate the archive (e.g. in a
container), mount something else at `data/characters` rather than pointing
builds at a separate folder — there is no "build elsewhere" knob anymore.

Working state (`data/state/captures`, `data/state/lorecache`) sits beside the
archive rather than inside it — under the one directory that has to be mounted
and backed up, so nothing the server writes can land anywhere else.

Install `userscript/jai-proxy-bridge.user.js` in Tampermonkey. In JanitorAI's
proxy/config settings, set the endpoint to
`http://127.0.0.1:8000/v1/chat/completions` (any model name — the server answers
whatever it is called). Point it at whatever host the bridge itself is
configured for: the URL has to share the bridge's `serverUrl` host and port, or
the `/models` probe stops being recognised.

That request is never actually sent over the network by the page. The bridge
injects a hook into the page that intercepts it and relays it through
`GM_xmlhttpRequest`, which is exempt from the page's mixed-content and CSP
rules — which is why an https JanitorAI can talk to an http server on the LAN
with no certificate anywhere. (A browser exempts `127.0.0.1` from
mixed-content blocking, so a *local* server used to work even with the hook
doing nothing; a LAN address does not, which is how that was finally noticed.)

**Without a checkout**, open the archive in a browser and go to **Settings →
Userscripts**: pick a bridge, set the server URL (it offers the address you are
already viewing the archive on) and the bulk tag filter, and hit Generate. The
server compiles the same bundle with those two constants substituted and hands
it back as copyable text — paste it into a new Tampermonkey script and save.
That is the intended install path for a containerized server, where the machine
running the browser has no repo, no Python and no `make`. The chosen values are
remembered with the rest of your settings.

Nothing is generated by a model. The chat endpoint exists only because sending a
message is what makes JanitorAI put the hidden definition in the system prompt;
the reply is assembled locally from canned roleplay fragments
(`proxy/runtime/mock_responder.py`) — an action beat, a line of dialogue, and one word
lifted from what you just said. It is meant to read as a small, distracted model
so a chat doesn't visibly break mid-capture, and nothing else.

The userscript is **compiled** from small modules under `userscript/src_jai/`.
After editing any of them, rebuild the single-file bundle with:

```bash
make compile        # -> userscript/jai-proxy-bridge.user.js
                    #    + userscript/saucepan-proxy-bridge.user.js
```

The concatenation itself lives in `proxy/userscripts.py`, not in the two scripts
under `scripts/` — the server runs the same code for Settings → Userscripts, with
`DEFAULT_SERVER` and `BULK_TAG_FILTER` substituted. With nothing substituted the
output is byte-identical to what `make compile` writes, and a test asserts that
against the checked-in bundles.

(A twin **saucepan** bridge is compiled the same way from
`userscript/src_saucepan/` — same server, same purple export button and
connection pill. It detects hidden companions too (`🟢 saucepan · hidden ✗`,
read from `open_definition`), but can't capture them — a hidden saucepan
definition isn't exposed by the API — so those export only their public blurb,
with a server warning.)

## Usage

A pill in the bottom-right corner shows 🟢/🔴 for server reachability and, on a
card page, whether the card in view is exportable right now — **without** naming
the character (the server pulls that cleanly from the API at build time). Open
cards show `🟢 jai-proxy · ready ✓`; hidden cards show `· hidden ✓` once captured
(`· hidden ✗` before). Off a card the pill is just `🟢 jai-proxy`. It carries a
small **CLEAR** link on its right edge.

Just above the pill is the purple **⬇ Export to card** button; it dims slightly
when the card in view isn't exportable yet. Export progress (`Fetching…`,
`✓ Saved`) appears in a status line just above the button.

### Public cards

Open the character's page and click **⬇ Export to card**. The card is read
straight from JanitorAI's JSON API — no chat needed, and no name prompt (the
server saves it under the real `chat_name`; JanitorAI's card title is often a
scenario blurb rather than the character name). The status line reports
`✓ Saved` on a clean export. Alternate greetings and public lorebooks are
fetched automatically during export; there's no separate step.

### Hidden-definition cards

A hidden card's JSON omits the definition and its primary greeting — both only
exist in the chat. So there's **one** extra step:

1. **Open a chat** with the character and **send any message** (e.g. `hello`).
   As the server relays that request it captures the hidden definition from the
   system prompt *and* the primary greeting from the chat's first message in one
   shot (pill shows `· hidden ✓`).
2. Click **⬇ Export to card** (on the character page, or from the chat).

If you click Export to card on a hidden card before it's been captured, the build
**hard-fails** with a message telling you to send a chat message first — it never
writes a broken card. Everything else a hidden card needs (name, tags, creator
notes, alternate greetings, avatar, lorebooks) still comes from the JSON.

### Re-exporting

A card whose id is already in the cards folder is **never rewritten**. Every
build path stops on it: the export button reports `• Already saved`, the bulk
sweep marks the row `• already saved — skipped`, and `make import` skips it too.
The match is the `_<id8>` filename fragment, so it holds however the file was
renamed since (as long as the fragment survives — SillyTavern's **Rename** drops
it, and such a card is acquirable again).

That makes the archive, and any edit you made to a card inside SillyTavern, safe
from an accidental second click. To genuinely re-acquire a character — a creator
updated the definition, or you first saved it before capturing its hidden
definition or lorebooks — **delete the card file and export it again**.

### Clearing the cache

Hidden cards are matched by character name against accumulated captures, so name
collisions get likelier as captures pile up. The pill's **CLEAR** link wipes all
captured system prompts and greetings (`data/state/captures/`) and also resets the
plugin's own remembered state (last card id / hidden flag). Finished card PNGs
are not affected.

### Reading the result

The status line above the button reports the outcome: `✓ Saved`,
`• Already saved` if you had the card already (nothing was written — see
[Re-exporting](#re-exporting)), or
`⚠️ Saved — N warning(s)` if something was degraded (e.g. an unresolved macro or
a missing field — hover the button for the full list), or `⚠️ <message>` if the
build request itself failed (the hidden-card gate reports its reason here;
otherwise check the server logs).

### Browsing (hiding what you already have)

Any page that renders character tiles — the logged-in homepage, search, tag
browse, a creator's profile — gets a **🙈 Hide saved** toggle above the export
button. Turning it on folds away every tile that is either already saved to your
cards folder or skipped by the include/exclude tag filter in
`userscript/src_jai/config.js`, so what's left is the stuff you don't have and
would actually want. The button then reads `👁 Show hidden (N)`; clicking it
again puts everything back.

Both verdicts come from the tile itself — the character id from its link, the
tags from its own chips — so no extra listing requests are made and the toggle
follows whatever segment/sort you've picked. "Already saved" is the same
`_<id8>` filename check the bulk sweep uses, so it stays accurate no matter what
you renamed the card to. Hiding is purely a CSS class on the tile; nothing on
JanitorAI's side is touched.

### Importing cards (datacat & Chub.ai)

Some sources hand you a PNG that already embeds a `chara`/`ccv3` character card.
Dropping a bucket of those into `./import` and re-homing them is faster than the
send-a-chat-then-capture flow — no server needed:

```bash
# drop card PNGs into ./import, then:
make import
```

Each card lands in the configured cards folder under the same `<name>_<id>.png`
naming the native retriever produces (so it shows as **acquired** on the next
scan, which keys off the `_<id>` filename fragment), running the shared avatar
normalize + pngquant compression. Two source formats are auto-detected:

- **datacat** (a closed-source JanitorAI retriever) — a bare definition with
  **no lorebook**. It's rebuilt through the card builder (macro sanitize,
  creator-notes HTML→markdown) with a `datacat_import` provenance block. Because
  it lacks the `character_book`, a card whose creator uses one is still better
  grabbed via the send-a-chat capture flow.
- **Chub.ai** (recognised by `extensions.chub`) — an already-complete
  `chara_card_v3` with its **own lorebook and rich extensions**. It's passed
  through near-verbatim: macros are sanitized and `creator_notes` is converted
  from Chub's styled HTML (its `<style>`/CSS shell is stripped, not leaked) to
  markdown, but the `extensions` block (including the `depth_prompt` injection)
  and the whole `character_book` — down to entry fields like `probability` /
  `selectiveLogic` and Chub's int-or-string `position` — are preserved exactly.

A card whose id is **already** in the cards folder is skipped, never overwritten,
so a fuller retrieval is never downgraded to an import. To replace one, delete
the existing file and re-run.

### Gallery ids (SillyTavern-CharacterLibrary)

CharacterLibrary gives each character its own image-gallery folder, keyed on the
character name **plus** an `extensions.gallery_id` — a random 12-character
alphanumeric string it mints for characters that don't have one. Without an id
the folder is keyed on the name alone, so two characters called "Aurora" share
(and overwrite) one gallery.

Every card written here carries one: `PngWriter` stamps it at write time, using
the same alphabet and length CharacterLibrary does, so our cards drop in and work
unchanged. An id already on a card is never regenerated — an import keeps the id
its source carried, and re-exporting a character keeps the id the card on disk
already has, so a gallery folder is never orphaned.

Cards built before this (or imported from a source with no id) can be caught up
in place:

```bash
make gallery-ids              # read-only report: which cards lack an id
make gallery-ids ARGS=--apply # mint and patch them in (pixels preserved)
```

## The archive API (`/api/v1`)

The archive itself lives in `data/` — `characters/` (the cards), `galleries/`
(their images), `cache/thumbs/` (browse-grid derivatives) — and is served by a
read-only HTTP contract of its own:

| Endpoint | What it gives you |
| --- | --- |
| `GET /api/v1/characters` | list + filter: `q`, `tag` (repeatable, ANDed), `creator`, `source`, `has_lorebook`, `has_gallery`, `health`, `sort`, `limit`/`offset` (`limit=0` = everything) |
| `GET /api/v1/characters/{filename}` | one card in full — the entire embedded V3 `data`, plus its gallery measured on disk |
| `GET /api/v1/characters/{filename}/png` | the card file, byte for byte — this is the single-card export |
| `GET /api/v1/characters/{filename}/thumb` | a ~10 KB derivative, generated on a cache miss |
| `GET /api/v1/facets` | every tag / creator / source with counts, for the filter UI |
| `GET /api/v1/stats` | archive and index health, including thumbnail coverage |
| `POST /api/v1/refresh` | force a rescan (endpoints already refresh themselves) |

Two decisions worth knowing, because both were the alternative being rejected:

**It is deliberately not SillyTavern's `/api` shape.** Answering
`/characters/edit-attribute` and friends would let CharacterLibrary's frontend be
vendored with no JavaScript changes at all — and would sign this server up to
impersonate SillyTavern's internals forever. What is worth keeping is *format*
compatibility (V3 PNG, the bundle zip, the `gallery_id` convention); none of that
requires matching anyone else's URLs. See `proxy/api/__init__.py`.

**There is no database.** A full scan — decode every PNG's tEXt chunk, base64,
JSON — costs ~1.3 s across all 3,839 cards, and the stat sweep that keeps the
index honest costs 21 ms. So the index is a dict in memory, rebuilt at startup and
re-validated per request on `(mtime, size)`; the filesystem stays the only source
of truth. A card acquired, renamed or deleted outside the process appears in the
grid within one debounce interval with no reindex step (`proxy/archive/catalog.py`).

Cards that fail to parse are recorded rather than skipped — `GET
/api/v1/characters?health=broken` lists them with the reason, because an archive
silently dropping the files it cannot read is the failure an archive exists to
prevent.

### Thumbnails

The grid cannot serve 800 KB originals, but it did not need a thumbnailer either:
both caches were inherited at cutover (SillyTavern's `thumbnails/avatar`, keyed by
the exact card filename, and CharacterLibrary's `cl_thumbs`) and covered 99.6% of
the archive on arrival. `make thumbs` closes the rest:

```bash
make thumbs               # report: missing, miscased, stale
make thumbs ARGS=--apply  # render the misses, retire the orphans
```

**Every inherited avatar thumb is JPEG data behind a `.png` extension** — all
4,663 of them. The media type is therefore always sniffed from the file's magic
number and never derived from its name; generated thumbs keep the same convention
so the cache stays uniform and drop-in compatible in both directions
(`proxy/archive/thumbs.py`).

## Code layout

```
proxy/
  server.py     the app: what gets mounted, in what order, how it is run
  config.py     settings, read from the environment then .env
  deps.py       the singletons the route modules share
  api/          every HTTP surface: /api/v1 (browse + edit, split by resource
                under v1/), the /build-* acquisition endpoints, the chat
                endpoints the sites are pointed at, and the wire schemas
  cards/        the card domain: models, builder, on-disk edits, PNG chunks,
                naming, galleries, lorebooks, avatars
  sources/      one module per site a card can come from (janitor, saucepan,
                chub, datacat, jannyai) plus the chat-prompt parsers
  text/         cleaning and formatting: macros, HTML→markdown, creator notes,
                name repair
  media/        the server-side media pipeline (Phase 3C)
  archive/      the archive as a collection: the index and the thumbnail cache
  state/        disk-backed working state: captures, lorebook cache, UI settings
  runtime/      the terminal dashboard and the canned chat responder
```

The dependency runs one way and does not cycle: `config` at the bottom, then the
leaf utilities, then `sources` mapping onto `cards`, then `api` on top. Nothing
below `api` knows about HTTP, and nothing below `sources` knows which site a card
came from. `tests/` mirrors this layout directory for directory.

## Tests

```bash
uv run pytest        # server (proxy/)
make test-js          # userscript (userscript/src_jai/)
```

All server-side logic (JanitorAI-JSON→V3 mapper, system-prompt parser, macro
repair, lorebook mapper, card builder, PNG round-trip, HTML→markdown, the datacat
and Chub import mappers, and the FastAPI routes) is validated against **real
captured fixtures** in `tests/fixtures/` (8 real `/hampter/characters/<id>`
payloads in `tests/fixtures/hampter/`, 2 real datacat card exports in
`tests/fixtures/datacat/`, a real Chub export in `tests/fixtures/chub/`) — see
`tests/fixtures/README.md` for provenance.

The userscript is authored as small modules under `userscript/src_jai/*.js`,
concatenated into one IIFE at compile time (`scripts/compile_userscript_jai.py`).
`userscript/tests/` (Node's built-in `node:test`, no dependencies) reproduces
that same concatenation for just the module(s) a test needs — see
`userscript/tests/helpers/load-src.js` — so pure logic can be unit-tested
without a browser. So far this only covers the bulk-download tag filter
(`bulk.tag-filter.test.js`) as a smoke test; the rest of the userscript's
DOM/network/in-page JSON+auth interaction still has no automated coverage and
is verified live.

## Status

`uv run pytest` green. Server-side JSON refactor complete; datacat + Chub.ai
bulk import live; live end-to-end verification of the userscript export flows is
pending.

A standalone **character archive** that runs beside SillyTavern rather than
inside it. Landed: `data/` holds the archive (3,868 cards), the browse and write
API above, 100% thumbnail coverage, on-disk UI settings (`data/settings.json`),
the local responder that replaced `mlx_client.py` — the MLX upstream was the
last thing tying the server to a second macOS-only process — and, on top of
that, the **container** above: builds land in the archive, every writable path
folded under `data/`, and the whole thing runs from one image with one mount.

**The UI rewrite is done** (`docs/UI_REWRITE_PLAN.md`, `docs/UI_PARITY_LEDGER.md`).
`web/` — 68k lines of vendored CharacterLibrary, still a SillyTavern extension
underneath — was replaced by the React client in `frontend/`, built against
`/api/v1` and nothing else, and deleted at the Stage 7 cut-over. The client now
owns `/`. The old directory is preserved on the **`legacy-web` branch**, which
is where the "ported from `web/...`" comments throughout `proxy/` and
`frontend/src/` resolve — and, until `POST /api/v1/bundle` exists, where a
bundle `.zip` gets restored from.

```
make run              # the server -- serves the client from frontend/dist at /
make frontend-dev     # the client on :5173, /api proxied to :8000
make api-schema       # regenerate the typed client after any route change
make frontend-test frontend-typing frontend-lint
make frontend-smoke   # the browser gate, against a running server
make pre-commit-install  # once per checkout: lint/format/typing on every commit
```

Deliberately out of scope unless a real need surfaces: reconstructing lorebook
entries from raw prompt text (real captures show no structural markers to
recover them — lorebooks come exclusively via the `/hampter/script` path).
