import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { screen, waitFor } from '@testing-library/react'
import { server } from '@/test/msw-server'
import { renderApp } from '@/test/render'
import { DiscoverPage } from './DiscoverPage'

/**
 * Discover's "Recent only": Following cards stamped with when they first came
 * back, and filtered to the last 48 hours.
 *
 * `tests/state/test_seen.py` covers the stamps themselves. What is worth
 * testing here is the wiring the feature depends on: every load reports its
 * ids, the filter is applied over the whole feed rather than the pages on
 * screen, and the chip only exists where there are stamps to filter.
 */

const HOUR = 60 * 60 * 1000

/** DataCat's Following, because it is the feed with the fewest moving parts:
 *  the follows live in settings and each creator is one request. */
const FOLLOWING = '/discover?provider=datacat&mode=following'

const CHARACTERS = [
  { characterId: 'aaaa-new', chatName: 'Abbie', creatorName: 'greatn' },
  { characterId: 'bbbb-old', chatName: 'Bella', creatorName: 'greatn' },
]

const settings = http.get('*/api/v1/settings', () =>
  HttpResponse.json({
    datacatFollowedCreators: [{ id: 'c1', name: 'greatn', source: 'datacat' }],
  }),
)

const datacat = [
  http.post('*/api/v1/datacat/dc-init', () => HttpResponse.json({ ok: true })),
  http.get('*/api/v1/datacat/dc-proxy/api/creators/c1/characters', () =>
    HttpResponse.json({ total: CHARACTERS.length, list: CHARACTERS }),
  ),
]

const emptyArchive = http.get('*/api/v1/characters/have-fragments', () =>
  HttpResponse.json({ fragments: [] }),
)

/** Abbie first seen an hour ago, Bella three days ago. Records what each load
 *  sent so a test can assert the feed reported itself. */
function seenStamps(posted: unknown[] = []) {
  return http.post('*/api/v1/discover/seen', async ({ request }) => {
    const body = (await request.json()) as { provider: string; ids: string[] }
    posted.push(body)
    const stamps: Record<string, number> = {
      'aaaa-new': Date.now() - HOUR,
      'bbbb-old': Date.now() - 72 * HOUR,
    }
    return HttpResponse.json({
      provider: body.provider,
      added: 0,
      first_seen: Object.fromEntries(body.ids.map((id) => [id, stamps[id]])),
    })
  })
}

beforeEach(() => {
  server.use(settings, ...datacat, emptyArchive)
})

describe('Recent only', () => {
  it('reports every id a Following load returned', async () => {
    const posted: unknown[] = []
    server.use(seenStamps(posted))
    renderApp(<DiscoverPage />, { route: FOLLOWING })

    await waitFor(() =>
      expect(posted).toEqual([
        { provider: 'datacat', ids: ['aaaa-new', 'bbbb-old'] },
      ]),
    )
  })

  it('shows only cards first seen inside the window', async () => {
    server.use(seenStamps())
    renderApp(<DiscoverPage />, { route: `${FOLLOWING}&recent=1` })

    await waitFor(() => expect(screen.getByText('Abbie')).toBeInTheDocument())
    expect(screen.queryByText('Bella')).not.toBeInTheDocument()
    // The count is of the filtered feed, with the whole feed beside it.
    expect(
      screen.getByText(/^1 results .*of 2 in your feed/),
    ).toBeInTheDocument()
  })

  it('toggles from the chip, and badges new cards with it off', async () => {
    server.use(seenStamps())
    const user = userEvent.setup()
    renderApp(<DiscoverPage />, { route: FOLLOWING })

    await waitFor(() => expect(screen.getByText('Bella')).toBeInTheDocument())
    // With the filter off, the badge is the answer to "which are new?".
    expect(screen.getAllByText('New')).toHaveLength(1)

    await user.click(screen.getByRole('button', { name: 'Recent only' }))
    await waitFor(() =>
      expect(screen.queryByText('Bella')).not.toBeInTheDocument(),
    )
    expect(screen.getByText('Abbie')).toBeInTheDocument()
    // Every tile is new now, so the badge would say nothing.
    expect(screen.queryByText('New')).not.toBeInTheDocument()
  })

  it('says so when nothing is new, rather than looking empty', async () => {
    server.use(
      http.post('*/api/v1/discover/seen', () =>
        HttpResponse.json({
          provider: 'datacat',
          added: 0,
          first_seen: {
            'aaaa-new': Date.now() - 72 * HOUR,
            'bbbb-old': Date.now() - 72 * HOUR,
          },
        }),
      ),
    )
    renderApp(<DiscoverPage />, { route: `${FOLLOWING}&recent=1` })

    await waitFor(() =>
      expect(
        screen.getByText('Nothing new in your feed in the last 48 hours.'),
      ).toBeInTheDocument(),
    )
  })

  it('keeps the feed usable when the stamps cannot be recorded', async () => {
    server.use(
      http.post('*/api/v1/discover/seen', () =>
        HttpResponse.json({ detail: 'disk full' }, { status: 500 }),
      ),
    )
    renderApp(<DiscoverPage />, { route: FOLLOWING })

    await waitFor(() => expect(screen.getByText('Bella')).toBeInTheDocument())
    expect(
      screen.getByText(/Couldn’t record which cards are new/),
    ).toBeInTheDocument()
  })

  it('is absent outside Following, where nothing is stamped', async () => {
    server.use(
      http.get('*//api.chub.ai/search', () =>
        HttpResponse.json({
          nodes: [{ id: 1, fullPath: 'a/b', name: 'Cass' }],
        }),
      ),
    )
    renderApp(<DiscoverPage />, { route: '/discover?recent=1' })

    await waitFor(() => expect(screen.getByText('Cass')).toBeInTheDocument())
    expect(
      screen.queryByRole('button', { name: 'Recent only' }),
    ).not.toBeInTheDocument()
  })
})
