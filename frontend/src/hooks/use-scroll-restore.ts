import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from 'react'
import { useLocation } from 'react-router'
import { useScrollContainer } from './use-scroll-container'

type Saved = { top: number; loaded: number }

const storageKey = (locationKey: string) => `grid-scroll:${locationKey}`

function read(locationKey: string): Saved | null {
  try {
    const raw = sessionStorage.getItem(storageKey(locationKey))
    return raw ? (JSON.parse(raw) as Saved) : null
  } catch {
    return null
  }
}

function write(locationKey: string, saved: Saved) {
  try {
    sessionStorage.setItem(storageKey(locationKey), JSON.stringify(saved))
  } catch {
    // Private mode or storage blocked: Back lands at the top, as it always did.
  }
}

/**
 * Put the browse grid back where it was when the user comes Back to it.
 *
 * Keyed by the history entry (`location.key`), not by the filter set: Back --
 * or an iPad back-swipe -- returns to the same place, while opening the tab
 * fresh or changing a filter (a `replace`, so a new key) starts at the top.
 * Until 2026-10-26 Back always landed at the top: the scroll container is
 * AppShell's div, not the document, so the browser's own restoration never
 * applied.
 *
 * The position only means something with the same cards above it, so the
 * number of cards loaded is saved too and the restore waits until that many
 * are back -- instant from the query cache, a few ~20 ms page reads if it
 * expired.
 *
 * `contentRef` is any element of the page: scroll events are ignored once it
 * has left the DOM, so the next page resetting the shared scroll container
 * cannot overwrite the position being saved.
 */
export function useScrollRestore({
  contentRef,
  loaded,
  hasNextPage,
  isFetching,
  fetchNextPage,
  resetKey,
}: {
  contentRef: RefObject<HTMLElement | null>
  loaded: number
  hasNextPage: boolean
  isFetching: boolean
  fetchNextPage: () => unknown
  /** Anything that changes which cards are listed; a change scrolls to top. */
  resetKey: string
}) {
  const scrollRef = useScrollContainer()
  const { key } = useLocation()
  // What to restore, read once on mount; `pending` clears when it is done.
  const [target] = useState(() => read(key))
  const pending = useRef(target)
  const attempts = useRef(0)
  const last = useRef<Saved>({ top: 0, loaded })
  useEffect(() => {
    last.current.loaded = loaded
  }, [loaded])

  // Record as the user scrolls; persist when the page goes away.
  useEffect(() => {
    const scroller = scrollRef.current
    if (!scroller) return
    const onScroll = () => {
      if (contentRef.current?.isConnected)
        last.current = { top: scroller.scrollTop, loaded: last.current.loaded }
    }
    scroller.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      scroller.removeEventListener('scroll', onScroll)
      write(key, last.current)
    }
  }, [scrollRef, contentRef, key])

  // Page in until the saved position's cards are back.
  useEffect(() => {
    if (!pending.current || !target) return
    if (loaded < target.loaded && hasNextPage && !isFetching)
      void fetchNextPage()
  }, [target, loaded, hasNextPage, isFetching, fetchNextPage])

  // Then jump. Retried for a few renders until the grid is tall enough to hold
  // the offset -- the virtualized grid sizes itself a render after its width is
  // known -- and abandoned after that (the archive shrank, say) rather than
  // fighting the user's own scrolling.
  useLayoutEffect(() => {
    const saved = pending.current
    const scroller = scrollRef.current
    if (!saved || !scroller) return
    if (loaded < saved.loaded && hasNextPage) return
    scroller.scrollTop = saved.top
    const reachable = scroller.scrollHeight - scroller.clientHeight
    if (reachable >= saved.top || ++attempts.current >= 5)
      pending.current = null
  })

  // A different set of cards starts at the top. Skipped on mount, where the
  // restore above owns the position.
  const previousReset = useRef(resetKey)
  useLayoutEffect(() => {
    if (previousReset.current === resetKey) return
    previousReset.current = resetKey
    pending.current = null
    if (scrollRef.current) scrollRef.current.scrollTop = 0
  }, [resetKey, scrollRef])
}
