import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { ExternalLink, Plus, TriangleAlert } from 'lucide-react'
import {
  CardDetailLayout,
  DetailPager,
  Sep,
} from '@/components/detail/CardDetailLayout'
import { EditProvider } from '@/components/detail/edit-context'
import {
  GreetingsPane,
  LorebookPane,
  NotesPane,
  OverviewPane,
} from '@/components/detail/panes'
import type { Pane } from '@/components/detail/panes-def'
import {
  idFragment,
  useAddToArchive,
  useDatacatFollows,
  useDiscoverSearch,
  useHaveFragments,
  type DiscoverItem,
} from '@/hooks/use-discover'
import {
  captureProviderCard,
  useDiscoverPreview,
  type ProviderCapture,
} from '@/hooks/use-discover-preview'
import {
  datacatAvatarUrl,
  isDatacatDegraded,
  type DatacatCharacter,
} from '@/lib/providers/datacat'
import { useProviderSettings } from '@/hooks/use-settings'
import { readDiscoverState, writeDiscoverState } from '@/lib/discover-state'
import { formatDate } from '@/lib/card'
import { toast } from '@/lib/toast'
import { cn, isTypingTarget } from '@/lib/utils'

/**
 * The panes a card that is not in the archive can honestly fill.
 *
 * Gallery, Related and Info are all *about* being in the archive — downloaded
 * media, neighbours by creator and tag, filename and size on disk — so they are
 * absent rather than present and empty. Everything else is the archive's own
 * detail view, unchanged: same layout, same four panes, same components.
 */
const PREVIEW_PANES: readonly Pane[] = [
  'overview',
  'notes',
  'greetings',
  'lore',
]

const PROVIDER_LABEL = { chub: 'Chub', datacat: 'DataCat' } as const

/**
 * Read a provider card before deciding to keep it.
 *
 * `web/` did this in a modal and the mock does it by opening the card; either
 * way the point is the same — you cannot judge a card from a thumbnail. The
 * shipped Discover offered neither, which is what made "Get" a coin flip.
 *
 * The card is mapped **server-side** by the same code that writes it
 * (`POST /api/v1/discover/preview`), so what is on screen is what lands in the
 * archive. The row it started from comes out of the Discover query the tile
 * linked from, which is also what prev/next steps through.
 */
export function DiscoverPreviewPage() {
  const { provider, id } = useParams<{
    provider: 'chub' | 'datacat'
    id: string
  }>()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const { chubToken, chubNsfw } = useProviderSettings()

  const state = readDiscoverState(params)
  const auth = useMemo(
    () => ({ token: chubToken, nsfw: chubNsfw }),
    [chubToken, chubNsfw],
  )
  const { creators: datacatFollows } = useDatacatFollows()

  // The same query the grid ran, read back from the URL the tile carried in.
  // Served from cache when you came from the grid; re-run on a cold deep link.
  const feed = useDiscoverSearch(state, { auth, followed: datacatFollows })
  const items = useMemo(
    () => feed.data?.pages.flatMap((page) => page.items) ?? [],
    [feed.data],
  )
  const position = items.findIndex((item) => item.providerId === id)
  const item: DiscoverItem | undefined = items[position]

  // The provider fetches the preview needs (full node / detail + lorebook), run
  // once and reused by the add, so keeping a card you just read costs nothing.
  //
  // Stamped with the id it was captured for. Without that, stepping to the next
  // card left the previous capture in state for a render, and the preview query
  // — keyed on the *new* id — fetched it: card 2's URL and pager, card 1's
  // description. Binding the two makes that state unrepresentable.
  const [held, setHeld] = useState<{ id: string; capture: ProviderCapture }>()
  const [captureError, setCaptureError] = useState<{
    id: string
    message: string
  }>()
  const capture = held?.id === id ? held.capture : undefined
  const error = captureError?.id === id ? captureError.message : null

  useEffect(() => {
    if (!item || !provider || !id) return
    let live = true
    setCaptureError(undefined)
    captureProviderCard(provider, item.raw, auth)
      .then((result) => live && setHeld({ id, capture: result }))
      .catch(
        (failure: Error) =>
          live && setCaptureError({ id, message: failure.message }),
      )
    return () => {
      live = false
    }
  }, [item, provider, id, auth])

  const preview = useDiscoverPreview(provider ?? 'chub', id, capture)
  const have = useHaveFragments()
  const inArchive = Boolean(id && have.data?.has(idFragment(id)))

  const add = useAddToArchive()
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
      const target = items[position + delta]
      if (target) {
        const next = new URLSearchParams(params)
        next.delete('tab')
        navigate(
          {
            pathname: `/discover/${target.provider}/${encodeURIComponent(target.providerId)}`,
            search: next.toString(),
          },
          { replace: true },
        )
      } else if (delta > 0 && feed.hasNextPage) {
        void feed.fetchNextPage()
      }
    },
    [position, items, params, navigate, feed],
  )

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isTypingTarget(event.target)) return
      if (event.metaKey || event.ctrlKey || event.altKey) return
      if (event.key === 'j') go(1)
      else if (event.key === 'k') go(-1)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [go])

  const back = {
    pathname: '/discover',
    search: writeDiscoverState(state).toString(),
  }

  if (!provider || !id) return <Problem>That is not a provider card.</Problem>
  if (feed.isPending && !item) return <Fallback>finding the card…</Fallback>
  if (!item)
    return (
      <Problem>
        This card is not in the results that were loaded. Go{' '}
        <Link to={back} className="text-sage underline">
          back to Discover
        </Link>{' '}
        and open it from the grid.
      </Problem>
    )

  // From here on there is always something to show: `item` came out of the
  // grid's own (already-fetched) query, so name/creator/tags/avatar are known
  // immediately. The shell renders from that on every render; only the panes
  // — which need the server-mapped `card` — wait on `preview`. This is the
  // same split the old modal made: paint the header from the row you clicked,
  // stream the body in behind it.
  const card = preview.data
  // Chub's `avatar_url` is already CDN-sized; DataCat's raw janitorai
  // original needs the same width hint the old modal used (600px) so the
  // hero doesn't decode a full-res image before `card.avatar_url` — the
  // provider's own choice, made after the real fetch — is ready.
  const previewAvatar =
    provider === 'datacat'
      ? datacatAvatarUrl(item.raw as DatacatCharacter, { width: 600 })
      : item.avatarUrl
  // The feed row says so up front; the detail read is a backstop in case a
  // feed shape ever stops carrying the badge.
  const degraded =
    item.degraded ||
    (capture?.provider === 'datacat' && isDatacatDegraded(capture.character))
  const hero = card?.avatar_url || previewAvatar || item.avatarUrl
  const name = card?.name ?? item.name
  const tags = card?.tags ?? item.tags

  return (
    <CardDetailLayout
      heroImage={hero}
      back={back}
      backLabel={`Discover · ${PROVIDER_LABEL[provider]}`}
      pager={
        <DetailPager
          position={position >= 0 ? position + 1 : null}
          total={
            feed.data?.pages[feed.data.pages.length - 1]?.total ?? items.length
          }
          onPrev={() => go(-1)}
          onNext={() => go(1)}
          prevDisabled={position <= 0}
          nextDisabled={
            position < 0 || (position >= items.length - 1 && !feed.hasNextPage)
          }
        />
      }
      portrait={
        <>
          {hero ? (
            <img
              src={hero}
              alt={name}
              className="aspect-[2/3] w-full rounded-[15px] border border-line object-cover shadow-[0_24px_60px_#00000080]"
            />
          ) : (
            <div className="aspect-[2/3] w-full rounded-[15px] border border-line bg-raised" />
          )}
          {degraded && (
            <div className="rounded-[10px] border border-warn/60 bg-surface px-3 py-2.5 text-[12px] leading-[1.5] text-faint">
              <b className="flex items-center gap-1.5 font-semibold text-warn">
                <TriangleAlert className="size-3.5" /> Incomplete on DataCat
              </b>
              <p className="mt-1">
                DataCat marks this card degraded: it has the greeting but no
                definition (description, personality, scenario, examples). The
                creator usually blocks proxies on these, so the definition can't
                be captured anywhere. Adding it archives the empty card, and an
                archived card is never overwritten.
              </p>
            </div>
          )}
          <button
            type="button"
            disabled={add.isPending}
            onClick={() =>
              add.mutate(
                { provider, raw: item.raw, capture },
                {
                  onSuccess: (result) =>
                    toast(
                      result.duplicate
                        ? 'Already in the archive.'
                        : `Added ${result.card?.name ?? name}.`,
                    ),
                  onError: (error) => toast(error.message, 'bad'),
                },
              )
            }
            className={cn(
              'flex h-[38px] items-center justify-center gap-2 rounded-[10px] border text-[13.5px] font-semibold disabled:opacity-60',
              degraded
                ? 'border-warn/60 text-warn hover:bg-raised'
                : inArchive
                  ? 'border-line text-muted-foreground hover:bg-raised'
                  : 'border-sage bg-sage text-on-sage hover:bg-[#68d0b1]',
            )}
          >
            <Plus className="size-4" />
            {add.isPending
              ? 'Adding…'
              : degraded
                ? 'Add anyway'
                : inArchive
                  ? 'Add again'
                  : 'Add to archive'}
          </button>
          {card?.source_url && (
            <a
              href={card.source_url}
              target="_blank"
              rel="noreferrer"
              className="flex h-[34px] items-center justify-center gap-2 rounded-[10px] border border-line text-[13px] text-muted-foreground hover:border-white/20 hover:text-text"
            >
              <ExternalLink className="size-3.5" /> Open on{' '}
              {PROVIDER_LABEL[provider]}
            </a>
          )}
          {inArchive && (
            <p className="px-1 text-center text-[12px] leading-[1.5] text-sage">
              Already in your archive.
            </p>
          )}
          {card && card.warnings.length > 0 && (
            <div className="rounded-[10px] border border-line-soft bg-surface px-3 py-2.5 text-[12px] leading-[1.5] text-faint">
              <b className="block font-semibold text-text">
                {card.warnings.length} thing
                {card.warnings.length === 1 ? '' : 's'} to know
              </b>
              <ul className="mt-1 list-disc pl-4">
                {card.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </div>
          )}
        </>
      }
      title={name}
      meta={
        <>
          by{' '}
          <span className="text-sage">
            {card?.creator || item.creator || 'unknown'}
          </span>
          {card?.create_date && (
            <>
              <Sep />{' '}
              <span className="font-mono">{formatDate(card.create_date)}</span>
            </>
          )}
          <Sep /> {PROVIDER_LABEL[provider]}
          {card && (
            <>
              <Sep />{' '}
              <span className="font-mono">
                ~{Math.round(card.prompt_chars / 4).toLocaleString()} tokens
              </span>
            </>
          )}
        </>
      }
      tags={
        tags.length > 0 ? (
          <div className="mt-3.5 flex flex-wrap gap-1.5">
            {tags.map((tag) => (
              <span
                key={tag}
                className="rounded-full border border-line bg-surface px-2.5 py-1 text-[12px] text-muted-foreground"
              >
                {tag}
              </span>
            ))}
          </div>
        ) : undefined
      }
      panes={PREVIEW_PANES}
      activeTab={PREVIEW_PANES.includes(activeTab) ? activeTab : 'overview'}
      onTabChange={setTab}
      tabCount={(pane) =>
        card
          ? pane === 'greetings'
            ? card.greetings
            : pane === 'lore'
              ? card.lore_entries
              : undefined
          : undefined
      }
    >
      {error ? (
        <Problem>{error}</Problem>
      ) : preview.error ? (
        <Problem>{preview.error.message}</Problem>
      ) : !card ? (
        <Fallback>reading the card…</Fallback>
      ) : (
        // Read-only: these panes are the archive's editors, and there is
        // nothing here to write to yet. `EditProvider` still wraps them
        // because they read the context unconditionally; `readOnly` is what
        // removes every Edit button.
        <EditProvider id={id} data={card.card} etag={null} readOnly>
          {activeTab === 'notes' ? (
            <NotesPane card={card} />
          ) : activeTab === 'greetings' ? (
            <GreetingsPane card={card} />
          ) : activeTab === 'lore' ? (
            <LorebookPane card={card} />
          ) : (
            <OverviewPane card={card} />
          )}
        </EditProvider>
      )}
    </CardDetailLayout>
  )
}

function Fallback({ children }: { children: React.ReactNode }) {
  return <p className="py-24 text-center text-faint">{children}</p>
}

function Problem({ children }: { children: React.ReactNode }) {
  return <p className="py-24 text-center text-bad">{children}</p>
}
