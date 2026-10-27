/**
 * Chub (chub.ai) browse + fetch, at Discover's mock fidelity -- salvage item 5.
 * Ported from the request shapes in `web/modules/providers/chub/chub-api.js`
 * and `chub-browse.js`; the full browse view (author search, timeline, tag
 * dropdown, favourites) is not -- Discover only needs a search box and a grid.
 */

import { fetchWithProxy } from './shared'

export const CHUB_API_BASE = 'https://api.chub.ai'
export const CHUB_AVATAR_BASE = 'https://avatars.charhub.io/avatars/'

/**
 * Chub's URQL_TOKEN, sent as a plain `Authorization: Bearer` header.
 *
 * The name misleads: chub.ai's own site uses urql as its GraphQL client and
 * persists the session token under the `URQL_TOKEN` localStorage key, so that
 * is what the "how do I get my token" instructions tell you to copy. There is
 * no GraphQL on our side -- the value is used as a REST bearer token, exactly
 * as `web/modules/providers/chub/chub-api.js:45-53` used it.
 */
export interface ChubAuth {
  token?: string | null
  nsfw?: boolean
}

function chubHeaders(auth?: ChubAuth): HeadersInit {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (auth?.token) headers.Authorization = `Bearer ${auth.token}`
  return headers
}

/** Chub takes NSFW as two separate flags; the setting drives both. */
function nsfwParams(auth?: ChubAuth): { nsfw: string; nsfl: string } {
  const on = auth?.nsfw ?? true
  return { nsfw: String(on), nsfl: String(on) }
}

/** The fields Discover's tile and "have" guard need from a search row. Chub's
 * envelope shape varies by endpoint, so every field is read defensively. */
export interface ChubNode {
  id?: number | string
  fullPath?: string
  name?: string
  tagline?: string
  avatar_url?: string
  max_res_url?: string
  topics?: string[]
  starCount?: number
  n_favorites?: number
  [key: string]: unknown
}

function extractNodes(data: unknown): ChubNode[] {
  const d = data as Record<string, unknown>
  if (Array.isArray(d?.nodes)) return d.nodes as ChubNode[]
  const inner = d?.data as Record<string, unknown> | unknown[] | undefined
  if (inner && !Array.isArray(inner) && Array.isArray(inner.nodes))
    return inner.nodes as ChubNode[]
  if (Array.isArray(inner)) return inner as ChubNode[]
  if (Array.isArray(d)) return d as unknown as ChubNode[]
  return []
}

/**
 * Chub's `/search`, assembled the way `chub-browse.js:2618-2656` assembles it.
 *
 * The three rules that are easy to get wrong, and were:
 *
 * - A preset carries a **time window** (`max_days_ago`) and sometimes a
 *   `special_mode`, not just a sort. Sending the sort alone turns "Hot this
 *   week" into "Most downloaded ever".
 * - Both are dropped once there is a search term, so a query is not silently
 *   restricted to the last seven days.
 * - Browsing one author ignores the window entirely and uses that view's own
 *   sort — you want their whole catalogue, not their recent half.
 */
export async function searchChub(opts: {
  search?: string
  page?: number
  /** A `CHUB_PRESETS` entry, already resolved to its parts. */
  sort?: string
  days?: number
  specialMode?: string
  auth?: ChubAuth
  /** One creator's catalogue, by Chub username. */
  username?: string
  perPage?: number
}): Promise<{ nodes: ChubNode[]; hasMore: boolean }> {
  const {
    search = '',
    page = 1,
    sort = 'trending',
    days = 0,
    specialMode,
    auth,
    username,
    perPage = 48,
  } = opts
  const params = new URLSearchParams({
    first: String(perPage),
    page: String(page),
    ...nsfwParams(auth),
    include_forks: 'true',
    venus: 'false',
    min_tokens: '50',
  })
  if (search) params.set('search', search)
  // `default` is Chub's server-side relevance ordering, which is what you get
  // by not naming a sort at all.
  if (sort && sort !== 'default') params.set('sort', sort)
  if (username) {
    params.set('username', username)
  } else if (!search) {
    if (specialMode) params.set('special_mode', specialMode)
    if (days > 0) params.set('max_days_ago', String(days))
  }
  // NOTE: `topics`/`excludetopics` are deliberately never sent -- Chub's
  // server-side tag matching is unreliable. Tag filtering is client-side, in
  // `shared.ts`. See salvage item 6.

  const response = await fetchWithProxy(`${CHUB_API_BASE}/search?${params}`, {
    headers: chubHeaders(auth),
  })
  if (!response.ok)
    throw new Error(`Chub search failed: HTTP ${response.status}`)
  const data = await response.json()
  const nodes = extractNodes(data)
  // A full page means there is plausibly another; `/search` publishes no total
  // and its `cursor` is not populated on this endpoint.
  return { nodes, hasMore: nodes.length >= perPage }
}

/** The browse-tile avatar: `avatar_url` is Chub's 200x200 thumbnail, right
 *  for a grid and wrong for a card -- `/build-chub` picks the full-res image
 *  server-side (`aspect/sources/chub.py` `avatar_url`), so never send this. */
export function chubAvatarUrl(node: ChubNode): string {
  if (node.avatar_url) return node.avatar_url
  if (node.max_res_url) return node.max_res_url
  return node.fullPath ? `${CHUB_AVATAR_BASE}${node.fullPath}/avatar.webp` : ''
}

/** The full node (`?full=true`), which is what `POST /build-chub` needs --
 * search rows are summaries missing `definition`. */
export async function fetchChubFull(
  fullPath: string,
  auth?: ChubAuth,
): Promise<ChubNode | null> {
  const response = await fetchWithProxy(
    `${CHUB_API_BASE}/api/characters/${fullPath}?full=true`,
    { headers: chubHeaders(auth) },
  )
  if (!response.ok) return null
  const data = await response.json()
  return (data as { node?: ChubNode }).node ?? null
}

/**
 * A linked lorebook, resolved through Chub's v4 git API.
 *
 * Chub stores a lorebook two ways. An `embedded_lorebook` travels inside the
 * character's own definition and needs nothing extra. A *linked* one is a
 * separate Chub project, and the character node only says that it exists, via a
 * non-empty `related_lorebooks`; the content lives in that project's
 * `raw/card.json`, at the latest commit. Nothing but this fetch produces it —
 * which is why cards added before this existed came in without the lorebook
 * they advertise.
 *
 * Port of `web/modules/providers/chub/chub-api.js:186-216`. The server-side
 * mapper already knows what to do with the result and already prefers the
 * embedded book when the linked one turns out to be empty
 * (`aspect/sources/chub.py:118-124`) — this only has to hand it over.
 */
export async function fetchChubLinkedLorebook(
  projectId: number | string | undefined,
  auth?: ChubAuth,
): Promise<Record<string, unknown> | null> {
  if (!projectId) return null
  const headers = chubHeaders(auth)
  const base = `${CHUB_API_BASE}/api/v4/projects/${projectId}/repository`
  try {
    const commits = await fetchWithProxy(`${base}/commits`, { headers })
    if (!commits.ok) return null
    const log = (await commits.json()) as { id?: string }[]
    const ref = Array.isArray(log) ? log[0]?.id : undefined
    if (!ref) return null

    // The path is doubly encoded on purpose: it is `raw/card.json` with the
    // slash percent-encoded, and Chub's gateway decodes once before matching.
    const file = await fetchWithProxy(
      `${base}/files/raw%252Fcard.json/raw?ref=${encodeURIComponent(ref)}`,
      { headers },
    )
    if (!file.ok) return null
    return (await file.json()) as Record<string, unknown>
  } catch {
    // A card whose lorebook cannot be resolved is still worth keeping; the
    // server falls back to the embedded book, or to none.
    return null
  }
}

// ---- Following (read-only) -------------------------------------------------

/** Raised when Chub answers 401/403 -- the token is missing or stale. The
 *  Following UI shows "connect your token" rather than an error. */
export class ChubAuthRequired extends Error {
  constructor() {
    super('Chub token required')
    this.name = 'ChubAuthRequired'
  }
}

export interface ChubCreator {
  id: string
  name: string
  username: string
  avatar?: string
}

/**
 * The signed-in account's own username, needed because the follows endpoint is
 * keyed by username rather than by "me" (`chub-browse.js:2360-2445`).
 */
export async function fetchChubAccount(auth: ChubAuth): Promise<string | null> {
  if (!auth.token) throw new ChubAuthRequired()
  const response = await fetchWithProxy(`${CHUB_API_BASE}/api/account`, {
    headers: chubHeaders(auth),
  })
  if (response.status === 401 || response.status === 403)
    throw new ChubAuthRequired()
  if (!response.ok) return null
  const data = (await response.json()) as Record<string, unknown>
  const inner = (data.user ?? data) as Record<string, unknown>
  const name = inner.user_name ?? inner.username ?? inner.name
  return typeof name === 'string' ? name : null
}

/**
 * Who this account follows on Chub. Read-only by decision (Stage 6B B2):
 * following and unfollowing happen on chub.ai itself, so the archive never
 * issues writes against a third-party account.
 */
export async function fetchChubFollows(auth: ChubAuth): Promise<ChubCreator[]> {
  const username = await fetchChubAccount(auth)
  if (!username) return []
  const creators: ChubCreator[] = []
  // Paged until the reported count is reached, capped the way the old UI capped
  // it so a bad `count` cannot spin forever.
  for (let page = 1; page <= 20; page += 1) {
    const response = await fetchWithProxy(
      `${CHUB_API_BASE}/api/follows/${encodeURIComponent(username)}?page=${page}`,
      { headers: chubHeaders(auth) },
    )
    if (response.status === 401 || response.status === 403)
      throw new ChubAuthRequired()
    if (!response.ok) break
    const data = (await response.json()) as Record<string, unknown>
    const rows = (data.follows ?? data.nodes ?? data.data ?? []) as Record<
      string,
      unknown
    >[]
    if (!Array.isArray(rows) || rows.length === 0) break
    for (const row of rows) {
      const name = (row.user_name ?? row.username ?? row.name) as
        string | undefined
      if (!name) continue
      creators.push({
        id: String(row.id ?? name),
        name: (row.display_name as string) || name,
        username: name,
        avatar: row.avatar_url as string | undefined,
      })
    }
    const total = Number(data.count ?? 0)
    if (total && creators.length >= total) break
  }
  return creators
}

/**
 * The timeline carries whatever a followed author published -- lorebooks and
 * posts included. Only characters belong in Discover's grid, and they are told
 * apart the way `chub-browse.js:1849-1878` told them apart: by path prefix,
 * plus the presence of at least one character-only field for rows whose
 * `fullPath` is unhelpful.
 */
function isCharacterNode(node: ChubNode): boolean {
  const path = (node.fullPath ?? (node.full_path as string) ?? '').toLowerCase()
  if (path.startsWith('lorebooks/') || path.startsWith('posts/')) return false
  if (Array.isArray(node.entries)) return false
  return (
    path.includes('/') ||
    node.tagline !== undefined ||
    node.definition !== undefined ||
    node.first_mes !== undefined ||
    node.topics !== undefined
  )
}

/**
 * The "new from followed authors" feed.
 *
 * Page-numbered, despite the `cursor`/`previous_cursor` fields in its envelope:
 * verified live 2026-08-19, `/api/timeline/v1` answers `cursor: null` on every
 * page, ignores `first` (20 rows per page, always) and honours `page`. Reading
 * that null cursor as "no more pages" is what capped Following at a single page
 * of 20 cards. `count` is no help either -- it reports the feed total on page 1
 * and the page's own row count from page 2 on -- so the end of the feed is a
 * page that comes back empty.
 */
export async function fetchChubTimeline(opts: {
  auth: ChubAuth
  page?: number
}): Promise<{ nodes: ChubNode[]; hasMore: boolean }> {
  const { auth, page = 1 } = opts
  if (!auth.token) throw new ChubAuthRequired()
  const params = new URLSearchParams({
    first: '50',
    page: String(page),
    count: 'true',
    ...nsfwParams(auth),
  })

  const response = await fetchWithProxy(
    `${CHUB_API_BASE}/api/timeline/v1?${params}`,
    { headers: chubHeaders(auth) },
  )
  if (response.status === 401 || response.status === 403)
    throw new ChubAuthRequired()
  if (!response.ok)
    throw new Error(`Chub timeline failed: HTTP ${response.status}`)
  const data = (await response.json()) as Record<string, unknown>
  const nodes = extractNodes(data)
  // `hasMore` keys off the unfiltered page: a page that is all lorebooks is
  // still a page, and must not end the feed.
  return { nodes: nodes.filter(isCharacterNode), hasMore: nodes.length > 0 }
}

// ---- Tag catalogue ---------------------------------------------------------

/**
 * Chub publishes no tag catalogue, so the old UI built one by tallying `topics`
 * across a few hundred popular cards (`chub-browse.js:1262-1380`). Same idea
 * here, deliberately cheap: four sorts, one page each, counted and ranked.
 *
 * This is a *suggestion* list for the filter popover, not an authority -- trap
 * 1 in `shared.ts` means any given card's tags may be truncated, so a tag being
 * absent here does not mean no card carries it. The popover's own search box
 * lets a known tag be typed in regardless.
 */
export async function fetchChubPopularTags(auth?: ChubAuth): Promise<string[]> {
  const sorts = ['trending', 'download_count', 'id']
  const counts = new Map<string, { label: string; n: number }>()
  const pages = await Promise.all(
    sorts.map((sort) =>
      searchChub({ sort, page: 1, auth }).catch(() => ({
        nodes: [] as ChubNode[],
        hasMore: false,
      })),
    ),
  )
  for (const { nodes } of pages) {
    for (const node of nodes) {
      for (const topic of node.topics ?? []) {
        if (!topic) continue
        const key = topic.toLowerCase()
        const seen = counts.get(key)
        if (seen) seen.n += 1
        else counts.set(key, { label: topic, n: 1 })
      }
    }
  }
  return [...counts.values()]
    .sort((a, b) => b.n - a.n || a.label.localeCompare(b.label))
    .slice(0, 600)
    .map((entry) => entry.label)
}
