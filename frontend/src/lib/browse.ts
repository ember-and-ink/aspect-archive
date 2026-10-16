import type { components } from './api-schema'

export type Card = components['schemas']['CardOut']

/**
 * The browse state, and its round trip through the URL.
 *
 * It lives in the query string rather than in React state on purpose: every
 * filtered view is then a link — shareable, bookmarkable, and restored by the
 * back button — which is the thing the old UI could not do (docs/UI_REWRITE_PLAN.md
 * §2.5). Nothing else holds a copy, so there is no state to keep in sync.
 */

/** A chip that is on or off. The tag chips are separate — they have three states. */
export type Flag =
  'fav' | 'lore' | 'greets' | 'new' | 'untagged' | 'media' | 'fork' | 'lowres'

/** Click a tag once to require it, again to exclude it, again to drop it. */
export type TagMode = 'inc' | 'exc'

export type Scope = 'all' | 'name' | 'creator' | 'tags'

export interface BrowseState {
  q: string
  scope: Scope
  sort: string
  flags: Set<Flag>
  tags: Map<string, TagMode>
  /** Exact creator name, or '' for any. One at a time: the API matches a single
   *  creator exactly, and "more cards by this person" is a one-person question. */
  creator: string
  /** `source_kind`s to OR over — a whole platform's worth at a time, since one
   *  platform spans two kinds. Empty for any. */
  sources: string[]
}

/** Flag names in full, for the ＋ Filter list where there is room to read. */
export const FLAG_LABELS: Record<Flag, string> = {
  fav: 'Favorites',
  lore: 'Has a lorebook',
  greets: 'Multiple greetings',
  new: 'Added this week',
  untagged: 'Untagged',
  media: 'Needs media',
  fork: 'Forks',
  lowres: 'Low-res image',
}

/**
 * The same flags as chips. Shorter, because the strip sits on the toolbar row
 * beside the pills, the count and the sort, and full sentences there cost the
 * horizontal space that made the old strip unusable once a few were on.
 */
export const FLAG_CHIP_LABELS: Record<Flag, string> = {
  fav: 'Favorites',
  lore: 'Lorebook',
  greets: 'Greetings',
  new: 'This week',
  untagged: 'Untagged',
  media: 'Needs media',
  fork: 'Forks',
  lowres: 'Low-res',
}

/** The chips the strip always shows, in the mock's order. */
export const PRESET_FLAGS: Flag[] = ['lore', 'greets', 'new']

export const SORTS: { value: string; label: string; hint?: string }[] = [
  { value: 'name', label: 'Name', hint: 'A→Z' },
  { value: '-added', label: 'Recently added' },
  { value: '-lore', label: 'Lore entries' },
  { value: '-greetings', label: 'Greetings' },
  { value: '-description', label: 'Description length' },
  { value: 'creator', label: 'Creator' },
]

export function sortLabel(sort: string): string {
  const bare = sort.replace(/^-/, '')
  const match = SORTS.find((s) => s.value.replace(/^-/, '') === bare)
  return match ? match.label : sort
}

/** Whether `sort` runs against its option's natural direction. */
export function isReversed(sort: string, option: string): boolean {
  return sort.startsWith('-') !== option.startsWith('-')
}

export function readState(params: URLSearchParams): BrowseState {
  const flags = new Set<Flag>()
  for (const flag of params.getAll('flag')) {
    if (flag in FLAG_LABELS) flags.add(flag as Flag)
  }
  const tags = new Map<string, TagMode>()
  for (const tag of params.getAll('tag')) tags.set(tag, 'inc')
  for (const tag of params.getAll('xtag')) tags.set(tag, 'exc')
  const scope = params.get('scope')
  return {
    q: params.get('q') ?? '',
    scope: (['name', 'creator', 'tags'] as string[]).includes(scope ?? '')
      ? (scope as Scope)
      : 'all',
    sort: params.get('sort') ?? 'name',
    flags,
    tags,
    creator: params.get('creator') ?? '',
    sources: params.getAll('source').filter(Boolean),
  }
}

/**
 * The browse state as a query string.
 *
 * `defaultSort` is the sort the page would apply anyway when the URL is silent
 * — `ui2.defaultSort` from Settings, or `name`. Only *that* value is omitted,
 * which is what lets a user whose default is "Recently added" still pick
 * "Name": it is no longer the default, so it is written down. Omitting a
 * hardcoded `name` instead made that click a no-op, because the page read the
 * empty URL and re-applied the stored default over it.
 */
export function writeState(
  state: BrowseState,
  defaultSort = 'name',
): URLSearchParams {
  const params = new URLSearchParams()
  if (state.q) params.set('q', state.q)
  if (state.scope !== 'all') params.set('scope', state.scope)
  if (state.sort !== defaultSort) params.set('sort', state.sort)
  for (const flag of state.flags) params.append('flag', flag)
  for (const [tag, mode] of state.tags) {
    params.append(mode === 'inc' ? 'tag' : 'xtag', tag)
  }
  if (state.creator) params.set('creator', state.creator)
  for (const kind of state.sources) params.append('source', kind)
  return params
}

/**
 * The querystring a card tile carries onto its detail link, so prev/next can
 * rebuild the exact set the grid was showing.
 *
 * Everything the browse URL may legitimately leave out has to be spelled out
 * here, because the detail page reads this string alone — it has no settings
 * context and no route context. Two things get lost otherwise: the sort, when
 * it is the stored default, and the `fav` flag on `/favorites`, which the route
 * pins rather than the URL. Both left the detail page rebuilding a different
 * set, where the card is not found at all and prev/next silently dies.
 */
export function tileSearch(state: BrowseState): string {
  const params = writeState(state)
  params.set('sort', state.sort)
  return `?${params}`
}

export function isFiltered(state: BrowseState): boolean {
  return (
    state.flags.size > 0 ||
    state.tags.size > 0 ||
    state.q !== '' ||
    state.creator !== '' ||
    state.sources.length > 0
  )
}

/**
 * The start of "this week", as the `added_after` chip means it: midnight seven
 * days ago, local time.
 *
 * Rounded to the day rather than computed as `now - 7d` so that the value is
 * stable across renders — it is part of a TanStack Query key, and a timestamp
 * that moves every millisecond would refetch the grid on every render.
 */
export function weekAgo(now = new Date()): string {
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  start.setDate(start.getDate() - 6)
  return start.toISOString()
}

/** The browse state as `GET /api/v1/characters` takes it. */
export function toQuery(state: BrowseState) {
  const include: string[] = []
  const exclude: string[] = []
  for (const [tag, mode] of state.tags) {
    ;(mode === 'inc' ? include : exclude).push(tag)
  }
  return {
    ...(state.q ? { q: state.q, scope: state.scope } : {}),
    sort: state.sort,
    ...(include.length ? { tag: include } : {}),
    ...(exclude.length ? { exclude_tag: exclude } : {}),
    ...(state.creator ? { creator: state.creator } : {}),
    ...(state.sources.length ? { source: state.sources } : {}),
    ...(state.flags.has('fav') ? { favorite: true } : {}),
    ...(state.flags.has('lore') ? { has_lorebook: true } : {}),
    ...(state.flags.has('greets') ? { min_greetings: 2 } : {}),
    ...(state.flags.has('untagged') ? { untagged: true } : {}),
    ...(state.flags.has('media') ? { needs_media: true } : {}),
    ...(state.flags.has('new') ? { added_after: weekAgo() } : {}),
    ...(state.flags.has('fork') ? { is_fork: true } : {}),
    ...(state.flags.has('lowres') ? { low_res: true } : {}),
  }
}

/**
 * The name a tile shows: the source page's listing title when the grid is in
 * tagline mode, the character's own name otherwise.
 *
 * Most cards carry a `page_name` ("Offer You Can't Refuse | Abbie") that says
 * far more about the card than the bare name does, but plenty carry none at
 * all -- Chub and JAI both allow a listing titled with just the character --
 * so the name is the fallback rather than a blank tile.
 */
export function tileName(card: Card, listing: boolean): string {
  return listing && card.page_name ? card.page_name : card.name
}
