import '@testing-library/jest-dom/vitest'
import { afterAll, afterEach, beforeAll } from 'vitest'
import { server } from './msw-server'

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

// jsdom implements none of these, and radix's primitives (Select, Popover,
// Dialog -- most of the mock's affordances) call them during pointer
// interaction. Without the stubs the components throw rather than render.
Element.prototype.hasPointerCapture ??= () => false
Element.prototype.setPointerCapture ??= () => {}
Element.prototype.releasePointerCapture ??= () => {}
Element.prototype.scrollIntoView ??= () => {}
// The detail tab strip scrolls its active tab into view (CardDetailLayout).
Element.prototype.scrollTo ??= () => {}

// jsdom implements neither observer, and both are load-bearing here: the grid
// pages on IntersectionObserver and the shelf measures its row with
// ResizeObserver. The stubs record their instances so a test can drive them --
// see `src/test/observers.ts`.
class StubResizeObserver implements ResizeObserver {
  private callback: ResizeObserverCallback
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
    resizeObservers.push(this)
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  /** Fire the callback as a real resize would. */
  fire() {
    this.callback([], this)
  }
}

class StubIntersectionObserver implements IntersectionObserver {
  readonly root = null
  readonly rootMargin = ''
  readonly thresholds: number[] = []
  private callback: IntersectionObserverCallback
  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback
    intersectionObservers.push(this)
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
  /** Report the observed element as having scrolled into view. */
  fire() {
    this.callback([{ isIntersecting: true } as IntersectionObserverEntry], this)
  }
}

export const resizeObservers: StubResizeObserver[] = []
export const intersectionObservers: StubIntersectionObserver[] = []

globalThis.ResizeObserver = StubResizeObserver
globalThis.IntersectionObserver =
  StubIntersectionObserver as unknown as typeof IntersectionObserver

afterEach(() => {
  resizeObservers.length = 0
  intersectionObservers.length = 0
})
