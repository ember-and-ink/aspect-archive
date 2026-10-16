import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Link,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from 'react-router'
import { Download } from 'lucide-react'
import {
  useCharacterDetail,
  type CardDetail,
} from '@/hooks/use-character-detail'
import { useCharacters } from '@/hooks/use-characters'
import { readState } from '@/lib/browse'
import { sourceLabel, formatDate, formatBytes } from '@/lib/card'
import { isTypingTarget } from '@/lib/utils'
import {
  CardDetailLayout,
  DetailPager,
  Sep,
} from '@/components/detail/CardDetailLayout'
import {
  DialoguePane,
  ExpressionsPane,
  GalleryPane,
  InfoPane,
  GreetingsPane,
  LorebookPane,
  NotesPane,
  OverviewPane,
  RelatedPane,
} from '@/components/detail/panes'
import { EditProvider } from '@/components/detail/edit-context'
import { HeaderTags } from '@/components/detail/HeaderTags'
import { PortraitActions } from '@/components/detail/PortraitActions'
import { PANES, type Pane } from '@/components/detail/panes-def'

/**
 * The full-page card detail: a blurred hero, a sticky portrait column with its
 * actions, and the seven tabbed panes (docs/UI_REWRITE_PLAN.md §4.5).
 *
 * The active tab lives in `?tab=` so a pane is deep-linkable; every other query
 * param is the browse filter this card was opened from, carried in by the tile
 * link so prev/next can step through the same filtered set the grid showed.
 */
export function CharacterDetailPage() {
  const { id } = useParams<{ id: string }>()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const location = useLocation()
  // Set by the Fork action's navigate — "open the fork in edit mode with the
  // name focused" (docs/FORKS_AND_EXTRAS_PLAN.md §3). PortraitActions consumes
  // and clears it, so a later back/forward to this same history entry does not
  // reopen the dialog.
  const autoOpenRename = Boolean(
    (location.state as { openRename?: boolean } | null)?.openRename,
  )

  const detail = useCharacterDetail(id)
  // A local cache-bust bumped only when the avatar is replaced. The portrait URL
  // is stable across an avatar swap (same filename, new pixels), and the browser
  // would otherwise keep showing the old face; a field edit or a favourite must
  // not trigger it, which is why it is a deliberate counter, not the ETag.
  const [avatarBust, setAvatarBust] = useState(0)

  // The neighbour list: the same query the grid ran, read from the filter
  // params carried in on the URL. TanStack Query serves it from cache when the
  // user came from the grid, and fetches page 0 on a cold deep link.
  const browseState = readState(params)
  const neighbours = useCharacters(browseState)
  // Memoized so the go() callback's identity is stable between renders that did
  // not change the page — flatMap makes a new array every render otherwise.
  const cards = useMemo(
    () => neighbours.data?.pages.flatMap((page) => page.items) ?? [],
    [neighbours.data],
  )
  const position = cards.findIndex((card) => card.id === id)

  const activeTab = (params.get('tab') as Pane) ?? 'overview'
  const setTab = (tab: Pane) => {
    const next = new URLSearchParams(params)
    if (tab === 'overview') next.delete('tab')
    else next.set('tab', tab)
    setParams(next, { replace: true })
  }

  const go = useCallback(
    (delta: number) => {
      if (position < 0) return
      const target = cards[position + delta]
      if (target) {
        const next = new URLSearchParams(params)
        next.delete('tab')
        navigate(
          {
            pathname: `/characters/${encodeURIComponent(target.id)}`,
            search: next.toString(),
          },
          { replace: true },
        )
      } else if (delta > 0 && neighbours.hasNextPage) {
        // The neighbour is on a page not yet fetched — pull it, then the effect
        // below runs again with a longer list.
        void neighbours.fetchNextPage()
      }
    },
    [position, cards, params, navigate, neighbours],
  )

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // J/K are bare letters, so they collide with typing. Stage 3 put
      // textareas and tag inputs on this page, and without this guard a
      // description containing "j" navigated to the next card mid-edit and
      // took the unsaved draft with it.
      if (isTypingTarget(event.target)) return
      // A shortcut, not a modified chord: ⌘K opens search, and Alt/Ctrl+K are
      // the terminal-style bindings a browser or OS may already own.
      if (event.metaKey || event.ctrlKey || event.altKey) return
      if (event.key === 'j') go(1)
      else if (event.key === 'k') go(-1)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [go])

  if (detail.isPending)
    return <p className="py-24 text-center text-faint">reading card…</p>
  if (detail.error)
    return <p className="py-24 text-center text-bad">{detail.error.message}</p>

  const { card, etag } = detail.data
  // The portrait column renders at up to 280 CSS px wide (2:3, so 420 tall);
  // the inherited avatar cache is a fixed 96x144 sized for the grid tile, and
  // stretching that into this much bigger box is what made the portrait look
  // blurry. Ask for a size that stays sharp at typical device pixel ratios.
  const thumb = `${card.thumb_url}?size=840${avatarBust ? `&v=${avatarBust}` : ''}`
  const backSearch = new URLSearchParams(params)
  backSearch.delete('tab')

  return (
    // Wraps the whole layout, not just the panes: the rename dialog in the
    // portrait column and the tag editor in the header both write through this
    // context too, so it has to sit above all three slots.
    <EditProvider id={card.id} data={card.card} etag={etag}>
      <CardDetailLayout
        heroImage={thumb}
        back={{ pathname: '/', search: backSearch.toString() }}
        backLabel="Characters"
        pager={
          <DetailPager
            position={position >= 0 ? position + 1 : null}
            total={neighbours.data?.pages[0]?.total ?? cards.length}
            onPrev={() => go(-1)}
            onNext={() => go(1)}
            prevDisabled={position <= 0}
            nextDisabled={
              position < 0 ||
              (position >= cards.length - 1 && !neighbours.hasNextPage)
            }
          />
        }
        portrait={
          <>
            <img
              src={thumb}
              alt={card.name}
              className="aspect-[2/3] w-full rounded-[15px] border border-line object-cover shadow-[0_24px_60px_#00000080]"
            />
            <a
              href={card.png_url}
              className="flex h-[38px] items-center justify-center gap-2 rounded-[10px] border border-sage bg-sage text-[13.5px] font-semibold text-on-sage hover:bg-[#68d0b1]"
              download
            >
              <Download className="size-4" /> Download card
            </a>
            <PortraitActions
              card={card}
              etag={etag}
              onAvatarReplaced={() => setAvatarBust((n) => n + 1)}
              autoOpenRename={autoOpenRename}
            />
          </>
        }
        title={card.name}
        meta={
          <>
            by{' '}
            {card.creator ? (
              // The way to answer "what else has this person made". The Creator
              // pill can do it too, but only by scrolling 192 names to find the
              // one already on screen.
              <Link
                // `/`, not `/characters` — browse is the index route and
                // `/characters/:id` is *this* page, so the tidier-looking path
                // falls through to the catch-all and lands on nothing.
                to={`/?creator=${encodeURIComponent(card.creator)}`}
                className="text-sage hover:underline"
                title={`Browse cards by ${card.creator}`}
              >
                {card.creator}
              </Link>
            ) : (
              <span className="text-sage">unknown</span>
            )}
            <Sep />{' '}
            <span className="font-mono">{formatDate(card.create_date)}</span>
            <Sep /> {sourceLabel(card.source_kind)}
            <Sep /> <span className="font-mono">{formatBytes(card.size)}</span>
            {card.is_fork && (
              <>
                <Sep />{' '}
                {card.forked_from ? (
                  <>
                    fork of{' '}
                    <Link
                      to={`/characters/${encodeURIComponent(card.forked_from.id)}`}
                      className="text-sage hover:underline"
                    >
                      {card.forked_from.name}
                    </Link>
                  </>
                ) : (
                  <span className="text-faint italic">
                    original no longer in archive
                  </span>
                )}
              </>
            )}
          </>
        }
        tags={<HeaderTags card={card} />}
        panes={PANES}
        activeTab={activeTab}
        onTabChange={setTab}
        tabCount={(pane) => paneCount(pane, card)}
      >
        {activeTab === 'overview' && <OverviewPane card={card} />}
        {activeTab === 'notes' && <NotesPane card={card} />}
        {activeTab === 'greetings' && <GreetingsPane card={card} />}
        {activeTab === 'dialogue' && <DialoguePane card={card} />}
        {activeTab === 'lore' && <LorebookPane card={card} />}
        {activeTab === 'gallery' && <GalleryPane card={card} />}
        {activeTab === 'expressions' && <ExpressionsPane card={card} />}
        {activeTab === 'related' && <RelatedPane card={card} />}
        {activeTab === 'info' && (
          <InfoPane
            card={card}
            etag={etag}
            onAvatarReplaced={() => setAvatarBust((n) => n + 1)}
          />
        )}
      </CardDetailLayout>
    </EditProvider>
  )
}

/** The small number beside the Greetings, Dialogue and Lorebook tabs. */
function paneCount(pane: Pane, card: CardDetail): number | undefined {
  if (pane === 'greetings') return card.greetings
  if (pane === 'dialogue') return card.has_example_dialogue ? 1 : undefined
  if (pane === 'lore') return card.lore_entries
  return undefined
}
