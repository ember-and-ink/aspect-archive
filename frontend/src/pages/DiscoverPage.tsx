import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Search, X } from 'lucide-react'
import { CardGrid } from '@/components/CardGrid'
import { DiscoverTile } from '@/components/DiscoverTile'
import { DiscoverSort } from '@/components/DiscoverSort'
import { DiscoverTagFilter } from '@/components/DiscoverTagFilter'
import { FollowingManager } from '@/components/FollowingManager'
import { type TagSelection } from '@/components/discover-tags-def'
import { useDebounced } from '@/hooks/use-debounced'
import {
  FOLLOWING_PER_CREATOR,
  idFragment,
  isRecent,
  useAddToArchive,
  useDatacatFollows,
  useDiscoverSearch,
  useHaveFragments,
} from '@/hooks/use-discover'
import { useProviderSettings, useSettings } from '@/hooks/use-settings'
import {
  defaultSort,
  readDiscoverState,
  writeDiscoverState,
  type DiscoverMode,
  type DiscoverState,
} from '@/lib/discover-state'
import {
  hasTagFilters,
  matchesTagFilters,
  withPersistentExcludes,
} from '@/lib/providers/shared'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'

/**
 * Search providers and add straight to the archive (docs/UI_REWRITE_PLAN.md
 * §3.8, §4.5) — Chub and DataCat, not the whole legacy provider stack.
 *
 * Rebuilt against `web/modules/providers/` after the first version turned out
 * to have been built against the mock's Discover route alone, which is a grid
 * and a chip bar. What the mock does not show — and `web/` does — is that
 * Discover has four feeds (browse and Following, per provider), each with its
 * own ordering, and that a provider card is *readable* before you keep it.
 *
 * State lives in the query string, like the archive side: a Discover view is a
 * link, and the card preview at `/discover/:provider/:id` reads the same params
 * to rebuild this grid for its prev/next.
 */
export function DiscoverPage() {
  const [params, setParams] = useSearchParams()
  const state = readDiscoverState(params)
  const { provider, mode, creator, creatorName } = state

  const settings = useSettings()
  const { chubToken, chubNsfw, providerExcludeTags } = useProviderSettings()
  // Absent (nothing saved, or still loading) reads as both enabled -- the
  // page's behaviour before the Settings toggle existed.
  const providersConf = settings.data?.ui2?.providers
  const chubOn = providersConf?.chub !== false
  const datacatOn = providersConf?.datacat !== false

  // The search box types faster than the provider answers; the URL takes the
  // debounced value so the address bar does not thrash either.
  const [rawSearch, setRawSearch] = useState(state.q)
  const search = useDebounced(rawSearch, 350)

  const patch = useCallback(
    (next: Partial<DiscoverState>) => {
      const merged = { ...readDiscoverState(params), ...next }
      // A sort belongs to the feed that offers it, so switching feed resets it
      // rather than carrying a name the new feed has never heard of.
      if (
        next.provider !== undefined ||
        next.mode !== undefined ||
        next.creator !== undefined
      ) {
        if (next.sort === undefined) merged.sort = defaultSort(merged)
      }
      setParams(writeDiscoverState(merged), { replace: true })
    },
    [params, setParams],
  )

  useEffect(() => {
    if (search !== state.q) patch({ q: search })
  }, [search, state.q, patch])

  const auth = useMemo(
    () => ({ token: chubToken, nsfw: chubNsfw }),
    [chubToken, chubNsfw],
  )
  const { creators: datacatFollows } = useDatacatFollows()

  // A provider turned off in Settings disappears from the toggle; if it was
  // the active one, fall back to whichever provider is still on.
  useEffect(() => {
    if (provider === 'chub' && !chubOn && datacatOn)
      patch({ provider: 'datacat' })
    else if (provider === 'datacat' && !datacatOn && chubOn)
      patch({ provider: 'chub' })
  }, [provider, chubOn, datacatOn, patch])

  const results = useDiscoverSearch(state, { auth, followed: datacatFollows })
  const {
    data,
    error,
    isPending,
    isFetching,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    refetch,
  } = results

  const items = useMemo(() => data?.pages.flatMap((p) => p.items) ?? [], [data])
  const lastPage = data?.pages[data.pages.length - 1]
  const total = lastPage?.total
  // DataCat's Following feed reads one page per creator (their newest), so a
  // creator with more than that is represented, not exhausted. Say so rather
  // than let the count read as "everything they have published".
  const truncatedCreators = lastPage?.truncated ?? 0

  // "Recent only" is applied inside `useDiscoverSearch`, over the whole feed
  // before paging -- so `total` is already the recent count, and nothing here
  // has to fetch more pages to fill a sparse window.
  const recentFeed = mode === 'following' && !creator
  const recentOn = recentFeed && state.recentOnly
  const seenFailed = recentFeed && (lastPage?.seenFailed ?? false)

  const tags: TagSelection = useMemo(
    () => ({
      include: [...state.tags]
        .filter(([, m]) => m === 'inc')
        .map(([tag]) => tag),
      exclude: [...state.tags]
        .filter(([, m]) => m === 'exc')
        .map(([tag]) => tag),
    }),
    [state.tags],
  )

  // Client-side only, always -- neither provider's server-side tag matching is
  // trustworthy (`lib/providers/shared.ts`, trap 2). The persistent
  // `providerExcludeTags` entry is layered underneath as a floor the chips
  // cannot re-admit.
  const filters = useMemo(
    () => withPersistentExcludes(tags, providerExcludeTags?.[provider]),
    [tags, providerExcludeTags, provider],
  )
  const filtering = hasTagFilters(filters)
  const tagMatched = useMemo(
    () =>
      filtering
        ? items.filter((i) => matchesTagFilters(i.tags, filters))
        : items,
    [items, filters, filtering],
  )

  // Read once and matched locally -- see `useHaveFragments`.
  const have = useHaveFragments()
  const haveFragments = have.data ?? new Set<string>()
  const isHave = (providerId: string) =>
    haveFragments.has(idFragment(providerId))

  const haveCount = tagMatched.filter((i) => isHave(i.providerId)).length
  const visible = state.hideHave
    ? tagMatched.filter((i) => !isHave(i.providerId))
    : tagMatched

  const feedNote =
    (recentOn && lastPage?.feedSize !== undefined
      ? ` · new in the last 48 hours, of ${lastPage.feedSize.toLocaleString()} in your feed`
      : '') +
    (truncatedCreators > 0
      ? ` · showing the most recent ${FOLLOWING_PER_CREATOR} per creator`
      : '')

  const hiddenNote = haveCount ? ` · ${haveCount} already in the archive` : ''

  // Trap 1: a page's own tag lists are truncated, so filtering thins results
  // unpredictably. Pull a few more pages rather than letting a live filter look
  // like an empty provider -- bounded, so a filter matching nothing still
  // settles instead of walking the whole provider.
  const autoPages = useRef(0)
  useEffect(() => {
    autoPages.current = 0
  }, [filters, provider, mode, search])
  useEffect(() => {
    if (!filtering || !hasNextPage || isFetching) return
    if (visible.length >= 12 || autoPages.current >= 3) return
    autoPages.current += 1
    void fetchNextPage()
  }, [filtering, hasNextPage, isFetching, visible.length, fetchNextPage])

  const chubNeedsToken =
    provider === 'chub' && mode === 'following' && !chubToken && !creator
  const noFollows =
    provider === 'datacat' &&
    mode === 'following' &&
    !creator &&
    datacatFollows.length === 0

  const add = useAddToArchive()
  const [addingKey, setAddingKey] = useState<string | null>(null)

  const sentinelRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const element = sentinelRef.current
    if (!element || !hasNextPage) return
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) void fetchNextPage()
      },
      { rootMargin: '600px' },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [hasNextPage, fetchNextPage, visible.length])

  const gridSearch = writeDiscoverState(state).toString()

  const client = useQueryClient()
  const now = Date.now()

  return (
    <>
      <div className="sticky top-0 z-20 border-b border-line-soft bg-ground">
        <div className="mx-auto flex max-w-[1560px] flex-wrap items-center gap-3.5 px-5 py-3">
          <h1 className="text-[19px] font-semibold tracking-[-0.015em] whitespace-nowrap">
            Discover
          </h1>
          <span className="text-[12.5px] whitespace-nowrap text-faint">
            search providers · add straight to the archive
          </span>

          <div className="flex min-w-0 flex-none items-center gap-1.5 rounded-full border border-white/6 bg-white/3 p-1">
            {chubOn && (
              <ProviderChip
                on={provider === 'chub'}
                onClick={() =>
                  patch({ provider: 'chub', creator: '', creatorName: '' })
                }
              >
                Chub
              </ProviderChip>
            )}
            {datacatOn && (
              <ProviderChip
                on={provider === 'datacat'}
                onClick={() =>
                  patch({ provider: 'datacat', creator: '', creatorName: '' })
                }
              >
                DataCat
              </ProviderChip>
            )}
            <span className="mx-1 h-[18px] w-px bg-white/8" />
            <ProviderChip
              on={mode === 'browse'}
              onClick={() => patch({ mode: 'browse' as DiscoverMode })}
            >
              Discover
            </ProviderChip>
            <ProviderChip
              on={mode === 'following'}
              onClick={() => patch({ mode: 'following' as DiscoverMode })}
            >
              Following
            </ProviderChip>
            <span className="mx-1 h-[18px] w-px bg-white/8" />
            <ProviderChip
              on={state.hideHave}
              onClick={() => patch({ hideHave: !state.hideHave })}
            >
              Hide cards I have
            </ProviderChip>
            {/* Absent rather than inert outside Following: only a Following
                load is stamped, so there is nothing to call recent elsewhere. */}
            {recentFeed && (
              <>
                <span className="mx-1 h-[18px] w-px bg-white/8" />
                <ProviderChip
                  on={state.recentOnly}
                  onClick={() => patch({ recentOnly: !state.recentOnly })}
                  title="Only cards your Following feed first returned in the last 48 hours"
                >
                  Recent only
                </ProviderChip>
              </>
            )}
          </div>

          <DiscoverTagFilter
            provider={provider}
            auth={auth}
            value={tags}
            onChange={(next) => {
              const map = new Map<string, 'inc' | 'exc'>()
              for (const tag of next.include) map.set(tag, 'inc')
              for (const tag of next.exclude) map.set(tag, 'exc')
              patch({ tags: map })
            }}
          />

          <div className="flex-1" />

          <FollowingManager
            provider={provider}
            auth={auth}
            onBrowseCreator={(id, name) =>
              patch({ creator: id, creatorName: name })
            }
          />
          <DiscoverSort state={state} onChange={(sort) => patch({ sort })} />
        </div>
      </div>

      <div className="mx-auto max-w-[1560px] px-5">
        {creator && (
          <div className="mt-4 flex items-center gap-3 rounded-xl border border-sage-line bg-sage-dim px-4 py-2.5">
            <span className="text-[13.5px]">
              Browsing{' '}
              <b className="font-semibold text-sage">
                {creatorName || creator}
              </b>
              ’s cards
            </span>
            <button
              type="button"
              onClick={() => patch({ creator: '', creatorName: '' })}
              className="ml-auto flex items-center gap-1.5 rounded-full border border-line px-3 py-1 text-[12.5px] text-muted hover:border-white/20 hover:text-text"
            >
              <X className="size-3.5" /> Clear
            </button>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2.5 py-5">
          {/* Neither Following feed takes a search term, and a creator's
              catalogue is already the filter -- so the box is absent there
              rather than present and inert. */}
          {mode === 'browse' && !creator && (
            <div className="flex h-[38px] max-w-[520px] flex-1 items-center gap-2.5 rounded-[10px] border border-line bg-surface px-3">
              <Search className="size-4 shrink-0 text-faint" />
              <input
                value={rawSearch}
                onChange={(e) => setRawSearch(e.target.value)}
                placeholder={`Search ${provider === 'chub' ? 'Chub' : 'DataCat'}…`}
                className="w-full bg-transparent text-[13.5px] outline-none"
              />
            </div>
          )}
          <span className="text-[12.5px] whitespace-nowrap text-faint">
            {isPending
              ? mode === 'following'
                ? 'building your feed…'
                : 'searching…'
              : filtering
                ? // With a tag filter on, the provider's own total is not the
                  // number on screen -- report what actually matched.
                  `${visible.length} of ${items.length} loaded match${hiddenNote}`
                : total !== undefined
                  ? `${total.toLocaleString()} results${hiddenNote}${feedNote}`
                  : `${items.length} results shown${hiddenNote}${feedNote}`}
          </span>
          <div className="flex-1" />
          <button
            type="button"
            onClick={() => {
              // The whole-feed loads are cached for five minutes beneath the
              // grid's own query, so a bare refetch would re-slice the old
              // load. Marking them stale makes Refresh actually ask the
              // provider again -- which, on Following, is also what stamps
              // anything new.
              void client
                .invalidateQueries({
                  queryKey: ['following-feed'],
                  refetchType: 'none',
                })
                .then(() => refetch())
              // The archive may have changed out from under this session
              // (another tab, the userscript) -- resync rather than waiting on
              // its staleTime.
              void have.refetch()
            }}
            disabled={isFetching}
            className="flex h-[35px] items-center gap-2 rounded-full border border-line px-3.5 text-[13px] text-muted-foreground hover:border-white/20 hover:text-text disabled:opacity-60"
          >
            <RefreshCw className={cn('size-4', isFetching && 'animate-spin')} />
            Refresh
          </button>
        </div>

        {chubNeedsToken ? (
          <EmptyFollowing
            title="Chub’s Following needs your token"
            body="Chub serves the followed-authors feed only to a signed-in account. Paste your URQL token in Settings → Providers and it will appear here. Following and unfollowing stay on chub.ai."
          />
        ) : noFollows ? (
          <EmptyFollowing
            title="You’re not following anyone on DataCat yet"
            body="DataCat has no accounts, so this list lives in your archive’s own settings. Follow a creator from the Following button above, or from any card, and their cards show up here."
          />
        ) : (
          <>
            {error && (
              <p className="py-16 text-center text-bad">{error.message}</p>
            )}
            {seenFailed && (
              <p className="pt-2 text-center text-[12.5px] text-bad">
                Couldn’t record which cards are new, so “Recent only” has
                nothing to go on this load.
              </p>
            )}
            {!isPending && !error && visible.length === 0 && (
              <p className="py-16 text-center text-faint">
                {items.length === 0
                  ? recentOn
                    ? 'Nothing new in your feed in the last 48 hours.'
                    : 'No results.'
                  : filtering && tagMatched.length === 0
                    ? 'No result on the pages loaded so far carries those tags.'
                    : recentOn
                      ? 'Everything new in the last 48 hours is already in the archive.'
                      : 'Every result on this page is already in the archive.'}
              </p>
            )}
          </>
        )}

        <CardGrid className="pb-[90px]">
          {visible.map((item) => (
            <DiscoverTile
              key={item.key}
              item={item}
              search={gridSearch}
              have={isHave(item.providerId)}
              // Redundant with the filter on, so only badged with it off --
              // where it is the whole answer to "which of these are new?".
              recent={recentFeed && !state.recentOnly && isRecent(item, now)}
              adding={addingKey === item.key}
              onBrowseCreator={() =>
                patch({
                  creator: item.creatorId || item.creator,
                  creatorName: item.creator,
                })
              }
              onAdd={() => {
                setAddingKey(item.key)
                add.mutate(
                  { provider: item.provider, raw: item.raw },
                  {
                    onSuccess: (result) =>
                      toast(
                        result.duplicate
                          ? 'Already in the archive.'
                          : `Added ${result.card?.name ?? 'card'}.`,
                      ),
                    onError: (err) => toast(err.message, 'bad'),
                    onSettled: () => setAddingKey(null),
                  },
                )
              }}
            />
          ))}
        </CardGrid>

        <div ref={sentinelRef} className="h-px" />
        {isFetchingNextPage && (
          <p className="pb-16 text-center text-[12.5px] text-faint">
            loading more…
          </p>
        )}
      </div>
    </>
  )
}

/** The two "Following is empty, and here is why" states. Both are ordinary
 *  conditions rather than errors, so neither uses the error colour. */
function EmptyFollowing({ title, body }: { title: string; body: string }) {
  return (
    <div className="mx-auto max-w-[460px] py-20 text-center">
      <p className="text-[15px] font-medium text-text">{title}</p>
      <p className="mt-2 text-[13px] leading-[1.6] text-faint">{body}</p>
    </div>
  )
}

function ProviderChip({
  on,
  className,
  ...props
}: React.ComponentProps<'button'> & { on?: boolean }) {
  return (
    <button
      type="button"
      className={cn(
        'inline-flex h-[30px] flex-none items-center gap-[7px] rounded-full border border-transparent px-3.5 text-[13px] text-muted-foreground hover:bg-white/5 hover:text-text',
        on && 'border-sage-line bg-sage-dim text-sage hover:bg-sage-dim',
        className,
      )}
      {...props}
    />
  )
}
