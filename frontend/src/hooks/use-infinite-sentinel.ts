import { useEffect, useRef } from 'react'

/**
 * Infinite scroll: a ref for a sentinel below the grid, which asks for the next
 * page as it comes into view. Kept a screen ahead with rootMargin so the grid
 * does not visibly stop at the bottom of each page. `length` re-arms the
 * observer once a page lands, in case the sentinel is still in view.
 */
export function useInfiniteSentinel(
  hasNextPage: boolean,
  fetchNextPage: () => unknown,
  length: number,
) {
  const sentinelRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const element = sentinelRef.current
    if (!element || !hasNextPage) return
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) void fetchNextPage()
      },
      { rootMargin: '600px' },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [hasNextPage, fetchNextPage, length])
  return sentinelRef
}
