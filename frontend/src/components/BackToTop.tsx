import { useEffect, useState, type RefObject } from 'react'
import { ArrowUp } from 'lucide-react'
import { cn } from '@/lib/utils'

/** How far down before the button is worth offering. Roughly one viewport. */
const THRESHOLD = 900

/**
 * Back to top, for the archive grid and the detail page (Stage 6B D4).
 *
 * It watches the shell's scroll container rather than the window, because the
 * app scrolls that element and not the document (see `AppShell`). Rendered
 * once, in the shell, so every route gets it without each page wiring its own.
 */
export function BackToTop({
  scrollRef,
}: {
  scrollRef: RefObject<HTMLDivElement | null>
}) {
  const [shown, setShown] = useState(false)

  useEffect(() => {
    const element = scrollRef.current
    if (!element) return
    const onScroll = () => setShown(element.scrollTop > THRESHOLD)
    onScroll()
    element.addEventListener('scroll', onScroll, { passive: true })
    return () => element.removeEventListener('scroll', onScroll)
  }, [scrollRef])

  return (
    <button
      type="button"
      aria-label="Back to top"
      // Kept mounted and faded rather than unmounted, so it cannot appear
      // mid-animation or steal focus when it returns.
      aria-hidden={!shown}
      tabIndex={shown ? 0 : -1}
      onClick={() =>
        scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
      }
      className={cn(
        'fixed right-6 bottom-6 z-30 grid size-11 place-items-center rounded-full border border-line bg-raised text-muted shadow-lg transition-all hover:border-sage-line hover:text-sage',
        shown
          ? 'pointer-events-auto translate-y-0 opacity-100'
          : 'pointer-events-none translate-y-2 opacity-0',
      )}
    >
      <ArrowUp className="size-5" />
    </button>
  )
}
