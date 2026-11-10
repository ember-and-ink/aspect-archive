# Fixture provenance

All fixtures here are **real captures**, not hand-written synthetic data. They were
trimmed 2026-07-17 to one fixture per distinct behaviour after the DOM→JSON refactor —
orphaned, redundant, and DOM-era reference material was removed (see git history).

## `system_prompt_hidden_*.txt`
Raw hidden-definition captures — the exact text the server saw in the first `system`
message after sending "hello"/"hi" to a hidden character's chat. Primary fixtures for
`prompt_parser.py` and the hidden-card capture flow (`capture_store.py`).

- `system_prompt_hidden_{lyra,lyra_2,ari,aubrey_evans}.txt` — hidden-definition cards.
  `lyra` and `lyra_2` are two independent captures of the same character (differ by one
  trailing space — a useful whitespace-tolerance edge case, not a meaningful diff).

The old `system_prompt_open_*.txt` captures were removed with the JSON refactor: open
cards now take their definition from the `/hampter` JSON, so the parser's output is only
load-bearing for hidden cards. `prompt_parser` still tolerates the open format — that's
covered by the graceful-degradation tests in `test_prompt_parser.py`, not by dedicated
open-card fixtures.

## `hampter/*.json`
Real `GET /hampter/characters/<id>` API payloads — the clean JSON that replaced DOM
scraping (see the `jai_proxy_janitor_api` memory). Captured 2026-07-16 in-page (bearer
JWT). Primary fixtures for `sources.janitor.py`. Six cards, 4 open + 2 hidden — one per
distinct mapping behaviour.

- `open_{nyla,akane_kujo,vaelyra,lila}.json` — open cards (`showdefinition: true`):
  `personality`/`scenario`/`example_dialogs` populated, `first_messages[0]` is the primary
  greeting. `open_lila` keeps its own **lorebook** closed but the card itself is open; it
  was renamed from `closed_lila` because the `closed_` prefix on the hidden cards below
  tracks card visibility, and the old name read as a hidden card. `open_akane_kujo.json` is
  the same character as the `akane_kujo_*_lorebook.json` files (multi-way cross-check); its
  tag mapping is pinned by `test_janitor_mapper.test_akane_maps_to_trusted_reference_metadata`,
  and its one LRM-only "separator" greeting is dropped to reproduce the trusted 9-greeting
  count.
- `closed_{amaya,selene}.json` — genuinely hidden (`showdefinition: false`): the server
  omits `personality`/`scenario`/`example_dialogs`/`first_message` and nulls
  `first_messages[0]`, leaking only the alternate greetings. Everything else
  (name/creator/tags/creator_notes/avatar) is still present — those come from the JSON even
  for hidden cards; the definition body + primary greeting come from the chat capture.

## `chat_request_hidden_ari.json`
A real JanitorAI chat-completions request body for a hidden card (Ari):
`[system(hidden def), user ".", assistant(rendered primary greeting), user "USER: hello"]`.
Proof that the primary greeting rides in as the first `assistant` message — the structure
`capture_store.record(..., primary_greeting=...)` depends on. Exercised by
`tests/test_server.py`.

## `datacat/*.json`
The base64-decoded character card embedded in two real datacat PNG exports (the `ccv3`
chunk of `import/*.png`, captured 2026-07-21). Primary fixtures for `sources.datacat.py`.
datacat writes a full `chara_card_v3` with macros intact and no `character_book`, so these
back the `task import` path.

- `abigail.json` (toraval) — single greeting, no alternates; HTML `creator_notes`.
- `alt_greetings.json` (Aoi, AlissaOne) — a `first_mes` plus 5 `alternate_greetings`.

## `chub/*.json`
The base64-decoded `data` object embedded in a real Chub.ai PNG export (the `ccv3`
chunk of an `import/*.png`, captured 2026-07-22), wrapped as `{"data": …}`. Primary
fixture for `sources.chub.py`. Unlike datacat, a Chub card is an already-complete
`chara_card_v3` with its own `character_book` and a rich `extensions` block, so this
backs the passthrough import path (clean the text, preserve the rest).

- `sakura_makoto.json` (Sancay) — 5 lorebook entries (with `probability` /
  `selectiveLogic` extras), a styled `creator_notes` blurb carrying a `<style>` CSS
  block, and a `depth_prompt` extension.

## `saucepan/`
**Different site (saucepan.ai), not JanitorAI.** This now backs the live `/build-saucepan`
path (the "New saucepan engine"), not a parked userscript.

### `saucepan_*.json` — companion payloads
Real captures of the `{id, definition, companion, lorebooks}` bundle the userscript posts
to `/build-saucepan`. Primary fixtures for `sources.saucepan.py`; each covers a distinct
case (see `test_saucepan_mapper.py` / `test_server.py`):

- `saucepan_04a0c1ac` (Eve) — open card, macros intact, plus a merged lorebook; the
  crown-jewel end-to-end fixture, spot-checked against the JanitorAI Akane for parity.
- `saucepan_1155a61e` (Taryn) — no Advanced Prompt; Response Formatting folds into scenario.
- `saucepan_7aef6bad` (Akane) — the JanitorAI mirror: macro fidelity + a heavy 110-entry
  (20 + 90) merged lorebook. Largest fixture, kept for the cross-site parity check.
- `saucepan_ff6eb375` (JJ) — carries Example Dialogue alongside a formatting scenario.
- `saucepan_closed_83831943` — a hidden companion (`open_definition: false`): the
  definition API returns a decoy `{error}`, so only the public v2 fields survive
  (name/blurb/greetings/avatar); scenario + example dialogue come back empty.

### `lorebook_chapter{0,1}.json` — obfuscated fragment format
Two real chapter responses (`GET /api/v2/lorebooks/<id>/chapters/<index>`), shape
`{index, title, text_fragments: {version, mask, fragments: [{text, key, proof}, ...]}}`.
`fragments` is a shuffled bag of real prose **mixed with decoys** (~25% of each payload).
Reassembly — keep fragments whose `proof` validates, sort by `key XOR mask`, concatenate —
was reverse-engineered from saucepan's minified bundle and now lives **server-side** in
`proxy/saucepan_fragments.py` (used by `sources.saucepan.py` on every live build).
`tests/test_saucepan_lorebook.py` pins the algorithm against these two files — the only
real captures of that format in existence. The trap they document: decoy ordinals all sort
*past* the real prose, so an implementation that skips `proof` validation emits the correct
text and then appends word-salad — it looks right unless you read the tail.

- `lorebook_chapter0.json` — "Eve's Father", mask 1977, 62/82 fragments real → 1827 chars.
- `lorebook_chapter1.json` — "The Bullies", mask 25346, 156/208 real → 4829 chars.
  (`mask` is per-payload, not a build constant.)

## `hampter_script_kamii_university.json`, `akane_kujo_jai_lorebook.json`, `akane_kujo_st_lorebook.json`
Real lorebook fixtures for Akane Kujo's "Kamii University: A Living Campus" script — for
`lorebook.py`. Same character as `hampter/open_akane_kujo.json`, so this is a
lorebook-specific facet of that same real capture set.

- `akane_kujo_jai_lorebook.json` — the real 20-entry JanitorAI script array (the parsed
  contents of a `/hampter/script/<id>` response's `script` field), manually exported by
  the owner from `https://janitorai.com/scripts/9e345de7-1e25-4f1b-8aec-6ea0b10b8b6b`
  ("Lorebook source"). This is the ground-truth INPUT to `LorebookMapper.map()`.
- `akane_kujo_st_lorebook.json` — a SillyTavern World Info export for the same character,
  re-exported by the owner from a previously-built card. SillyTavern's own
  `entries: {"0": {...}, ...}` (its native worldinfo shape: `order`/`disable`/`uid`/
  `outletName`/etc, NOT the V3 shape) is **not** directly comparable to our
  `character_book.entries` output — treat it as a loose sanity check, not a schema to
  imitate. Its `originalData` field, however, is gold: SillyTavern preserves the *pristine*
  V3 `character_book` it imported (before flattening into its own native entries) verbatim —
  `{name, description, scan_depth, token_budget, recursive_scanning, extensions, entries[]}`,
  exactly `models.CharacterBook`'s shape. That `originalData` covers a two-book merge (Kamii
  University's 20 entries + a second "Sex Positions & Kinks" script's 90, per its
  `extensions.jai_sources` — the owner only handed over the raw JAI-side script for the first
  book, to keep fixtures compact) so `originalData.entries[0:20]` is real ground-truth OUTPUT
  for mapping `akane_kujo_jai_lorebook.json`'s 20 entries, confirmed index-for-index by
  matching `content`/`keys` (see `tests/test_lorebook.py`).

  **Field-for-field validated:** feeding `akane_kujo_jai_lorebook.json` through
  `LorebookMapper.map()` as a single book produces output identical to
  `originalData.entries[0:20]` on every field except `comment` — which differs only by the
  `[Kamii University: A Living Campus] ` prefix `originalData` carries from its original
  two-book merge context (`multi_book=True` there vs `False` in the single-book test); the
  base `comment` (post-prefix) is identical. This is about as strong a real-data validation
  as this port gets.
- `hampter_script_kamii_university.json` — the full raw `/hampter/script/<id>` response
  envelope (`{type, id, title, description, depth, settings, script, ...}`), built by
  wrapping `akane_kujo_jai_lorebook.json`'s 20 real entries in the real envelope fields
  confirmed via a browser console dump made while probing JanitorAI's `/hampter/*`
  endpoints — which confirms the real endpoint is `/hampter/script/<id>` singular, not
  `/hampter/scripts/<id>` or `/hampter/lorebooks/<id>` (both 404 there). This is the fixture
  `LorebookMapper.map()` tests actually feed in, since it's the shape a real `/build-jai`
  `lorebooks[].raw` payload has.

**Do NOT confuse with `saucepan/lorebook_chapter*.json`** — a different, obfuscated shape
entirely, from a different *site*. They are not `/hampter/script` responses, and their
`mask`/`proof` fields correspond to nothing `LorebookMapper` reads.

Cross-check: entry 0's `content` field (the "Kamii University: The Living Campus..." prose)
also appears **verbatim, completely undelimited** in the raw hidden-capture system prompts —
real proof that an activated lore entry gets folded straight into the chat system prompt
with zero structural markers. See `proxy/sources/prompts/janitor.py`'s comment on trailing content and
`proxy/state/captures.py`'s comment on why `lore_entries` accumulation from raw prompt text
isn't attempted.
