import { useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router'
import { ArrowLeftRight } from 'lucide-react'
import { ChipStrip } from '@/components/ChipStrip'
import { CreatorPill, SourcePill, TagsPill } from '@/components/FilterPills'
import { CardGrid } from '@/components/CardGrid'
import { CardTile } from '@/components/CardTile'
import { RecentShelf } from '@/components/RecentShelf'
import { VirtualCardGrid } from '@/components/VirtualCardGrid'
import { SortPopover } from '@/components/SortPopover'
import { useBatchSelection } from '@/hooks/use-batch-selection'
import { useGridColumns } from '@/hooks/use-grid-columns'
import { useScrollRestore } from '@/hooks/use-scroll-restore'
import { useArchiveCount, useCharacters } from '@/hooks/use-characters'
import { useSettings, useUpdateUi2 } from '@/hooks/use-settings'
import { cn } from '@/lib/utils'
import {
  isFiltered,
  readState,
  tileSearch,
  writeState,
  type BrowseState,
} from '@/lib/browse'

/**
 * Browse the archive: the chip strip, the sort control, the recently-added
 * shelf and the grid.
 *
 * `/favorites` is this page with one filter pinned on, rather than a page of
 * its own — the mock treats it as a tab, and everything else about it (chips,
 * sort, shelf suppression) is identical.
 */
export function CharactersPage({ favorites = false }: { favorites?: boolean }) {
  const [params, setParams] = useSearchParams()
  const gridRef = useRef<HTMLDivElement>(null)
  const columns = useGridColumns(gridRef)
  const settings = useSettings()
  const updateUi2 = useUpdateUi2()
  const batch = useBatchSelection()

  const urlState = readState(params)
  // Settings §3.7's "default sort" only applies when the URL is silent about
  // it -- an explicit `?sort=` (a bookmark, the shelf's "See all") always
  // wins, exactly like `?tab=` beats nothing on the detail page.
  const defaultSort = settings.data?.ui2?.defaultSort ?? 'name'
  const state: BrowseState = {
    ...(favorites
      ? { ...urlState, flags: new Set(urlState.flags).add('fav') }
      : urlState),
    sort: !params.has('sort') && defaultSort ? defaultSort : urlState.sort,
  }
  // The shelf's own Hide button and the Settings toggle write the same key, so
  // either one turns it off for good rather than just this session (the gap
  // Stage 1 left open). Undefined (nothing saved yet, or still loading) reads
  // as shown -- the pre-Stage-6 default.
  const shelfHidden = settings.data?.ui2?.showRecentShelf === false
  // Name/tagline mode. Stored rather than kept in the URL: it changes how the
  // same set of cards reads, not which cards they are, so a shared link should
  // carry the reader's own preference rather than the sender's.
  const listingNames = settings.data?.ui2?.showListingNames === true

  const setState = (next: BrowseState) => {
    // Replace rather than push: filtering is a running adjustment, and pushing
    // every chip click would make Back walk through each of them one at a time.
    // `defaultSort` goes in so that picking the sort the user has *stored* as
    // their default clears `sort=` from the URL, while picking any other one --
    // including plain "Name" -- writes it down and actually takes effect.
    setParams(writeState(next, defaultSort), { replace: true })
  }

  const archiveCount = useArchiveCount()
  const characters = useCharacters(state)
  const {
    data,
    error,
    isPending,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isFetching,
  } = characters

  const cards = data?.pages.flatMap((page) => page.items) ?? []
  const total = data?.pages[0]?.total ?? 0

  // Infinite scroll: a sentinel below the grid asks for the next page as it
  // comes into view. Kept a screen ahead with rootMargin so the grid does not
  // visibly stop at the bottom of each page.
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
  }, [hasNextPage, fetchNextPage, cards.length])

  useScrollRestore({
    contentRef: gridRef,
    loaded: cards.length,
    hasNextPage,
    isFetching,
    fetchNextPage,
    resetKey: tileSearch(state),
  })

  const showShelf = !shelfHidden && !favorites && !isFiltered(state)

  return (
    <>
      <div className="sticky top-0 z-20 border-b border-line-soft bg-ground">
        <div className="mx-auto flex max-w-[1560px] items-center gap-3.5 px-5 py-3">
          {/* The heading is the toggle: click it to retitle every tile with
              its listing name (the tagline) and back. There is no room on this
              row for a control of its own, and the word it swaps *is* the
              state -- "Characters" or "Taglines". Which tab this is stays
              legible from the top bar and, on /favorites, the pinned chip. */}
          <h1 className="text-[19px] font-semibold tracking-[-0.015em] whitespace-nowrap">
            <button
              type="button"
              onClick={() =>
                updateUi2.mutate({
                  key: 'showListingNames',
                  value: !listingNames,
                })
              }
              title={
                listingNames
                  ? 'Showing listing names — click for character names'
                  : 'Showing character names — click for listing names'
              }
              className="group flex items-center gap-1.5 hover:text-sage"
            >
              {listingNames
                ? 'Taglines'
                : favorites
                  ? 'Favorites'
                  : 'Characters'}
              <ArrowLeftRight
                className={cn(
                  'size-3.5 transition-opacity',
                  listingNames
                    ? 'text-sage opacity-100'
                    : 'opacity-0 group-hover:opacity-70',
                )}
              />
            </button>
          </h1>
          <span className="text-[12.5px] whitespace-nowrap text-faint">
            {isPending
              ? 'reading…'
              : `${total.toLocaleString()}${
                  archiveCount.data
                    ? ` of ${archiveCount.data.cards.toLocaleString()}`
                    : ''
                }`}
          </span>
          {/* The three long-list filters, then the fixed flag strip. Ordered so
              that the controls whose contents vary sit together and the strip,
              which never changes width, anchors the row. */}
          <TagsPill state={state} onChange={setState} />
          <CreatorPill state={state} onChange={setState} />
          <SourcePill state={state} onChange={setState} />
          <ChipStrip
            state={state}
            onChange={setState}
            pinned={favorites ? ['fav'] : []}
          />
          <div className="flex-1" />
          <SortPopover
            sort={state.sort}
            onChange={(sort) => setState({ ...state, sort })}
          />
        </div>
      </div>

      <div className="mx-auto max-w-[1560px] px-5">
        {showShelf && (
          <RecentShelf
            columns={columns}
            showListingNames={listingNames}
            onHide={() =>
              updateUi2.mutate({ key: 'showRecentShelf', value: false })
            }
          />
        )}

        {error && <p className="py-16 text-center text-bad">{error.message}</p>}
        {!isPending && !error && cards.length === 0 && (
          <p className="py-16 text-center text-faint">
            Nothing matches those filters.
          </p>
        )}

        {/* An empty CardGrid as a ruler: `useGridColumns` reads the track
            count off it (and the shelf sizes itself from that), so the grid's
            responsive column rule still lives only in CardGrid. The cards
            themselves render through the virtualizer. */}
        <CardGrid ref={gridRef} aria-hidden className="h-0" />
        <div className="pt-[22px] pb-[90px]">
          <VirtualCardGrid
            items={cards}
            columns={columns}
            getKey={(card) => card.id}
            renderItem={(card) => (
              <CardTile
                card={card}
                search={tileSearch(state)}
                showListingName={listingNames}
                batchMode={batch.active}
                selected={batch.selected.has(card.id)}
                onToggleSelect={batch.toggleSelected}
              />
            )}
          />
        </div>

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
