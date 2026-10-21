import { NavLink, useLocation, useNavigate } from 'react-router'
import { Search, Settings, SquareMousePointer } from 'lucide-react'
import { useBatchSelection } from '@/hooks/use-batch-selection'
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
 * §3.6). The Import ＋ opens the add-to-archive menu (Stage 3).
 */
export function TopBar({ onSearch }: { onSearch: () => void }) {
  const navigate = useNavigate()
  const location = useLocation()
  const batch = useBatchSelection()
  const showBatchToggle = BATCH_TOGGLE_ROUTES.has(location.pathname)

  return (
    <header className="fixed inset-x-0 top-0 z-40 flex h-[60px] items-center gap-3.5 border-b border-line-soft bg-ground/90 px-[18px] backdrop-blur-[14px]">
      <div className="flex items-center gap-2.5 font-serif text-[21px] tracking-[0.01em]">
        <span className="block size-2 rounded-full bg-sage shadow-[0_0_12px_var(--sage)]" />
        Archive
      </div>

      <nav className="flex gap-[3px] rounded-full border border-white/8 bg-white/5 p-[3px]">
        {TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.to === '/'}
            className={({ isActive }) =>
              cn(
                'rounded-full px-4 py-1.5 text-[13.5px] font-medium text-muted-foreground hover:text-text',
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
        className="flex h-[35px] min-w-[210px] items-center gap-2.5 rounded-full border border-white/8 px-3.5 text-[13.5px] text-faint hover:border-white/20 hover:text-text"
      >
        <Search className="size-[15px]" />
        Search the archive
        <kbd className="ml-auto rounded-[5px] border border-line px-1.5 py-px font-mono text-[10.5px] text-faint">
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
        title="Settings"
        onClick={() => navigate('/settings')}
        className="grid size-[35px] place-items-center rounded-full border border-white/7 text-muted-foreground hover:border-white/17 hover:bg-raised hover:text-text"
      >
        <Settings className="size-4" />
      </button>
    </header>
  )
}
