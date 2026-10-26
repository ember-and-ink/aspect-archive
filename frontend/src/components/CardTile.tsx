import { Link, useLocation } from 'react-router'
import { Check, GitFork, Star } from 'lucide-react'
import { tileName, type Card } from '@/lib/browse'
import { cn } from '@/lib/utils'

/**
 * The height of the name + creator block under the art, in px: an 8px gap, two
 * lines of name at 13.5/1.3, and the creator line. Fixed rather than
 * content-sized so every grid row is the same height and the virtualized grid
 * (`VirtualCardGrid`) can compute row offsets instead of measuring them. A
 * one-line name leaves the space blank; a longer one already clamps to two.
 */
export const TILE_TEXT_HEIGHT = 62

/**
 * One card in the grid: portrait, name, creator, and the two badges the mock
 * puts on the art — lore-entry count (or greeting count, when there is no
 * lorebook) and the star.
 *
 * The whole tile is a link, so middle-click and ⌘-click open a card in a new
 * tab. Deep links are the point of the SPA fallback; a div with an onClick
 * would throw that away for every card in the archive. Batch mode is the one
 * exception — the click is redirected to a selection toggle instead of
 * navigating, and the lore/greeting badge shifts right to make room for the
 * selection square in the corner it would otherwise share.
 */
export function CardTile({
  card,
  isNew,
  search,
  className,
  batchMode = false,
  selected = false,
  showListingName = false,
  onToggleSelect,
  onClick,
  ...props
}: Omit<React.ComponentProps<typeof Link>, 'to'> & {
  card: Card
  isNew?: boolean
  /** The browse querystring to carry onto the link, so the detail page can
   *  rebuild the set this tile belonged to for prev/next. Defaults to the
   *  current URL's — surfaces with their own ordering (the recently-added
   *  shelf) pass theirs, which the page's filters would not otherwise capture. */
  search?: string
  /** Batch-select mode — see `use-batch-selection`. */
  batchMode?: boolean
  selected?: boolean
  /** Title the tile with the source page's listing name (the tagline) rather
   *  than the character's name — the Characters page's heading toggle. Cards
   *  with no listing name keep their name. */
  showListingName?: boolean
  onToggleSelect?: (id: string) => void
}) {
  const location = useLocation()
  const linkSearch = search ?? location.search
  const title = tileName(card, showListingName)
  const badge = card.lore_entries
    ? `${card.lore_entries} lore`
    : card.greetings > 1
      ? `${card.greetings} greetings`
      : null

  return (
    <Link
      to={{
        pathname: `/characters/${encodeURIComponent(card.id)}`,
        search: linkSearch,
      }}
      className={cn(
        'group block',
        selected && 'rounded-xl ring-2 ring-sage',
        className,
      )}
      onClick={(event) => {
        if (batchMode) {
          event.preventDefault()
          onToggleSelect?.(card.id)
          return
        }
        onClick?.(event)
      }}
      {...props}
    >
      <div className="relative aspect-[2/3] w-full overflow-hidden rounded-xl border border-line-soft bg-raised group-hover:border-sage-line">
        {batchMode && (
          <span
            className={cn(
              'absolute top-1.5 left-1.5 z-3 grid size-6 place-items-center rounded-[7px] border',
              selected
                ? 'border-sage bg-sage text-on-sage'
                : 'border-white/30 bg-ground/85 text-transparent',
            )}
          >
            <Check className="size-3.5" />
          </span>
        )}
        {badge && (
          <span
            className={cn(
              'absolute top-[7px] z-2 rounded-[7px] border border-white/12 bg-ground/85 px-[7px] py-0.5 text-[10.5px]',
              batchMode ? 'left-[38px]' : 'left-[7px]',
              card.lore_entries ? 'border-sage-line text-sage' : 'text-text',
            )}
          >
            {badge}
          </span>
        )}
        {card.favorite && (
          <span className="absolute top-1.5 right-1.5 z-2 grid size-6 place-items-center rounded-[7px] bg-ground/85 text-sage">
            <Star className="size-3.5 fill-current" />
          </span>
        )}
        {isNew && (
          <span className="absolute bottom-[7px] left-[7px] z-2 rounded-md bg-sage px-1.5 py-px text-[10px] font-bold tracking-[0.08em] text-on-sage uppercase">
            new
          </span>
        )}
        {card.is_fork && (
          <span
            title="Fork"
            className="absolute right-1.5 bottom-1.5 z-2 grid size-6 place-items-center rounded-[7px] bg-ground/85 text-faint"
          >
            <GitFork className="size-3.5" />
          </span>
        )}
        <img
          // The grid track's min width is 158px (CardGrid) and tiles routinely
          // render wider than that as auto-fill stretches the last column, so
          // the inherited 96x144 avatar cache -- built for a much smaller old-UI
          // tile -- upscales visibly. 560 is roughly 2x a typical rendered
          // height, the same retina-margin CharacterDetailPage uses for the
          // portrait.
          src={`${card.thumb_url}?size=560`}
          alt=""
          loading="lazy"
          className="size-full object-cover transition-transform duration-[450ms] ease-[cubic-bezier(.2,.7,.3,1)] group-hover:scale-105"
        />
        {/* The gradient the mock lays over the bottom of every tile, so a
            light-coloured portrait does not fight the name beneath it. */}
        <span className="pointer-events-none absolute inset-0 shadow-[inset_0_-54px_44px_-30px_#0f1113de]" />
      </div>
      <div className="overflow-hidden" style={{ height: TILE_TEXT_HEIGHT }}>
        <h3
          // Listing names run long and routinely clamp to their two lines, so
          // the full one is reachable on hover; a bare character name never
          // needs the tooltip.
          title={showListingName ? title : undefined}
          className="mt-2 line-clamp-2 text-[13.5px] leading-[1.3] font-medium"
        >
          {title}
        </h3>
        <div className="mt-px truncate text-[11.5px] leading-[1.5] text-faint">
          {card.creator || 'unknown'}
        </div>
      </div>
    </Link>
  )
}
