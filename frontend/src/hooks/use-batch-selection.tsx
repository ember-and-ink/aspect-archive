import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { useLocation } from 'react-router'

interface BatchSelectionValue {
  /** Batch mode on/off — toggled from the icon between the search bar and
   *  the import button. */
  active: boolean
  selected: Set<string>
  toggleActive: () => void
  toggleSelected: (id: string) => void
  /** Cancel: unselect everything and exit batch mode. */
  clear: () => void
}

const BatchSelectionContext = createContext<BatchSelectionValue | null>(null)

/** The routes batch mode makes sense on — the character grid in either of its
 *  two guises. Elsewhere there is no grid to select from. */
const BATCH_ROUTES = new Set(['/', '/favorites'])

/**
 * Batch-select state for the bulk-delete flow (docs: batch mode).
 *
 * Lives above `TopBar` and the routed pages rather than inside
 * `CharactersPage` — the toggle icon and the grid it drives are siblings
 * under `AppShell`, not parent/child, so the state has to sit a layer up.
 */
export function BatchSelectionProvider({
  children,
}: {
  children: React.ReactNode
}) {
  const location = useLocation()
  const [active, setActive] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  // Navigating off the grid (a tab click, a card... though cards don't
  // navigate in batch mode) leaves nothing behind to select — exit rather
  // than carry a stale selection to a page that can't show it.
  useEffect(() => {
    if (!BATCH_ROUTES.has(location.pathname)) {
      setActive(false)
      setSelected(new Set())
    }
  }, [location.pathname])

  const value = useMemo<BatchSelectionValue>(
    () => ({
      active,
      selected,
      toggleActive: () => {
        setActive((was) => !was)
        setSelected(new Set())
      },
      toggleSelected: (id) =>
        setSelected((prev) => {
          const next = new Set(prev)
          if (next.has(id)) next.delete(id)
          else next.add(id)
          return next
        }),
      clear: () => {
        setActive(false)
        setSelected(new Set())
      },
    }),
    [active, selected],
  )

  return (
    <BatchSelectionContext.Provider value={value}>
      {children}
    </BatchSelectionContext.Provider>
  )
}

// The provider and its hook belong together; fast-refresh's "components only"
// rule does not apply to a context module.
// oxlint-disable-next-line react/only-export-components
export function useBatchSelection() {
  const ctx = useContext(BatchSelectionContext)
  if (!ctx)
    throw new Error(
      'useBatchSelection must be used within a BatchSelectionProvider',
    )
  return ctx
}
