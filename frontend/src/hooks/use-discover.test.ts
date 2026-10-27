import { describe, it, expect } from 'vitest'
import { http, HttpResponse } from 'msw'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { server } from '@/test/msw-server'
import { idFragment, useHaveFragments } from './use-discover'

function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
}

describe('idFragment — the client-side mirror of aspect/cards/naming.py:id_fragment', () => {
  it('takes the first 8 characters', () => {
    expect(idFragment('0d162f5f-86ab-4fdd-a2c2-3912adf24960')).toBe('0d162f5f')
  })

  it('strips characters a filename cannot carry before slicing', () => {
    expect(idFragment('  weird/id:with*junk  ')).toBe('weirdidw')
  })

  it('strips dots too, matching aspect/cards/naming.py exactly', () => {
    // Only [A-Za-z0-9_-] survive server-side; a dot is not in that set.
    expect(idFragment('a.b.c.d.e.f.g.h.i')).toBe('abcdefgh')
  })

  it('passes a short id through unchanged, not padded', () => {
    expect(idFragment('42')).toBe('42')
  })
})

describe('useHaveFragments — fetched once, matched locally', () => {
  it('exposes the archive fragment set as a Set', async () => {
    server.use(
      http.get('*/api/v1/characters/have-fragments', () =>
        HttpResponse.json({ fragments: ['0d162f5f', 'aaaa1111'] }),
      ),
    )

    const { result } = renderHook(() => useHaveFragments(), {
      wrapper: wrapper(),
    })

    await waitFor(() => expect(result.current.data).toBeDefined())
    expect(result.current.data).toEqual(new Set(['0d162f5f', 'aaaa1111']))
    // The full id, matched by its fragment against the fetched set --
    // exactly how DiscoverPage/DiscoverPreviewPage now check "have".
    expect(
      result.current.data?.has(idFragment('0d162f5f-86ab-4fdd-a2c2')),
    ).toBe(true)
    expect(result.current.data?.has(idFragment('deadbeef-0000-0000'))).toBe(
      false,
    )
  })
})
