import { useCallback, useEffect } from 'react'
import { useNavigate, type SetURLSearchParams } from 'react-router'
import { isTypingTarget } from '@/lib/utils'

/** The detail page's active pane, kept in `?tab=` (absent means overview). */
export function useTabParam<Pane extends string>(
  params: URLSearchParams,
  setParams: SetURLSearchParams,
) {
  const activeTab = (params.get('tab') as Pane | null) ?? ('overview' as Pane)
  const setTab = (tab: Pane) => {
    const next = new URLSearchParams(params)
    if (tab === 'overview') next.delete('tab')
    else next.set('tab', tab)
    setParams(next, { replace: true })
  }
  return [activeTab, setTab] as const
}

/**
 * Prev/next through the list the user came from, on the pager and on J/K.
 * `pathOf` names a neighbour's detail route; the grid's filter params ride
 * along so the neighbour list stays the same query. Returns `go(delta)`.
 */
export function useNeighbourNavigation<T>({
  items,
  position,
  params,
  pathOf,
  feed,
}: {
  items: T[]
  position: number
  params: URLSearchParams
  pathOf: (item: T) => string
  feed: { hasNextPage: boolean; fetchNextPage: () => unknown }
}) {
  const navigate = useNavigate()
  const go = useCallback(
    (delta: number) => {
      if (position < 0) return
      const target = items[position + delta]
      if (target) {
        const next = new URLSearchParams(params)
        next.delete('tab')
        navigate(
          { pathname: pathOf(target), search: next.toString() },
          { replace: true },
        )
      } else if (delta > 0 && feed.hasNextPage) {
        // The neighbour is on a page not yet fetched — pull it, then the list
        // grows and the next press finds it.
        void feed.fetchNextPage()
      }
    },
    [position, items, params, navigate, pathOf, feed],
  )

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // J/K are bare letters, so they collide with typing. Stage 3 put
      // textareas and tag inputs on the detail page, and without this guard a
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

  return go
}
