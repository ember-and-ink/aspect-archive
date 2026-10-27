/**
 * DataCat (datacat.run) browse + fetch, at Discover's mock fidelity -- salvage
 * item 5. Ported from `web/modules/providers/datacat/datacat-api.js`'s
 * network/browse/card-builder sections; the JanitorAI Supabase auth flow
 * (page-2+ Hampter unlock), MeiliSearch, and script hydration are deliberately
 * left out -- none of them are needed to browse and add a card, and the plan's
 * §3.8 scope is "goes through the existing dc-init / dc-proxy / dc-extract
 * session routes", not the whole legacy provider.
 */

import { apiClient } from '@/lib/api-client'

export const DATACAT_JANITOR_IMAGE_BASE =
  'https://ella.janitorai.com/bot-avatars/'

// Matches DataCat's own frontend default quality floor.
export const MIN_TOTAL_TOKENS = 889

export interface DatacatCharacter {
  character_id?: string
  characterId?: string
  chat_name?: string
  chatName?: string
  name?: string
  avatar?: string
  tags?: (string | { name?: string; slug?: string })[]
  creator_name?: string
  creatorName?: string
  primary_content_source_kind?: string | null
  primaryContentSourceKind?: string | null
  [key: string]: unknown
}

let bootstrap: Promise<boolean> | null = null
let warmed = false
let savedToken: string | null = null

/**
 * Hand the module the token persisted in settings (Stage 6B).
 *
 * The server keeps its session in process memory only, so after a restart it
 * holds nothing and `dc-init` would mint a brand-new anonymous identity --
 * discarding the one the user saved. Publishing the saved token here lets
 * `bootstrapSession` restore it *first* and only identify fresh if it is dead.
 */
export function setSavedDatacatToken(token: string | null | undefined): void {
  const next = token ?? null
  if (next === savedToken) return
  savedToken = next
  // A different token invalidates whatever the server currently holds.
  warmed = false
}

/** `dc-init`, shared across concurrent callers and re-armed after it settles
 * so a later 401 can trigger a fresh attempt -- same shape as the JS
 * `tryBootstrapSession`'s in-flight promise. A saved token is offered to the
 * server before falling back to a fresh anonymous identify. */
function bootstrapSession(): Promise<boolean> {
  if (!bootstrap) {
    bootstrap = (async () => {
      if (savedToken) {
        try {
          if (await datacatAdoptSavedToken(savedToken)) return true
        } catch {
          // fall through to a fresh identify
        }
      }
      const { data } = await apiClient.POST('/api/v1/datacat/dc-init', {
        body: { force: false },
      })
      return Boolean((data as { ok?: boolean } | undefined)?.ok)
    })()
      .catch(() => false)
      .finally(() => {
        bootstrap = null
      })
  }
  return bootstrap
}

/** One authenticated GET through the server's dc-proxy. The server holds no
 * session until the first `dc-init`, so the very first call of a page load
 * would otherwise always 401 -- `warmed` bootstraps once, proactively,
 * before the first request rather than reactively after a guaranteed miss.
 * A *later* 401 (the held token expired) still bootstraps again and retries
 * once. `path` is the full datacat.run API path, query string included
 * (e.g. `/api/characters/recent-public?limit=48`). */
async function dcFetch(path: string): Promise<Response> {
  if (!warmed) warmed = await bootstrapSession()
  let response = await fetch(`/api/v1/datacat/dc-proxy${path}`)
  if (response.status === 401 || response.status === 403) {
    warmed = await bootstrapSession()
    if (warmed) {
      response = await fetch(`/api/v1/datacat/dc-proxy${path}`)
    }
  }
  return response
}

export async function searchDatacat(opts: {
  search?: string
  limit?: number
  offset?: number
}): Promise<{ totalCount: number; characters: DatacatCharacter[] }> {
  const { search = '', limit = 48, offset = 0 } = opts
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    summary: '1',
    minTotalTokens: String(MIN_TOTAL_TOKENS),
  })
  if (search) params.set('search', search)

  const response = await dcFetch(`/api/characters/recent-public?${params}`)
  if (!response.ok)
    throw new Error(`DataCat search failed: HTTP ${response.status}`)
  const data = await response.json()
  return { totalCount: data.totalCount ?? 0, characters: data.characters ?? [] }
}

/**
 * DataCat's `/api/characters/fresh` — the endpoint behind every sort that is
 * not plain `recent` (`datacat-api.js:475-490`).
 *
 * It answers *two* windows in one call, `last24h` and `thisWeek`, each a whole
 * list rather than a page; the caller picks one and pages it client-side. That
 * shape is why the sort control needs its own request path instead of a
 * parameter on `searchDatacat`, and why it was easier to ship no sort control
 * at all than to wire it — which is what happened.
 */
export async function fetchDatacatFresh(opts: {
  sortBy: string
  limit24?: number
  limitWeek?: number
}): Promise<{ last24h: DatacatCharacter[]; thisWeek: DatacatCharacter[] }> {
  const { sortBy, limit24 = 200, limitWeek = 200 } = opts
  const params = new URLSearchParams({
    summary: '1',
    sortBy,
    limit24: String(limit24),
    limitWeek: String(limitWeek),
    minTotalTokens: String(MIN_TOTAL_TOKENS),
  })
  const response = await dcFetch(`/api/characters/fresh?${params}`)
  if (!response.ok)
    throw new Error(`DataCat fresh failed: HTTP ${response.status}`)
  const data = await response.json()
  const windows = data.windows ?? {}
  return {
    last24h: windows.last24h?.characters ?? [],
    thisWeek: windows.thisWeek?.characters ?? [],
  }
}

/**
 * `avatar`, resolved to a URL and — for janitorai-hosted originals only —
 * pulled down to a thumbnail.
 *
 * Port of `web/modules/providers/datacat/datacat-api.js:51-80`
 * (`resolveDatacatAvatarUrl`)'s width behaviour: janitorai's `bot-avatars` are
 * full-size originals (1024px+), so decoding one into a ~150px grid slot is
 * what made hampter grids chug. `?width=` only means something on that host —
 * datacat-native rows already resolve to an optimized `card.webp` variant, so
 * the host guard leaves those untouched rather than appending a no-op param.
 */
export function datacatAvatarUrl(
  hit: DatacatCharacter,
  opts: { width?: number } = {},
): string {
  const avatar = hit.avatar
  if (!avatar || typeof avatar !== 'string') return ''
  const url = /^https?:\/\//i.test(avatar)
    ? avatar
    : `${DATACAT_JANITOR_IMAGE_BASE}${avatar}`
  if (!opts.width) return url
  let hostname = ''
  try {
    hostname = new URL(url).hostname
  } catch {
    return url
  }
  if (!/(^|\.)janitorai\.com$/i.test(hostname)) return url
  return url + (url.includes('?') ? '&' : '?') + `width=${opts.width}`
}

export function datacatCharacterId(hit: DatacatCharacter): string {
  return hit.character_id ?? hit.characterId ?? ''
}

export function datacatName(hit: DatacatCharacter): string {
  return hit.chat_name ?? hit.chatName ?? hit.name ?? 'Unknown'
}

export function datacatCreatorName(hit: DatacatCharacter): string {
  return hit.creator_name ?? hit.creatorName ?? ''
}

/** Which upstream the card came from (`janitor_core`, `saucepan`, ...), needed
 * by the detail read. Creator rows spell it camelCase, browse rows
 * snake_case. */
export function datacatSourceKind(hit: DatacatCharacter): string | null {
  const kind = hit.primary_content_source_kind ?? hit.primaryContentSourceKind
  return typeof kind === 'string' && kind ? kind : null
}

export function resolveDatacatTagNames(
  tags: DatacatCharacter['tags'],
): string[] {
  if (!Array.isArray(tags)) return []
  return tags
    .map((t) =>
      typeof t === 'string' ? t.trim() : (t?.name ?? t?.slug ?? '').trim(),
    )
    .filter(Boolean)
}

/** The full detail payload `POST /build-datacat` needs. */
export async function fetchDatacatDetail(
  id: string,
  sourceKind?: string | null,
): Promise<DatacatCharacter | null> {
  const qs = sourceKind ? `?sourceKind=${encodeURIComponent(sourceKind)}` : ''
  const response = await dcFetch(`/api/characters/${id}${qs}`)
  if (!response.ok) return null
  const data = await response.json()
  return data.character ?? null
}

// ---- Lorebook script hydration ---------------------------------------------

/** The only `api_path` shape hampter serves a script from. Anything else on a
 *  `scripts[]` row is not a lorebook fetch and is left alone. */
const HAMPTER_SCRIPT_PATH = /^\/hampter\/script\/[a-f0-9-]{36}$/i

interface DatacatScript {
  type?: string
  is_public?: boolean
  is_code_public?: boolean
  api_path?: string
  script?: string
  settings?: string
}

/** True when the row advertises a public lorebook whose content was not
 *  obtained -- what a caller checks to know the card will import thin. */
export function hasUnfetchedLorebook(character: DatacatCharacter): boolean {
  const scripts = character?.scripts as DatacatScript[] | undefined
  if (!Array.isArray(scripts)) return false
  return scripts.some((s) => s?.type === 'lorebook' && s.is_public && !s.script)
}

/**
 * Fill in the lorebook content DataCat's detail payload leaves out, in place.
 *
 * DataCat stopped inlining `script` on its rows; a lorebook's entries now live
 * at `api_path` on janitorai.com, one fetch per script. Skipping this is why
 * DataCat cards were importing with an empty lorebook — the mapper
 * (`aspect/sources/datacat.py:extract_character_book_from_scripts`) reads
 * `script`, and there was never anything in it.
 *
 * Two things make this the browser's job rather than the server's, both ported
 * verbatim from `web/modules/providers/datacat/datacat-api.js:591-621`:
 * janitorai.com is CORS-open to a page but the endpoint only accepts browser
 * TLS fingerprints, so a plain `fetch` is required and `fetchWithProxy`'s
 * undici fallback is a guaranteed 403.
 *
 * Never throws. A script that will not load leaves the card thinner, which
 * `hasUnfetchedLorebook` reports; it does not stop the card being read or kept.
 */
export async function hydrateDatacatScripts(
  character: DatacatCharacter,
): Promise<boolean> {
  const scripts = character?.scripts as DatacatScript[] | undefined
  if (!Array.isArray(scripts) || !scripts.length) return true
  // Each script is an independent fetch to a different hampter path -- one
  // has no bearing on another, so nothing here needs to be sequential. A card
  // with several lorebook scripts used to pay their round trips one after
  // another before the preview could show anything; this pays the slowest of
  // them, once.
  await Promise.all(
    scripts.map(async (script) => {
      if (!script || script.type !== 'lorebook' || !script.is_public) return
      if (script.script) return
      // Listed publicly, but the creator locked the content; hampter serves
      // the metadata and nothing else, so asking is pointless rather than
      // failing.
      if (script.is_code_public === false) return
      if (
        typeof script.api_path !== 'string' ||
        !HAMPTER_SCRIPT_PATH.test(script.api_path)
      )
        return
      try {
        const response = await fetch(
          `https://janitorai.com${script.api_path}`,
          { headers: { Accept: 'application/json' } },
        )
        if (!response.ok) return
        const full = (await response.json()) as {
          script?: string
          settings?: string
        }
        if (typeof full?.script === 'string' && full.script) {
          script.script = full.script
          if (!script.settings && typeof full.settings === 'string')
            script.settings = full.settings
        }
      } catch {
        // Left unfetched; `hasUnfetchedLorebook` is how a caller notices.
      }
    }),
  )
  return !hasUnfetchedLorebook(character)
}

// ---- Session token lifecycle (Stage 6B B3) ---------------------------------

/**
 * The anonymous session token, made visible and controllable.
 *
 * `DatacatSession` holds it in process memory only
 * (`aspect/sources/datacat_client.py:239-244`), so before Stage 6B a server
 * restart silently minted a brand-new anonymous identity and there was no way
 * to see, refresh, or clear it. These four wrap the server routes that already
 * existed and were called by nothing; the caller persists the token to the
 * settings blob as `datacatToken` so it survives a restart, exactly as the old
 * UI did (`web/modules/providers/datacat/datacat-api.js:175-229`).
 */
export interface DatacatSessionState {
  valid: boolean
  /** Why not, when invalid -- shown verbatim in Settings so a dead token is
   *  diagnosable rather than just "off". */
  reason?: string
  totalCount?: number
}

/** NOTE: `dc-validate` answers `{valid, reason}`, *not* the `{ok}` every other
 *  route in this group uses (`aspect/sources/datacat_client.py:295-311`). */
export async function datacatValidate(): Promise<DatacatSessionState> {
  const { data } = await apiClient.GET('/api/v1/datacat/dc-validate', {})
  // The route is typed as a bare JSON object, so this narrows rather than
  // asserts -- via `unknown`, since the two shapes do not overlap structurally.
  const record = (data ?? {}) as unknown as Partial<DatacatSessionState>
  return {
    valid: Boolean(record.valid),
    reason: record.reason,
    totalCount: record.totalCount,
  }
}

/** Restore a previously saved token into the server's in-memory session. */
export async function datacatSetToken(token: string): Promise<boolean> {
  const { data } = await apiClient.POST('/api/v1/datacat/dc-set-token', {
    body: { token },
  })
  const ok = Boolean((data as { ok?: boolean } | undefined)?.ok)
  if (ok) warmed = true
  return ok
}

/** Mint a fresh anonymous identity (`force`), returning the new token so the
 *  caller can persist it. */
export async function datacatRefreshToken(): Promise<string | null> {
  const { data } = await apiClient.POST('/api/v1/datacat/dc-init', {
    body: { force: true },
  })
  const record = data as { ok?: boolean; token?: string } | undefined
  warmed = Boolean(record?.ok)
  return record?.token ?? null
}

export async function datacatClearToken(): Promise<void> {
  await apiClient.POST('/api/v1/datacat/dc-clear-token', {})
  warmed = false
}

/**
 * Adopt a token saved in settings, once per page load, before anything else
 * dials DataCat. Without this the server's warm-start mints a *new* anonymous
 * session on every restart and the saved one is dead weight.
 */
export async function datacatAdoptSavedToken(
  token: string | null | undefined,
): Promise<boolean> {
  if (!token) return false
  if (!(await datacatSetToken(token))) return false
  return (await datacatValidate()).valid
}

// ---- Following (Stage 6B B4) -----------------------------------------------

/**
 * DataCat has no account and no server-side follow list -- the followed-creator
 * list is purely local settings data (`datacatFollowedCreators`). That makes
 * this list *the* feature rather than a cache of one, which is why follow and
 * unfollow are built here in full while Chub's are deliberately not.
 */
export interface DatacatCreator {
  id: string
  name: string
  avatar?: string
  characterCount?: number
}

/**
 * NOTE: `/api/creators/{id}` does *not* speak the browse feed's vocabulary. It
 * answers `{success, creator}` where the creator carries `creatorId`,
 * `userName` and `charCount` -- there is no `name` and no `creator_name` on it
 * at all (verified live 2026-08-19). Reading the feed's spellings here made
 * every lookup return null, which is why following a creator stored the raw
 * uuid as their display name. `web/` read `creator.userName`
 * (`datacat-browse.js:1163`); so does this now, with the other spellings kept
 * as fallbacks so neither shape can break the other.
 */
export async function fetchDatacatCreator(
  id: string,
): Promise<DatacatCreator | null> {
  const response = await dcFetch(`/api/creators/${encodeURIComponent(id)}`)
  if (!response.ok) return null
  const data = await response.json()
  const creator = (data.creator ?? data) as Record<string, unknown>
  const name = (creator.userName ??
    creator.user_name ??
    creator.name ??
    creator.creator_name) as string | undefined
  if (!name) return null
  return {
    id: String(creator.creatorId ?? creator.id ?? creator.creator_id ?? id),
    // `avatar` alone is a bare filename; the display URLs are whole URLs.
    avatar: (creator.avatarDisplayUrl ??
      creator.avatar_display_url ??
      creator.avatar) as string | undefined,
    name,
    characterCount:
      Number(creator.charCount ?? creator.character_count ?? 0) || undefined,
  }
}

/**
 * NOTE: this endpoint does *not* answer in the `{totalCount, characters}` shape
 * `/api/characters/recent-public` uses -- it answers `{total, list}`, and its
 * rows are camelCase (`characterId`, `primaryContentSourceKind`) where the
 * browse feed's are snake_case. Reading the browse names here is what made
 * Following silently empty: every request returned 200 and every page mapped to
 * zero cards. Both spellings are accepted so neither feed can break the other.
 */
export async function fetchDatacatCreatorCharacters(opts: {
  creatorId: string
  limit?: number
  offset?: number
  sortBy?: string
}): Promise<{ totalCount: number; characters: DatacatCharacter[] }> {
  const { creatorId, limit = 48, offset = 0, sortBy = 'recent' } = opts
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    sortBy,
  })
  const response = await dcFetch(
    `/api/creators/${encodeURIComponent(creatorId)}/characters?${params}`,
  )
  if (!response.ok)
    throw new Error(`DataCat creator fetch failed: HTTP ${response.status}`)
  const data = await response.json()
  return {
    totalCount: data.total ?? data.totalCount ?? 0,
    characters: data.list ?? data.characters ?? [],
  }
}

/**
 * Everything one creator has published, paged to the end.
 *
 * `limit`/`offset` with a 50-row page, stopping on a short page or once
 * `total` is reached — `datacat-browse.js:2021-2042`. A page that fails keeps
 * what the creator already contributed rather than discarding the list, which
 * matters when the feed is fanning out over twenty creators and one of them
 * times out.
 *
 * The cap is a safety valve, not a policy: the two stop conditions above are
 * the real end, and this only bounds a `total` that never arrives.
 */
export async function fetchDatacatCreatorAll(
  creatorId: string,
  opts: { sortBy?: string; maxPages?: number } = {},
): Promise<DatacatCharacter[]> {
  const { sortBy = 'newest', maxPages = 40 } = opts
  const limit = 50
  const rows: DatacatCharacter[] = []
  for (let page = 0; page < maxPages; page += 1) {
    const result = await fetchDatacatCreatorCharacters({
      creatorId,
      limit,
      offset: page * limit,
      sortBy,
    }).catch(() => null)
    if (!result) break
    rows.push(...result.characters)
    if (result.characters.length < limit || rows.length >= result.totalCount)
      break
  }
  return rows
}

// ---- Tag catalogue ---------------------------------------------------------

export interface DatacatTag {
  id: string
  name: string
  count?: number
}

/**
 * DataCat's faceted tag catalogue -- a real published catalogue, unlike Chub's,
 * so no tallying is needed (`datacat-api.js:499-512`). Already allow-listed
 * server-side at `aspect/sources/datacat_client.py:227` and, until Stage 6B,
 * called by nothing.
 *
 * As with Chub, the result feeds the filter popover only; matching itself is
 * client-side and `tagIds` is never sent as a query param (trap 2).
 */
export async function fetchDatacatTags(): Promise<DatacatTag[]> {
  const params = new URLSearchParams({
    mode: 'recent',
    minTotalTokens: String(MIN_TOTAL_TOKENS),
  })
  const response = await dcFetch(`/api/tags/faceted?${params}`)
  if (!response.ok) return []
  const data = await response.json()
  const rows = (data.tags ?? []) as Record<string, unknown>[]
  return rows
    .map((row) => ({
      id: String(row.id ?? row.slug ?? row.name ?? ''),
      name: String(row.name ?? row.slug ?? ''),
      count: Number(row.count ?? row.total ?? 0) || undefined,
    }))
    .filter((tag) => tag.name)
}
