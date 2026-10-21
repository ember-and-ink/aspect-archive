/**
 * Discover's state, and its round trip through the URL.
 *
 * Same reasoning as `lib/browse.ts` for the archive side: a view you can link
 * to. It also makes the card preview possible at all — `/discover/chub/12345`
 * needs to know which feed the card came from to rebuild the set for prev/next,
 * and the query string is where that lives.
 */

import type { TagMode } from './browse'

export type Provider = 'chub' | 'datacat'

/** Discover's two feeds — the mock's `Discover | Following` pair. They are
 *  separate queries against separate endpoints, never a filter over each
 *  other. */
export type DiscoverMode = 'browse' | 'following'

// ---- Chub sorts ------------------------------------------------------------

/**
 * Chub's discovery presets, verbatim from
 * `web/modules/providers/chub/chub-browse.js:68-80`.
 *
 * A preset is a *sort plus a time window*, not just a sort — "Hot this week"
 * and "Most downloaded" are the same `download_count` ordering over different
 * `max_days_ago` values, and collapsing them (which is what shipping three bare
 * sorts did) loses the distinction that makes the list useful.
 */
export const CHUB_PRESETS = {
  trending: { label: 'Trending', sort: 'trending', days: 0 },
  popular_week: { label: 'Hot this week', sort: 'download_count', days: 7 },
  popular_month: { label: 'Hot this month', sort: 'download_count', days: 30 },
  popular_year: {
    label: 'Popular this year',
    sort: 'download_count',
    days: 365,
  },
  popular_all: { label: 'Most downloaded', sort: 'download_count', days: 0 },
  rated_week: { label: 'Top rated this week', sort: 'star_count', days: 7 },
  rated_all: { label: 'Top rated', sort: 'star_count', days: 0 },
  newest: { label: 'Newest', sort: 'id', days: 30 },
  updated: { label: 'Recently updated', sort: 'last_activity_at', days: 0 },
  // `default` is Chub's server-side relevance ordering; `newcomer` narrows to
  // new characters picking up activity.
  recent_hits: {
    label: 'Recent hits',
    sort: 'default',
    days: 0,
    specialMode: 'newcomer',
  },
  random: { label: 'Random', sort: 'random', days: 0 },
} as const satisfies Record<
  string,
  { label: string; sort: string; days: number; specialMode?: string }
>

export type ChubPreset = keyof typeof CHUB_PRESETS

export const DEFAULT_CHUB_PRESET: ChubPreset = 'popular_week'

/** Sorts for one author's catalogue (`chub-browse.js:460-465`). */
export const CHUB_CREATOR_SORTS = [
  { value: 'id', label: 'Newest' },
  { value: 'last_activity_at', label: 'Recently updated' },
  { value: 'download_count', label: 'Most downloaded' },
  { value: 'star_count', label: 'Top rated' },
] as const

// ---- DataCat sorts ---------------------------------------------------------

/**
 * DataCat's `/api/characters/fresh` orderings (`datacat-browse.js:1021-1027`),
 * each available over two windows. Together with plain `recent` that is the
 * eleven-option list the old UI offered and the new one offered none of — the
 * sort control was hidden entirely whenever DataCat was the active provider.
 */
export const DATACAT_FRESH_SORTS = [
  { value: 'fresh', label: 'Freshest' },
  { value: 'score', label: 'Score' },
  { value: 'chat_count', label: 'Chat count' },
  { value: 'messages_per_chat', label: 'Messages per chat' },
  { value: 'first_published', label: 'First published' },
] as const

export const DATACAT_WINDOWS = [
  { suffix: '24h', label: 'Last 24 hours' },
  { suffix: 'week', label: 'This week' },
] as const

export const DEFAULT_DATACAT_SORT = 'recent'

/** A DataCat browse sort, split back into what the request needs. `null` means
 *  plain `recent` over `/api/characters/recent-public`. */
export function parseDatacatSort(
  sort: string,
): { sortBy: string; window: 'last24h' | 'thisWeek' } | null {
  for (const w of DATACAT_WINDOWS) {
    const suffix = `_${w.suffix}`
    if (sort.endsWith(suffix)) {
      return {
        sortBy: sort.slice(0, -suffix.length),
        window: w.suffix === '24h' ? 'last24h' : 'thisWeek',
      }
    }
  }
  return null
}

/** Sorts for one creator's catalogue (`datacat-browse.js:1029-1033`). */
export const DATACAT_CREATOR_SORTS = [
  { value: 'chat_count', label: 'Most messages' },
  { value: 'newest', label: 'Newest' },
  { value: 'oldest', label: 'Oldest' },
] as const

// ---- Following sorts -------------------------------------------------------

/**
 * How a Following feed is ordered (`datacat-browse.js:2114-2131`).
 *
 * Following is loaded whole and sorted here, not asked for in an order — which
 * is the point of the feed: cards from every creator you follow, interleaved
 * newest-first, rather than one creator's catalogue after another's.
 */
export const FOLLOWING_SORTS = [
  { value: 'newest', label: 'Newest' },
  { value: 'oldest', label: 'Oldest' },
  { value: 'name_asc', label: 'Name A–Z' },
  { value: 'name_desc', label: 'Name Z–A' },
  { value: 'chat_count', label: 'Most messages' },
] as const

export type FollowingSort = (typeof FOLLOWING_SORTS)[number]['value']

export const DEFAULT_FOLLOWING_SORT: FollowingSort = 'newest'

// ---- The state -------------------------------------------------------------

export interface DiscoverState {
  provider: Provider
  mode: DiscoverMode
  /** Free-text search. Browse only — neither Following feed takes one. */
  q: string
  /** Whichever sort applies to the active provider and mode. */
  sort: string
  /** Set when browsing one creator's catalogue: their provider id (DataCat) or
   *  username (Chub). Independent of `mode` — you can reach a creator from
   *  either feed. */
  creator: string
  /** Shown beside the back banner; the id alone is not readable. */
  creatorName: string
  tags: Map<string, TagMode>
  hideHave: boolean
  /** Following only: show just the cards first seen inside the grace window
   *  (`RECENT_WINDOW_MS`). Kept in the URL when you switch to Discover, where
   *  it is inert, so switching back restores it. */
  recentOnly: boolean
}

/** The sort that applies when the URL names none, for a given provider/mode. */
export function defaultSort(state: {
  provider: Provider
  mode: DiscoverMode
  creator: string
}): string {
  if (state.creator) return state.provider === 'chub' ? 'id' : 'chat_count'
  if (state.mode === 'following') return DEFAULT_FOLLOWING_SORT
  return state.provider === 'chub' ? DEFAULT_CHUB_PRESET : DEFAULT_DATACAT_SORT
}

export function readDiscoverState(params: URLSearchParams): DiscoverState {
  const provider: Provider =
    params.get('provider') === 'datacat' ? 'datacat' : 'chub'
  const mode: DiscoverMode =
    params.get('mode') === 'following' ? 'following' : 'browse'
  const creator = params.get('creator') ?? ''
  const tags = new Map<string, TagMode>()
  for (const tag of params.getAll('tag')) tags.set(tag, 'inc')
  for (const tag of params.getAll('xtag')) tags.set(tag, 'exc')
  return {
    provider,
    mode,
    q: params.get('q') ?? '',
    sort: params.get('sort') ?? defaultSort({ provider, mode, creator }),
    creator,
    creatorName: params.get('creatorName') ?? '',
    tags,
    hideHave: params.get('have') === '0',
    recentOnly: params.get('recent') === '1',
  }
}

/** The inverse. Defaults are omitted so a plain `/discover` link stays plain. */
export function writeDiscoverState(state: DiscoverState): URLSearchParams {
  const params = new URLSearchParams()
  if (state.provider !== 'chub') params.set('provider', state.provider)
  if (state.mode !== 'browse') params.set('mode', state.mode)
  if (state.q) params.set('q', state.q)
  if (state.sort !== defaultSort(state)) params.set('sort', state.sort)
  if (state.creator) params.set('creator', state.creator)
  if (state.creatorName) params.set('creatorName', state.creatorName)
  for (const [tag, mode] of state.tags) {
    params.append(mode === 'inc' ? 'tag' : 'xtag', tag)
  }
  if (state.hideHave) params.set('have', '0')
  if (state.recentOnly) params.set('recent', '1')
  return params
}

// ---- Sort options for the active feed ---------------------------------------

export interface SortOption {
  value: string
  label: string
  /** Options are grouped under a heading, the way both providers' own selects
   *  group them. */
  group?: string
}

/**
 * Every ordering the active feed offers, and only those.
 *
 * Four catalogues because there are four feeds, and each provider publishes its
 * own. The rewrite shipped three hard-coded Chub sorts and, for DataCat, no
 * sort control at all — the button was hidden whenever DataCat was selected,
 * because nothing had been wired behind it.
 */
export function discoverSortOptions(state: DiscoverState): SortOption[] {
  if (state.creator) {
    const sorts =
      state.provider === 'chub' ? CHUB_CREATOR_SORTS : DATACAT_CREATOR_SORTS
    return sorts.map((s) => ({ value: s.value, label: s.label }))
  }
  if (state.mode === 'following') {
    return FOLLOWING_SORTS.map((s) => ({ value: s.value, label: s.label }))
  }
  if (state.provider === 'chub') {
    return Object.entries(CHUB_PRESETS).map(([value, preset]) => ({
      value,
      label: preset.label,
    }))
  }
  // DataCat: plain `recent`, then each fresh ordering over each window.
  return [
    { value: 'recent', label: 'Recent' },
    ...DATACAT_WINDOWS.flatMap((w) =>
      DATACAT_FRESH_SORTS.map((s) => ({
        value: `${s.value}_${w.suffix}`,
        label: s.label,
        group: w.label,
      })),
    ),
  ]
}

export function discoverSortLabel(state: DiscoverState): string {
  const match = discoverSortOptions(state).find((o) => o.value === state.sort)
  return match?.label ?? state.sort
}
