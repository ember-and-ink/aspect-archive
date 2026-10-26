import { Link } from 'react-router'
import { ArrowLeft, ChevronLeft, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { TAB_LABELS, type Pane } from './panes-def'

/**
 * How a card is read, wherever it came from.
 *
 * This is the layout the detail page spent a long time getting right — the
 * blurred hero, the sticky portrait column, the serif title over its meta row,
 * the tab bar. Discover's preview shows a card that is not in the archive yet
 * and needs exactly the same reading experience, so the shell lives here and
 * both pages call it rather than one growing a second, worse copy.
 *
 * Everything that differs between the two is a slot: the portrait's image and
 * the actions under it, the meta row, which tabs exist, and where Back goes.
 * Nothing here knows about the archive.
 */
export function CardDetailLayout({
  heroImage,
  back,
  backLabel,
  pager,
  portrait,
  title,
  meta,
  tags,
  panes,
  activeTab,
  onTabChange,
  tabCount,
  children,
}: {
  /** Painted behind the header, blurred. The portrait, normally. */
  heroImage: string
  back: { pathname: string; search?: string }
  backLabel: string
  pager?: React.ReactNode
  /** The image and the buttons under it — the whole left column. */
  portrait: React.ReactNode
  title: string
  meta: React.ReactNode
  tags?: React.ReactNode
  panes: readonly Pane[]
  activeTab: Pane
  onTabChange: (pane: Pane) => void
  /** The small number beside a tab's label, when it has one. */
  tabCount?: (pane: Pane) => number | undefined
  /** The active pane's content. */
  children: React.ReactNode
}) {
  return (
    <>
      <div className="sticky top-0 z-20 flex items-center gap-2.5 border-b border-line-soft bg-ground px-5 py-[11px]">
        <Link
          to={back}
          className="flex h-8 items-center gap-2 rounded-[10px] border border-line px-3 text-[13px] hover:bg-raised"
        >
          <ArrowLeft className="size-4" /> Back
        </Link>
        <span className="text-[12.5px] text-faint">{backLabel}</span>
        {pager && (
          <div className="ml-auto flex items-center gap-1.5">{pager}</div>
        )}
      </div>

      <div className="relative pt-[34px]">
        {/*
          The blur bleeds past the hero, so it has to be clipped — but the clip
          has to live on this inner wrapper, not on the container the grid sits
          in. `overflow: hidden` makes an element the nearest scrollport for
          anything `position: sticky` inside it, and the portrait column is
          sticky. Clipping the container therefore made the portrait resolve
          `top: 76px` against a box that never scrolls: it hung a full 76px down
          its own column on any tab whose pane left that much room, and sat
          flush on the short ones (Lorebook, Gallery). That is the jump between
          tabs. Clipping only the backdrop hands the portrait back to the real
          scrollport — AppShell's scroll region — where sticky behaves.
        */}
        <div className="pointer-events-none absolute inset-0 overflow-hidden">
          <div
            className="absolute -inset-10 bg-cover bg-center opacity-30 blur-[46px] saturate-125"
            style={{ backgroundImage: `url(${heroImage})` }}
          />
          <div className="absolute inset-0 bg-gradient-to-b from-ground/70 to-ground" />
        </div>
        <div className="relative z-2 mx-auto grid max-w-[1240px] grid-cols-1 gap-[34px] px-5 md:grid-cols-[min(280px,25vw)_1fr]">
          <div className="flex flex-col gap-2.5 md:sticky md:top-[76px] md:self-start">
            {portrait}
          </div>

          <div>
            <h1 className="font-serif text-[clamp(30px,4.2vw,50px)] leading-[1.05] font-normal text-balance">
              {title}
            </h1>
            <div className="mt-2.5 flex flex-wrap items-center gap-2.5 text-[13.5px] text-muted">
              {meta}
            </div>

            {tags}

            <nav className="mt-6 flex gap-5 overflow-x-auto overflow-y-hidden border-b border-line">
              {panes.map((pane) => {
                const count = tabCount?.(pane)
                return (
                  <button
                    key={pane}
                    type="button"
                    onClick={() => onTabChange(pane)}
                    className={cn(
                      '-mb-px border-b-2 border-transparent py-2.5 text-[13.5px] whitespace-nowrap text-muted hover:text-text',
                      activeTab === pane &&
                        'border-sage font-semibold text-text',
                    )}
                  >
                    {TAB_LABELS[pane]}
                    {!!count && (
                      <span className="ml-1.5 text-[11px] text-faint">
                        {count}
                      </span>
                    )}
                  </button>
                )
              })}
            </nav>

            <div className="flex max-w-[80ch] flex-col gap-6 pt-[22px] pb-[90px]">
              {children}
            </div>
          </div>
        </div>
      </div>
    </>
  )
}

/** The `‹ ›` pair in the header bar. */
export function NavArrow({
  onClick,
  disabled,
  children,
}: {
  onClick: () => void
  disabled?: boolean
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="grid size-8 place-items-center rounded-lg border border-line text-muted hover:bg-raised disabled:opacity-35 disabled:hover:bg-transparent"
    >
      {children}
    </button>
  )
}

/** Prev / next, with the position readout the detail page shows beside them. */
export function DetailPager({
  position,
  total,
  onPrev,
  onNext,
  prevDisabled,
  nextDisabled,
}: {
  /** 1-based, or null when this card is not in the set being paged. */
  position: number | null
  total: number
  onPrev: () => void
  onNext: () => void
  prevDisabled: boolean
  nextDisabled: boolean
}) {
  return (
    <>
      {position !== null && (
        <span className="font-mono text-[12.5px] text-faint">
          {position} of {total}
        </span>
      )}
      <NavArrow onClick={onPrev} disabled={prevDisabled}>
        <ChevronLeft className="size-4" />
      </NavArrow>
      <NavArrow onClick={onNext} disabled={nextDisabled}>
        <ChevronRight className="size-4" />
      </NavArrow>
    </>
  )
}

/** The `·` between meta items. */
export function Sep() {
  return <span className="text-faint">·</span>
}
