import {
  Fragment,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useScrollContainer } from '@/hooks/use-scroll-container'
import { cn } from '@/lib/utils'
import { TILE_TEXT_HEIGHT } from './CardTile'

// CardGrid's gaps, in px: gap-x-3.5 and gap-y-[22px].
const GAP_X = 14
const GAP_Y = 22
// Rows kept rendered beyond each edge of the viewport. Two is about a screen on
// a phone and half of one on a desktop -- enough that a flick does not outrun
// the tiles, few enough that the DOM stays a few dozen tiles deep.
const OVERSCAN_ROWS = 2

/**
 * The browse grid, rendering only the rows near the viewport.
 *
 * The grid used to keep every tile it had paged in: ~9 DOM nodes, 8 listeners
 * and a decoded 373x560 thumb per card, so memory grew ~0.2 MB per card
 * scrolled past and never came back (measured 2026-10-26: 502 tiles held
 * 4,761 nodes and 3,966 listeners). On iPad/iPhone Safari that is what makes
 * deep browsing stutter and the tab reload. Now off-screen rows unmount, and
 * their images with them.
 *
 * Rows are fixed-height -- 2:3 art plus `TILE_TEXT_HEIGHT` -- so offsets are
 * computed from the width rather than measured, and a restored scroll position
 * lands on the same row it was saved from. Columns come from the page's
 * `useGridColumns` ruler, so the track definition still lives only in
 * `CardGrid`.
 */
export function VirtualCardGrid<T>({
  items,
  columns,
  getKey,
  renderItem,
  className,
}: {
  items: T[]
  columns: number
  getKey: (item: T) => string
  renderItem: (item: T) => ReactNode
  className?: string
}) {
  const scrollRef = useScrollContainer()
  const listRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  // Where the list starts inside the scroll container -- the sticky bar and the
  // shelf sit above it, and the virtualizer's offsets are relative to the
  // scroll container's top.
  const [scrollMargin, setScrollMargin] = useState(0)

  useLayoutEffect(() => {
    const list = listRef.current
    if (!list) return
    // The scroll container is read per call, not captured: on a cold load this
    // effect runs before AppShell -- a parent -- has attached its ref, and the
    // observer's first callback (after paint) is what fills the margin in.
    const measure = () => {
      setWidth(list.clientWidth)
      const scroller = scrollRef.current
      if (scroller)
        setScrollMargin(
          list.getBoundingClientRect().top -
            scroller.getBoundingClientRect().top +
            scroller.scrollTop,
        )
    }
    measure()
    // The list's own width, and its parent's height -- which moves when the
    // shelf above it appears, hides or reflows.
    const observer = new ResizeObserver(measure)
    observer.observe(list)
    if (list.parentElement) observer.observe(list.parentElement)
    return () => observer.disconnect()
  }, [scrollRef])

  const cols = Math.max(1, columns)
  const tileWidth = width > 0 ? (width - GAP_X * (cols - 1)) / cols : 0
  const rowHeight = tileWidth * 1.5 + TILE_TEXT_HEIGHT + GAP_Y
  const rowCount = Math.ceil(items.length / cols)

  const virtualizer = useVirtualizer({
    count: rowCount,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowHeight,
    overscan: OVERSCAN_ROWS,
    scrollMargin,
  })

  // estimateSize is only read when the cache is rebuilt, so a width or column
  // change (rotating an iPad, a scrollbar appearing) has to ask for it.
  useLayoutEffect(() => {
    virtualizer.measure()
  }, [virtualizer, rowHeight, cols])

  return (
    <div
      ref={listRef}
      className={cn('relative', className)}
      style={{ height: virtualizer.getTotalSize() }}
    >
      {virtualizer.getVirtualItems().map((row) => (
        <div
          key={row.key}
          className="absolute inset-x-0 top-0 grid gap-x-3.5"
          style={{
            gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
            transform: `translateY(${row.start - scrollMargin}px)`,
          }}
        >
          {items
            .slice(row.index * cols, row.index * cols + cols)
            .map((item) => (
              <Fragment key={getKey(item)}>{renderItem(item)}</Fragment>
            ))}
        </div>
      ))}
    </div>
  )
}
