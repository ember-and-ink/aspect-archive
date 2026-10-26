import { NavLink, useLocation, useNavigate } from 'react-router'
import { RefreshCw, Search, Settings, SquareMousePointer } from 'lucide-react'
import { useBatchSelection } from '@/hooks/use-batch-selection'
import { useRescan } from '@/hooks/use-rescan'
import { cn } from '@/lib/utils'
import { ImportPopover } from './ImportPopover'

/** The routes whose batch mode is toggled from here — the same set as
 *  `BATCH_ROUTES` in `use-batch-selection.tsx`. */
const BATCH_TOGGLE_ROUTES = new Set(['/', '/favorites'])

const TABS = [
  { to: '/', label: 'Characters' },
  { to: '/favorites', label: 'Favorites' },
  { to: '/discover', label: 'Discover' },
  { to: '/tools', label: 'Tools' },
]

/**
 * The fixed top bar: brand, the four tabs, the search pill, settings.
 *
 * No Activity bell — dropped with the feed it opened (docs/UI_REWRITE_PLAN.md
 * §3.6). The Import ＋ opens the add-to-archive menu (Stage 3), and the
 * circular arrow beside it rescans the archive directory -- the server never
 * does that on its own (see `use-rescan`).
 */
export function TopBar({ onSearch }: { onSearch: () => void }) {
  const navigate = useNavigate()
  const location = useLocation()
  const batch = useBatchSelection()
  const showBatchToggle = BATCH_TOGGLE_ROUTES.has(location.pathname)
  const rescan = useRescan()
  // The last result rides on the tooltip rather than a toast: a rescan that
  // found nothing (the usual case) is not worth interrupting anyone over.
  const rescanTitle = rescan.isError
    ? `Rescan failed: ${rescan.error.message}`
    : rescan.data
      ? `Rescanned ${rescan.data.scanned.toLocaleString()} cards · ${(
          rescan.data.parsed + rescan.data.removed
        ).toLocaleString()} changed`
      : 'Rescan the archive folder'

  return (
    // Below `lg` the row cannot hold all of it: the tabs drop to a second line
    // and the search pill collapses to its icon. The global `min-width: 0`
    // (index.css) means a row that does not fit squashes its items into each
    // other rather than overflowing, so it has to be made to fit. The height
    // here and AppShell's `top-[…]` move together.
    <header className="fixed inset-x-0 top-0 z-40 flex h-[100px] flex-wrap content-center items-center gap-x-3.5 gap-y-2 border-b border-line-soft bg-ground px-[18px] lg:h-[60px] lg:flex-nowrap">
      <div className="flex items-center gap-2.5 font-serif text-[21px] tracking-[0.01em]">
        <span className="block size-2 rounded-full bg-sage shadow-[0_0_12px_var(--sage)]" />
        Archive
      </div>

      <nav className="order-last flex w-full gap-[3px] rounded-full border border-white/8 bg-white/5 p-[3px] lg:order-none lg:w-auto">
        {TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.to === '/'}
            className={({ isActive }) =>
              cn(
                'grow rounded-full px-2 py-1.5 text-center text-[13.5px] font-medium whitespace-nowrap text-muted-foreground hover:text-text lg:grow-0 lg:px-4',
                isActive && 'bg-text font-semibold text-[#121416]',
              )
            }
          >
            {tab.label}
          </NavLink>
        ))}
      </nav>

      <div className="flex-1" />

      <button
        type="button"
        onClick={onSearch}
        aria-label="Search the archive"
        className="flex h-[35px] w-[35px] shrink-0 items-center justify-center gap-2.5 rounded-full border border-white/8 text-[13.5px] text-faint hover:border-white/20 hover:text-text lg:w-auto lg:min-w-[210px] lg:justify-start lg:px-3.5"
      >
        <Search className="size-[15px] shrink-0" />
        <span className="hidden lg:inline">Search the archive</span>
        <kbd className="ml-auto hidden rounded-[5px] border border-line px-1.5 py-px font-mono text-[10.5px] text-faint lg:inline">
          ⌘K
        </kbd>
      </button>

      {showBatchToggle && (
        <button
          type="button"
          title="Batch select"
          onClick={batch.toggleActive}
          className={cn(
            'grid size-[35px] place-items-center rounded-full border hover:border-white/17 hover:bg-raised hover:text-text',
            batch.active
              ? 'border-sage-line bg-sage/15 text-sage'
              : 'border-white/7 text-muted-foreground',
          )}
        >
          <SquareMousePointer className="size-4" />
        </button>
      )}

      <ImportPopover />

      <button
        type="button"
        title={rescanTitle}
        aria-label="Rescan the archive folder"
        onClick={() => rescan.mutate()}
        disabled={rescan.isPending}
        className={cn(
          'grid size-[35px] place-items-center rounded-full border border-white/7 text-muted-foreground hover:border-white/17 hover:bg-raised hover:text-text disabled:opacity-70',
          rescan.isError && 'border-bad/40 text-bad',
        )}
      >
        <RefreshCw
          className={cn('size-4', rescan.isPending && 'animate-spin')}
        />
      </button>

      <button
        type="button"
        title="Settings"
        onClick={() => navigate('/settings')}
        className="grid size-[35px] place-items-center rounded-full border border-white/7 text-muted-foreground hover:border-white/17 hover:bg-raised hover:text-text"
      >
        <Settings className="size-4" />
      </button>
    </header>
  )
}
