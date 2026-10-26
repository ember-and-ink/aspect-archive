import { useEffect, useRef, useState } from 'react'
import { Outlet } from 'react-router'
import { BatchSelectionProvider } from '@/hooks/use-batch-selection'
import { ScrollContainerContext } from '@/hooks/use-scroll-container'
import { useProviderSettings } from '@/hooks/use-settings'
import { setSavedDatacatToken } from '@/lib/providers/datacat'
import { BackToTop } from './BackToTop'
import { BatchActionBar } from './BatchActionBar'
import { SearchOverlay } from './SearchOverlay'
import { TopBar } from './TopBar'
import { Toaster } from './ui/Toaster'

/**
 * The frame every route renders inside: the fixed top bar, one scroll
 * container beneath it, and the ⌘K overlay.
 *
 * The scrolling belongs to this element rather than to the document because the
 * top bar is fixed and the section bar under it is `sticky top-0` — a sticky
 * header needs a scroll container it is actually inside of.
 */
export function AppShell() {
  const [searchOpen, setSearchOpen] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const { datacatToken } = useProviderSettings()

  // Publish the saved DataCat token to the provider module before anything
  // dials DataCat. The server holds its session in memory only, so without
  // this a restart quietly replaces the saved identity with a new anonymous
  // one (Stage 6B B3).
  useEffect(() => {
    setSavedDatacatToken(datacatToken)
  }, [datacatToken])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setSearchOpen((open) => !open)
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])

  return (
    <BatchSelectionProvider>
      <TopBar onSearch={() => setSearchOpen(true)} />
      {/* `scrollbar-gutter: stable` reserves the scrollbar's width whether or
          not it is showing. Without it, a route whose content crosses the
          viewport height gains a scrollbar and everything centred in the layout
          jumps sideways -- most visibly the detail page's portrait (Stage 6B
          D1). */}
      <div
        ref={scrollRef}
        className="absolute inset-0 top-[60px] overflow-x-hidden overflow-y-auto [scrollbar-gutter:stable]"
      >
        <ScrollContainerContext value={scrollRef}>
          <Outlet />
        </ScrollContainerContext>
      </div>
      <BackToTop scrollRef={scrollRef} />
      <SearchOverlay open={searchOpen} onOpenChange={setSearchOpen} />
      <BatchActionBar />
      <Toaster />
    </BatchSelectionProvider>
  )
}
