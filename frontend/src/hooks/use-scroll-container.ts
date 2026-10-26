import { createContext, useContext, type RefObject } from 'react'

/**
 * AppShell's scroll container -- the element that actually scrolls, since the
 * document never does (see AppShell). Pages that need to read or set the
 * scroll position, or hand it to a virtualizer, take it from here.
 */
export const ScrollContainerContext =
  createContext<RefObject<HTMLDivElement | null> | null>(null)

export function useScrollContainer(): RefObject<HTMLDivElement | null> {
  const ref = useContext(ScrollContainerContext)
  if (!ref) throw new Error('useScrollContainer must be used inside AppShell')
  return ref
}
