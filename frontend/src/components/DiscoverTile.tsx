import { useState } from 'react'
import { Link } from 'react-router'
import { TriangleAlert } from 'lucide-react'
import type { DiscoverItem } from '@/hooks/use-discover'
import { cn } from '@/lib/utils'

/**
 * One provider search result.
 *
 * A link, like `CardTile` — the mock makes every card open the card, Discover's
 * included (`d-archive.html:882`), and `web/` opened a full preview modal on
 * click. The first version of this was neither: a thumbnail with a hover
 * button, so there was no way to read a card before deciding to keep it.
 *
 * `Get` and the creator name sit inside the link and stop the click from
 * reaching it, so the tile has three targets without three overlapping regions.
 */
export function DiscoverTile({
  item,
  have,
  recent = false,
  onAdd,
  onBrowseCreator,
  adding,
  search,
}: {
  item: DiscoverItem
  have: boolean
  /** First returned by Following inside the grace window. Badged only with
   *  "Recent only" off — with it on, every tile is new and the badge says
   *  nothing. `have` wins the corner: a card you already own is not news. */
  recent?: boolean
  onAdd: () => void
  onBrowseCreator: () => void
  adding: boolean
  /** Discover's query string, carried onto the link so the preview can rebuild
   *  this grid for its prev/next — the same trick `CardTile` uses. */
  search: string
}) {
  // Provider avatars arrive at wildly different sizes and connection speeds,
  // and popping in fully-decoded reads as the grid "snapping" mid-scroll. A
  // fade removes the pop without waiting on anything extra to load.
  const [loaded, setLoaded] = useState(false)
  return (
    <Link
      to={{
        pathname: `/discover/${item.provider}/${encodeURIComponent(item.providerId)}`,
        search,
      }}
      className="group block"
    >
      <div className="relative aspect-[2/3] w-full overflow-hidden rounded-xl border border-line-soft bg-raised group-hover:border-sage-line">
        {have ? (
          <span className="absolute top-[7px] right-[7px] z-2 rounded-[6px] border border-sage-line bg-ground/85 px-1.5 py-px text-[10px] font-bold tracking-[0.05em] text-sage uppercase">
            Have
          </span>
        ) : (
          recent && (
            <span className="absolute top-[7px] right-[7px] z-2 rounded-[6px] border border-sage bg-sage px-1.5 py-px text-[10px] font-bold tracking-[0.05em] text-on-sage uppercase">
              New
            </span>
          )
        )}
        {item.degraded && (
          // Left corner, so it never competes with Have/New on the right: a
          // card can be both owned and incomplete, and both facts matter.
          <span
            title="Incomplete on DataCat: no definition, only the greeting. Capture it from JanitorAI with the userscript instead."
            aria-label="Incomplete card"
            className="absolute top-[7px] left-[7px] z-2 flex items-center gap-1 rounded-[6px] border border-warn/60 bg-ground/85 px-1.5 py-px text-[10px] font-bold tracking-[0.05em] text-warn uppercase"
          >
            <TriangleAlert className="size-3" />
            Partial
          </span>
        )}
        {item.avatarUrl ? (
          <img
            src={item.avatarUrl}
            alt=""
            loading="lazy"
            onLoad={() => setLoaded(true)}
            className={cn(
              'size-full object-cover transition-[opacity,transform] duration-[450ms] ease-[cubic-bezier(.2,.7,.3,1)] group-hover:scale-105',
              loaded ? 'opacity-100' : 'opacity-0',
            )}
          />
        ) : (
          <div className="size-full bg-raised" />
        )}
        <span className="pointer-events-none absolute inset-0 shadow-[inset_0_-54px_44px_-30px_#0f1113de]" />
        <button
          type="button"
          onClick={(event) => {
            event.preventDefault()
            event.stopPropagation()
            onAdd()
          }}
          disabled={adding}
          className={cn(
            'absolute inset-x-[7px] bottom-[7px] z-3 hidden h-7 items-center justify-center rounded-lg text-[12px] font-semibold group-hover:flex disabled:opacity-60',
            item.degraded
              ? 'border border-warn/60 bg-ground/90 text-warn'
              : 'bg-sage text-on-sage',
          )}
        >
          {adding
            ? 'Adding…'
            : item.degraded
              ? 'Get anyway'
              : have
                ? 'Add again'
                : 'Get'}
        </button>
      </div>
      <h3 className="mt-2 line-clamp-2 text-[13.5px] leading-[1.3] font-medium">
        {item.name}
      </h3>
      <button
        type="button"
        onClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
          onBrowseCreator()
        }}
        className="mt-px block max-w-full truncate text-left text-[11.5px] text-faint hover:text-sage hover:underline"
      >
        {item.creator || 'unknown'}
      </button>
    </Link>
  )
}
