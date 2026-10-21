import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { apiClient, unwrap } from '@/lib/api-client'
import type { components } from '@/lib/api-schema'
import {
  chubAvatarUrl,
  fetchChubFollows,
  fetchChubPopularTags,
  fetchChubTimeline,
  searchChub,
  ChubAuthRequired,
  type ChubAuth,
  type ChubNode,
} from '@/lib/providers/chub'
import {
  datacatAvatarUrl,
  datacatCharacterId,
  datacatCreatorName,
  datacatName,
  fetchDatacatCreator,
  fetchDatacatCreatorAll,
  fetchDatacatCreatorCharacters,
  fetchDatacatFresh,
  fetchDatacatTags,
  resolveDatacatTagNames,
  searchDatacat,
  type DatacatCharacter,
  type DatacatCreator,
} from '@/lib/providers/datacat'
import {
  CHUB_PRESETS,
  DEFAULT_CHUB_PRESET,
  parseDatacatSort,
  type ChubPreset,
  type DiscoverState,
  type Provider,
} from '@/lib/discover-state'
import {
  useProviderSettings,
  useUpdateRoot,
  type FollowedCreator,
} from './use-settings'
import {
  captureProviderCard,
  type ProviderCapture,
} from './use-discover-preview'
import { invalidateArchive } from './use-card-mutations'

export type { Provider, DiscoverMode } from '@/lib/discover-state'

/** One provider result, normalized to what `DiscoverTile` and the have-guard
 * need. `raw` carries the row exactly as the provider returned it, since
 * reading or adding a card needs a further per-provider fetch (Chub's search
 * rows are summaries; DataCat's need a detail + download read). */
export interface DiscoverItem {
  key: string
  provider: Provider
  providerId: string
  name: string
  creator: string
  /** The creator's provider id where one exists (DataCat), so a tile can link
   *  to their catalogue. Chub keys creators by username, which `creator`
   *  already is. */
  creatorId: string
  avatarUrl: string
  tags: string[]
  /** For the Following feeds' client-side ordering. Epoch ms, 0 when unknown. */
  created: number
  chats: number
  /** When a Following load first returned this card, epoch ms on the server's
   *  clock (`proxy/state/seen.py`). Absent outside Following, and when the
   *  stamps could not be recorded. */
  firstSeen?: number
  raw: ChubNode | DatacatCharacter
}

/** How long a Following card counts as new after it first came back. A grace
 *  window rather than "since your last visit": a card seen once in passing
 *  stays findable for two days instead of vanishing on the next refresh. */
export const RECENT_WINDOW_MS = 48 * 60 * 60 * 1000

export function isRecent(item: DiscoverItem, now = Date.now()): boolean {
  return item.firstSeen !== undefined && now - item.firstSeen < RECENT_WINDOW_MS
}

const PAGE_SIZE = 48

/** How many results a Following page shows before the next scroll, matching
 *  the old UI's display window (`datacat-browse.js:117`). The whole feed is
 *  already in memory by then; this only paces the DOM. */
const FOLLOWING_PAGE = 60

function epoch(value: unknown): number {
  if (typeof value !== 'string' && typeof value !== 'number') return 0
  const ms = new Date(value).getTime()
  return Number.isFinite(ms) ? ms : 0
}

function fromChub(node: ChubNode): DiscoverItem {
  const id = node.id != null ? String(node.id) : ''
  const creator = node.fullPath?.split('/')[0] ?? ''
  return {
    key: `chub:${id || node.fullPath}`,
    provider: 'chub',
    providerId: id,
    name: node.name ?? 'Unknown',
    creator,
    creatorId: creator,
    avatarUrl: chubAvatarUrl(node),
    tags: node.topics ?? [],
    created: epoch(node.createdAt ?? node.created_at),
    chats: Number(node.n_favorites ?? node.starCount ?? 0) || 0,
    raw: node,
  }
}

/** Grid cards render ~150px; requesting this width keeps janitorai originals
 *  from decoding full-size (`datacat-api.js:75`, "what makes hampter grids
 *  chug"). */
const GRID_AVATAR_WIDTH = 400

function fromDatacat(hit: DatacatCharacter): DiscoverItem {
  const id = datacatCharacterId(hit)
  return {
    key: `datacat:${id}`,
    provider: 'datacat',
    providerId: id,
    name: datacatName(hit),
    creator: datacatCreatorName(hit),
    creatorId: String(hit.creator_id ?? hit.creatorId ?? ''),
    avatarUrl: datacatAvatarUrl(hit, { width: GRID_AVATAR_WIDTH }),
    tags: resolveDatacatTagNames(hit.tags),
    created: epoch(hit.createdAt ?? hit.created_at),
    chats: Number(hit.chatCount ?? hit.chat_count ?? 0) || 0,
    raw: hit,
  }
}

/**
 * Order a whole Following feed (`datacat-browse.js:2114-2131`).
 *
 * Applied to the merged set, not per creator — that interleaving is what makes
 * Following a feed rather than a list of catalogues stapled together.
 */
function sortFeed(items: DiscoverItem[], sort: string): DiscoverItem[] {
  const out = [...items]
  switch (sort) {
    case 'oldest':
      return out.sort((a, b) => a.created - b.created)
    case 'name_asc':
      return out.sort((a, b) => a.name.localeCompare(b.name))
    case 'name_desc':
      return out.sort((a, b) => b.name.localeCompare(a.name))
    case 'chat_count':
      return out.sort((a, b) => b.chats - a.chats)
    default:
      return out.sort((a, b) => b.created - a.created)
  }
}

function dedupe(items: DiscoverItem[]): DiscoverItem[] {
  const seen = new Set<string>()
  const out: DiscoverItem[] = []
  for (const item of items) {
    if (!item.key || seen.has(item.key)) continue
    seen.add(item.key)
    out.push(item)
  }
  return out
}

// ---- The Following feeds ---------------------------------------------------

/** Creators fetched at once while building DataCat's feed. Wider than the old
 *  UI's three (`datacat-browse.js:1998`) because each creator is now a single
 *  request rather than a whole paged catalogue. */
const CREATORS_PER_BATCH = 6

/** Cards read per followed creator. DataCat's creator endpoint pages newest
 *  first, so one page *is* "their latest". */
export const FOLLOWING_PER_CREATOR = 50

/** A whole-feed load plus what had to be left out to get it. */
interface FeedLoad {
  items: DiscoverItem[]
  /** Followed creators whose catalogue is longer than one page, so the feed
   *  holds only their newest `FOLLOWING_PER_CREATOR`. Zero means the feed is
   *  everything everybody has published. */
  truncated: number
  /** Following only: the first-seen stamps could not be recorded, so no card
   *  carries `firstSeen` and "Recent only" has nothing to go on. */
  seenFailed?: boolean
}

/**
 * Stamp a Following load's cards with when each was first seen.
 *
 * The whole load, every time it is fetched: the server stamps the ids it has
 * never had and answers every id's original stamp, so a card's `firstSeen` is
 * the first load that returned it, not this one. A failure leaves the feed
 * usable -- only "Recent only" depends on this -- and says so.
 */
async function stampFirstSeen(
  provider: Provider,
  feed: FeedLoad,
): Promise<FeedLoad> {
  const ids = feed.items.map((i) => i.providerId).filter(Boolean)
  if (ids.length === 0) return feed
  try {
    const { first_seen } = await unwrap(
      apiClient.POST('/api/v1/discover/seen', { body: { provider, ids } }),
      'could not record which cards are new',
    )
    return {
      ...feed,
      items: feed.items.map((item) =>
        item.providerId in first_seen
          ? { ...item, firstSeen: first_seen[item.providerId] }
          : item,
      ),
    }
  } catch {
    return { ...feed, seenFailed: true }
  }
}

/**
 * The newest cards from every creator you follow on DataCat.
 *
 * DataCat has no timeline endpoint, so the feed has to be *assembled* from the
 * per-creator catalogues, merged and deduped. It is one query rather than an
 * infinite one because the result has to be sorted across creators before any
 * of it can be shown — you cannot interleave by date what you have not fetched.
 *
 * Each creator contributes one page, not their whole catalogue: the endpoint
 * orders newest first, so a page is exactly the part of a catalogue a
 * date-ordered feed can show, and reading to the end cost one request per 50
 * cards per creator — a single 300-card creator was six round trips for rows
 * that sort below everyone's recent work anyway. The banner says so, and the
 * creator's own view (`?creator=`) still reads their catalogue whole.
 *
 * A creator whose page fails contributes nothing rather than failing the feed.
 */
async function datacatFollowingFeed(
  followed: FollowedCreator[],
): Promise<FeedLoad> {
  const items: DiscoverItem[] = []
  let truncated = 0
  for (let i = 0; i < followed.length; i += CREATORS_PER_BATCH) {
    const batch = followed.slice(i, i + CREATORS_PER_BATCH)
    const results = await Promise.all(
      batch.map((creator) =>
        fetchDatacatCreatorCharacters({
          creatorId: creator.id,
          limit: FOLLOWING_PER_CREATOR,
          offset: 0,
          sortBy: 'newest',
        }).catch(() => null),
      ),
    )
    for (const result of results) {
      if (!result) continue
      items.push(...result.characters.map(fromDatacat))
      // `total` is the honest signal; a full page stands in for it if the
      // endpoint ever stops sending one, so the note fails on rather than off.
      const more = result.totalCount
        ? result.totalCount > result.characters.length
        : result.characters.length >= FOLLOWING_PER_CREATOR
      if (more) truncated += 1
    }
  }
  return { items: dedupe(items), truncated }
}

/** Timeline pages walked before the per-author supplement takes over
 *  (`chub-browse.js:1913` — 8 pages, 20 rows each). */
const CHUB_TIMELINE_PAGES = 8

/** Authors fetched at once during the supplement (`chub-browse.js:1999`). */
const CHUB_AUTHORS_PER_BATCH = 5

/**
 * New cards from the authors you follow on Chub — timeline *and* supplement.
 *
 * `/api/timeline/v1` is the obvious source and is not sufficient: measured over
 * 12 pages it surfaced 30 distinct authors out of 61 followed, so a third of
 * the people you follow simply never appear in it. The old UI worked around
 * that by fetching each followed author's newest page directly and merging
 * (`supplementTimelineWithAuthorFetches`), and this does the same — that
 * supplement is the difference between "Following" and "whatever the timeline
 * felt like showing".
 */
async function chubFollowingFeed(auth: ChubAuth): Promise<FeedLoad> {
  const items: DiscoverItem[] = []
  for (let page = 1; page <= CHUB_TIMELINE_PAGES; page += 1) {
    const { nodes, hasMore } = await fetchChubTimeline({ auth, page })
    items.push(...nodes.map(fromChub))
    if (!hasMore) break
  }

  const follows = await fetchChubFollows(auth).catch(() => [])
  for (let i = 0; i < follows.length; i += CHUB_AUTHORS_PER_BATCH) {
    const batch = follows.slice(i, i + CHUB_AUTHORS_PER_BATCH)
    const results = await Promise.all(
      batch.map((creator) =>
        searchChub({
          username: creator.username,
          page: 1,
          sort: 'id',
          perPage: 24,
          auth,
        })
          .then((r) => r.nodes)
          .catch(() => [] as ChubNode[]),
      ),
    )
    for (const nodes of results) items.push(...nodes.map(fromChub))
  }
  return { items: dedupe(items), truncated: 0 }
}

// ---- The feed query --------------------------------------------------------

/**
 * Discover's grid, over four feeds that page in three different ways.
 *
 * Browse is genuinely paged over the network: Chub by page number, DataCat by
 * offset. Following is not — it is loaded whole (above) and then paged *here*,
 * client-side, because a feed spanning every creator you follow has to be
 * merged and sorted before its first row is correct. A creator's catalogue is
 * paged like browse for Chub and, on DataCat, read whole for the same reason
 * its sorts are client-side.
 *
 * `queryClient.fetchQuery` is what lets one `useInfiniteQuery` serve both: the
 * whole-feed load is a separate cached query, so page 2 slices what page 1
 * already fetched instead of re-fetching it.
 */
export function useDiscoverSearch(
  state: DiscoverState,
  opts: { auth?: ChubAuth; followed?: FollowedCreator[] } = {},
) {
  const client = useQueryClient()
  const { auth, followed = [] } = opts
  const { provider, mode, q, sort, creator, recentOnly } = state
  const followedIds = followed.map((c) => c.id).join(',')
  const authKey = auth?.token ? 'authed' : 'anon'

  // A creator overrides the feed: you reach their catalogue *from* Discover or
  // Following, and `mode` then only records where Clear goes back to. Reading
  // it the other way round made a creator opened from Following show the
  // Following feed again, one creator's name on the banner and everybody's
  // cards underneath.
  const fresh = creator ? null : parseDatacatSort(sort)

  /** The whole-feed loads, cached under their own keys so the infinite query's
   *  later pages are pure slicing. */
  const wholeFeed = async (): Promise<FeedLoad> => {
    // DataCat has no paged creator endpoint worth using here: its creator sorts
    // (most messages / oldest) are orderings over the whole catalogue, so the
    // catalogue is what gets read.
    if (creator) {
      return client.fetchQuery({
        queryKey: ['datacat-creator', creator, sort],
        staleTime: 5 * 60_000,
        queryFn: async () => ({
          items: (await fetchDatacatCreatorAll(creator, { sortBy: sort })).map(
            fromDatacat,
          ),
          truncated: 0,
        }),
      })
    }
    if (mode === 'following') {
      return client.fetchQuery({
        queryKey: ['following-feed', provider, authKey, followedIds],
        staleTime: 5 * 60_000,
        queryFn: async () =>
          stampFirstSeen(
            provider,
            provider === 'chub'
              ? await chubFollowingFeed(auth ?? {})
              : await datacatFollowingFeed(followed),
          ),
      })
    }
    return client.fetchQuery({
      queryKey: ['datacat-fresh', sort],
      staleTime: 5 * 60_000,
      queryFn: async () => {
        const windows = await fetchDatacatFresh({ sortBy: fresh!.sortBy })
        return { items: windows[fresh!.window].map(fromDatacat), truncated: 0 }
      },
    })
  }

  /** True when the active feed is one of the whole-list ones above. */
  const isWholeFeed = creator
    ? provider === 'datacat'
    : mode === 'following' || (provider === 'datacat' && fresh !== null)

  return useInfiniteQuery({
    queryKey: [
      'discover',
      provider,
      mode,
      q,
      sort,
      creator,
      // The token changes what Chub returns, so it belongs in the key -- but
      // only as a presence flag, never the secret itself.
      authKey,
      auth?.nsfw ?? true,
      mode === 'following' ? followedIds : '',
      // Only Following has stamps to filter on; elsewhere the flag is inert
      // and must not split the cache.
      mode === 'following' && !creator && recentOnly,
    ],
    initialPageParam: 1,
    queryFn: async ({ pageParam }) => {
      const index = pageParam

      if (isWholeFeed) {
        const feed = await wholeFeed()
        // Following interleaves creators; a single creator's catalogue is
        // already in the order it was asked for.
        const isFollowing = !creator && mode === 'following'
        // "Recent only" filters the whole feed *before* it is paged, never the
        // pages already on screen. The feed is in memory either way, so a
        // window holding three new cards is three cards and no more pages --
        // filtering after paging would leave the grid near-empty and the
        // scroll sentinel walking the rest of the feed to fill it.
        const kept =
          isFollowing && recentOnly
            ? feed.items.filter((i) => isRecent(i))
            : feed.items
        const ordered = isFollowing ? sortFeed(kept, sort) : kept
        const size = isFollowing ? FOLLOWING_PAGE : PAGE_SIZE
        const start = (index - 1) * size
        const slice = ordered.slice(start, start + size)
        return {
          items: slice,
          hasMore: start + slice.length < ordered.length,
          total: ordered.length as number | undefined,
          truncated: feed.truncated,
          feedSize: feed.items.length as number | undefined,
          seenFailed: feed.seenFailed ?? false,
        }
      }

      if (provider === 'chub') {
        const preset = creator
          ? { sort, days: 0, specialMode: undefined }
          : (CHUB_PRESETS[sort as ChubPreset] ??
            CHUB_PRESETS[DEFAULT_CHUB_PRESET])
        const { nodes, hasMore } = await searchChub({
          search: q,
          page: index,
          sort: preset.sort,
          days: preset.days,
          specialMode: 'specialMode' in preset ? preset.specialMode : undefined,
          username: creator || undefined,
          auth,
        })
        return {
          items: nodes.map(fromChub),
          hasMore,
          total: undefined as number | undefined,
          truncated: 0,
          feedSize: undefined as number | undefined,
          seenFailed: false,
        }
      }

      const offset = (index - 1) * PAGE_SIZE
      const { characters, totalCount } = await searchDatacat({
        search: q,
        limit: PAGE_SIZE,
        offset,
      })
      return {
        items: characters.map(fromDatacat),
        hasMore: offset + characters.length < totalCount,
        total: totalCount,
        truncated: 0,
        feedSize: undefined as number | undefined,
        seenFailed: false,
      }
    },
    getNextPageParam: (last, pages) =>
      last.hasMore ? pages.length + 1 : undefined,
    // A missing/expired Chub token is an expected state with its own UI, not a
    // transient failure worth retrying.
    retry: (count, error) => !(error instanceof ChubAuthRequired) && count < 2,
  })
}

/** Chub's follows, read-only by decision (Stage 6B B2) -- following and
 *  unfollowing happen on chub.ai itself. */
export function useChubFollows(auth: ChubAuth) {
  return useQuery({
    queryKey: ['chub-follows', auth.token ? 'authed' : 'anon'],
    enabled: Boolean(auth.token),
    staleTime: 5 * 60_000,
    retry: false,
    queryFn: () => fetchChubFollows(auth),
  })
}

/**
 * DataCat's followed creators, which are ours to manage: the list is local
 * settings data tied to no account, so this *is* the feature rather than a
 * cache of a remote one.
 */
export function useDatacatFollows() {
  const { datacatFollowedCreators } = useProviderSettings()
  const update = useUpdateRoot()

  const follow = useMutation({
    mutationFn: async (idOrUrl: string) => {
      const id = extractDatacatCreatorId(idOrUrl)
      if (!id) throw new Error('that does not look like a DataCat creator id')
      if (datacatFollowedCreators.some((c) => c.id === id))
        throw new Error('already following that creator')
      // Resolve the name so the stored row is readable later; a creator that
      // cannot be resolved is still followable by id rather than being refused.
      const creator = await fetchDatacatCreator(id)
      const row: FollowedCreator = {
        id,
        name: creator?.name ?? id,
        source: 'datacat',
      }
      await update.mutateAsync({
        datacatFollowedCreators: [...datacatFollowedCreators, row],
      })
      return row
    },
  })

  const unfollow = useMutation({
    mutationFn: (id: string) =>
      update.mutateAsync({
        datacatFollowedCreators: datacatFollowedCreators.filter(
          (c) => c.id !== id,
        ),
      }),
  })

  // Rows stored before the creator lookup was fixed carry the uuid as their
  // name (`fetchDatacatCreator` read keys DataCat does not send, so every
  // lookup came back null and `name` fell back to the id). Re-resolve those and
  // write the real names back, so an existing list heals itself instead of
  // needing an unfollow/refollow. The write lives in the query function rather
  // than an effect because several components hold this hook at once and
  // react-query runs one shared fetch for them -- an effect would fire per
  // instance and write the same list twice.
  const unresolved = datacatFollowedCreators
    .filter((c) => c.name === c.id)
    .map((c) => c.id)
  useQuery({
    queryKey: ['datacat-follow-names', unresolved.join(',')],
    enabled: unresolved.length > 0,
    staleTime: Infinity,
    retry: false,
    queryFn: async () => {
      const found = await Promise.all(
        unresolved.map((id) => fetchDatacatCreator(id).catch(() => null)),
      )
      const names = new Map(
        found
          .filter((c): c is DatacatCreator => !!c?.name && c.name !== c.id)
          .map((c) => [c.id, c.name]),
      )
      if (names.size === 0) return 0
      await update.mutateAsync({
        datacatFollowedCreators: datacatFollowedCreators.map((c) =>
          names.has(c.id) ? { ...c, name: names.get(c.id)! } : c,
        ),
      })
      return names.size
    },
  })

  return { creators: datacatFollowedCreators, follow, unfollow }
}

/** Accept a bare uuid, a profile URL, or a paste of either. */
export function extractDatacatCreatorId(input: string): string | null {
  const text = input.trim()
  const uuid = text.match(
    /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i,
  )
  return uuid ? uuid[0] : null
}

/**
 * The tag catalogue behind Discover's ＋ Filter popover.
 *
 * A suggestion list, not an authority: both providers truncate per-card tag
 * lists in list payloads (trap 1), so a tag missing here does not mean no card
 * carries it -- which is why the popover keeps a free-text search box.
 */
export function useProviderTags(provider: Provider, auth?: ChubAuth) {
  return useQuery({
    queryKey: ['provider-tags', provider],
    staleTime: 30 * 60_000,
    queryFn: async (): Promise<string[]> =>
      provider === 'chub'
        ? fetchChubPopularTags(auth)
        : (await fetchDatacatTags()).map((tag) => tag.name),
  })
}

/** Which of the currently-loaded provider ids are already archived --
 * `POST /characters/have`, the same `_<id8>` fragment match the userscript's
 * own `/existing` answers from (§3.8). Kept for a one-off point check; the
 * grid uses `useHaveFragments` below instead (see its docstring for why). */
export function useHaveGuard(providerIds: string[]) {
  return useQuery({
    queryKey: ['characters-have', providerIds.join(',')],
    enabled: providerIds.length > 0,
    queryFn: () =>
      unwrap(
        apiClient.POST('/api/v1/characters/have', {
          body: { ids: providerIds },
        }),
        'could not check the archive',
      ),
    select: (data) => new Set(data.have),
    staleTime: 30_000,
  })
}

/** The same `_<id8>` slice `proxy/cards/naming.py:id_fragment` derives --
 * strip characters a filename can't carry, keep the first 8. */
export function idFragment(providerId: string): string {
  return providerId.replace(/[^A-Za-z0-9_-]/g, '').slice(0, 8)
}

/**
 * Every fragment already on disk, fetched once and matched locally.
 *
 * Discover used to ask `POST /characters/have` about every id loaded *so
 * far*, growing with each scroll page. That made the query key churn on
 * every page (a fresh key with no seed data goes `undefined` between the
 * scroll and the round trip resolving, so "hide cards I have" would briefly
 * hide nothing), and reissued the whole cumulative list every time instead of
 * asking only about what was new. The old `web/` UI never had this problem:
 * it built one `Set` from the whole local library on load and matched
 * against it in memory (`browse-view.js:544-608`) — this is the same idea,
 * sized for an archive too big to hold the whole card list client-side: just
 * the fragments (`GET /characters/have-fragments`, a few KB), not the cards.
 * `useAddToArchive` below invalidates this after a successful add.
 */
export function useHaveFragments() {
  return useQuery({
    queryKey: ['characters-have-fragments'],
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/characters/have-fragments', {}),
        'could not check the archive',
      ),
    select: (data) => new Set(data.fragments),
    staleTime: 60_000,
  })
}

type BuildResponse = components['schemas']['BuildResponse']

/**
 * "Get" — capture the card in full, then hand it to the build route.
 *
 * One mutation for both providers, because the capture is the same object the
 * preview reads (`captureProviderCard`): a card you looked at is added without
 * fetching it again, and — the part that was actually broken — the capture
 * includes the lorebook. Chub's linked lorebook lives in a separate project and
 * DataCat's lives behind a per-script janitorai fetch; neither was ever
 * requested, so cards advertising a lorebook were being written without one.
 *
 * The server does the mapping and the write, exactly as before.
 */
export function useAddToArchive() {
  const client = useQueryClient()
  const { chubToken, chubNsfw } = useProviderSettings()
  return useMutation<
    BuildResponse,
    Error,
    {
      provider: Provider
      raw: ChubNode | DatacatCharacter
      capture?: ProviderCapture
    }
  >({
    mutationFn: async ({ provider, raw, capture }) => {
      const payload =
        capture ??
        (await captureProviderCard(provider, raw, {
          token: chubToken,
          nsfw: chubNsfw,
        }))
      if (payload.provider === 'chub') {
        return unwrap(
          apiClient.POST('/build-chub', {
            body: {
              node: payload.node,
              linked_lorebook:
                (payload.linked_lorebook as Record<string, unknown>) ?? null,
            },
          }),
          'could not add the card',
        )
      }
      return unwrap(
        apiClient.POST('/build-datacat', {
          body: {
            character: payload.character,
          },
        }),
        'could not add the card',
      )
    },
    onSuccess: () => {
      invalidateArchive(client)
      void client.invalidateQueries({ queryKey: ['characters-have'] })
      void client.invalidateQueries({
        queryKey: ['characters-have-fragments'],
      })
    },
  })
}
