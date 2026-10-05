import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import userEvent from '@testing-library/user-event'
import { screen, waitFor, within } from '@testing-library/react'
import { server } from '@/test/msw-server'
import { card, renderApp } from '@/test/render'
import { Route, Routes } from 'react-router'
import { CharacterDetailPage } from './CharacterDetailPage'
import { Toaster } from '@/components/ui/Toaster'

/** The detail page needs a matched route for useParams to see :id. The Toaster
 *  rides along so write feedback (Saved, stale-write) is assertable. */
function renderDetail(route: string) {
  return renderApp(
    <>
      <Routes>
        <Route path="/characters/:id" element={<CharacterDetailPage />} />
      </Routes>
      <Toaster />
    </>,
    { route },
  )
}

/** A CardDetailOut: the summary a tile uses, plus the embedded V3 `card`. */
function detailCard(overrides: Record<string, unknown> = {}) {
  return {
    ...card(overrides),
    spec: 'chara_card_v3',
    spec_version: '3.0',
    gallery: { gallery_id: '', folder: '', exists: false, images: 0, bytes: 0 },
    expressions: {
      gallery_id: '',
      folder: '',
      exists: false,
      images: 0,
      bytes: 0,
    },
    expressions_zip_url: null,
    card: {
      name: 'Abbie',
      description: 'A long description of Abbie.',
      personality: 'wry and watchful',
      first_mes: 'You again.',
      alternate_greetings: ['A second greeting.'],
      creator_notes: '## About\n\nWorks best under 300 words.',
      character_book: {
        name: 'Abbie world',
        entries: [{ keys: ['the tower'], content: 'tall and cold', id: 1 }],
      },
      ...(overrides.card as object),
      // Indexable: a test that needs one more field (a scenario, say) sets it
      // on the fixture afterwards rather than restating the whole card.
    } as Record<string, unknown>,
    ...overrides,
  }
}

const detailHandler = (data = detailCard()) =>
  http.get('*/api/v1/characters/:id', () =>
    HttpResponse.json(data, { headers: { ETag: '"abc-1"' } }),
  )

/** The neighbour list the prev/next pager reads. */
const listHandler = (items = [detailCard({ id: 'Abbie_0d162f5f.png' })]) =>
  http.get('*/api/v1/characters', () =>
    HttpResponse.json({ total: items.length, limit: 100, offset: 0, items }),
  )

/** What the Lorebook tab reads beside the card: the lorebooks its book was
 *  assembled from, and whether a sync has rewritten it. */
const loreHandlers = (
  books: Record<string, unknown>[] = [],
  original: Record<string, unknown> = { exists: false },
) => [
  http.get('*/api/v1/characters/:id/lorebooks', () => HttpResponse.json(books)),
  http.get('*/api/v1/characters/:id/lorebook-original', () =>
    HttpResponse.json(original),
  ),
]

/** The Description section's own Edit button, scoped so a click can't land on
 *  Scenario's or Personality's -- Overview shows all three now. */
function descriptionEditButton() {
  const heading = screen.getByRole('heading', { name: /^Description/ })
  return within(heading.closest('section') as HTMLElement).getByRole('button', {
    name: /^Edit$/,
  })
}

describe('CharacterDetailPage', () => {
  it('renders the overview: tagline, description, scenario, personality, but NOT the first greeting', async () => {
    // The tagline is the page blurb (`page_name`) when it differs from the name.
    // `detailCard`'s trailing `...overrides` spread replaces `card` wholesale
    // rather than merging it, so the scenario is added after the fact.
    const withScenario = detailCard({ page_name: 'The reluctant guardian' })
    withScenario.card.scenario = 'A storm strands them together.'
    server.use(detailHandler(withScenario), listHandler())
    renderDetail('/characters/Abbie_0d162f5f.png')

    expect(
      await screen.findByRole('heading', { name: 'Abbie', level: 1 }),
    ).toBeInTheDocument()
    expect(screen.getByText('The reluctant guardian')).toBeInTheDocument()
    expect(screen.getByText('A long description of Abbie.')).toBeInTheDocument()
    expect(
      screen.getByText('A storm strands them together.'),
    ).toBeInTheDocument()
    expect(screen.getByText('wry and watchful')).toBeInTheDocument()
    // Stage 6B D7: greeting 1 belongs to the Greetings tab, which shows it in
    // full and is the only place it can be edited. Overview used to render it
    // too, so a long greeting swamped the tab and two copies could disagree
    // mid-edit.
    expect(screen.queryByText('You again.')).not.toBeInTheDocument()
  })

  it('shows an empty prose field, not an empty-state placeholder, for a missing scenario', async () => {
    // The fixture card carries no `scenario` at all.
    server.use(detailHandler(), listHandler())
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    expect(
      screen.getByRole('heading', { name: /^Scenario/ }),
    ).toBeInTheDocument()
    expect(screen.queryByText(/no scenario/i)).not.toBeInTheDocument()
  })

  it('switches tabs: Greetings lists every greeting, Info shows card.json', async () => {
    server.use(detailHandler(), listHandler())
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    await userEvent.click(screen.getByRole('button', { name: /Greetings/ }))
    expect(screen.getByText('Greeting 1 · primary')).toBeInTheDocument()
    expect(screen.getByText('A second greeting.')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /^Info$/ }))
    expect(
      screen.getByText(/"chara_card_v3"|chara_card_v3/),
    ).toBeInTheDocument()
    // card.json block carries the description verbatim.
    expect(screen.getByText(/A long description of Abbie/)).toBeInTheDocument()
  })

  it('Info records the listing name, including when it repeats the card name', async () => {
    // The title the card was listed under upstream. Overview hides it when it
    // merely repeats the name; Info is a provenance record and always shows it,
    // because "the listing was called the same thing" is a real answer.
    const listed = detailCard({ page_name: 'Abbie' })
    server.use(detailHandler(listed), listHandler([listed]))
    renderDetail('/characters/Abbie_0d162f5f.png?tab=info')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    const row = screen.getByText('Listing name').closest('div') as HTMLElement
    expect(row).toHaveTextContent('Abbie')
  })

  it('Info dashes the listing name for a card that carries none', async () => {
    // A hand-dropped PNG has no upstream listing at all -- the row stays, so a
    // missing title reads as missing rather than as an absent row.
    server.use(detailHandler(), listHandler())
    renderDetail('/characters/Abbie_0d162f5f.png?tab=info')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    const row = screen.getByText('Listing name').closest('div') as HTMLElement
    expect(row).toHaveTextContent('\u2014')
  })

  it('the lorebook tab shows the entry keys', async () => {
    server.use(detailHandler(), listHandler(), ...loreHandlers())
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    await userEvent.click(screen.getByRole('button', { name: /Lorebook/ }))
    expect(screen.getByText('the tower')).toBeInTheDocument()
    expect(screen.getByText('tall and cold')).toBeInTheDocument()
  })

  it('the lorebook tab lists the card’s lorebooks and detaches one', async () => {
    const book = {
      lorebook_id: 'abc123',
      name: 'Abbie world',
      kind: 'embedded',
      entry_count: 1,
      card_count: 3,
      revision: 'f1',
      is_newest: true,
      revision_count: 1,
      state: 'attached',
      pending_upgrades: 0,
    }
    let detached = false
    server.use(
      detailHandler(),
      listHandler(),
      ...loreHandlers([book], {
        exists: true,
        taken_at: '2026-10-05T12:00:00+00:00',
        entry_count: 4,
        differs: true,
      }),
      http.delete('*/api/v1/characters/:id/lorebooks/:lorebook', () => {
        detached = true
        return HttpResponse.json([{ ...book, state: 'detaching' }])
      }),
    )
    renderDetail('/characters/Abbie_0d162f5f.png?tab=lore')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    expect(
      await screen.findByRole('link', { name: 'Abbie world' }),
    ).toHaveAttribute('href', '/lore/abc123')
    // A synced card offers its original back.
    expect(
      screen.getByRole('button', { name: 'Restore the original' }),
    ).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Detach' }))
    await waitFor(() => expect(detached).toBe(true))
  })

  it('the dialogue tab renders mes_example as a transcript, macro speaker resolved', async () => {
    const withDialogue = detailCard({ has_example_dialogue: true })
    withDialogue.card.mes_example =
      '<start>\n{{user}}: Who are you?\n{{char}}: Just a traveler.'
    server.use(detailHandler(withDialogue), listHandler([withDialogue]))
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    await userEvent.click(screen.getByRole('button', { name: /^Dialogue/ }))
    expect(screen.getByText('Who are you?')).toBeInTheDocument()
    expect(screen.getByText('Just a traveler.')).toBeInTheDocument()
    // Speaker labels: {{char}} resolves to the card's name, {{user}} to "You".
    expect(screen.getAllByText('Abbie').length).toBeGreaterThan(0)
    expect(screen.getByText('You')).toBeInTheDocument()
  })

  it('the dialogue tab shows the empty state for a card with no mes_example', async () => {
    server.use(detailHandler(), listHandler())
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    await userEvent.click(screen.getByRole('button', { name: /^Dialogue/ }))
    expect(screen.getByText(/no example dialogue/i)).toBeInTheDocument()
  })

  it('the expressions tab groups sprites by label, neutral first, and offers a download link', async () => {
    const withExpressions = detailCard({
      expressions: {
        gallery_id: 'kzbYR2QbpncC',
        folder: 'Abbie_kzbYR2QbpncC',
        exists: true,
        images: 3,
        bytes: 300,
      },
      expressions_zip_url:
        '/api/v1/characters/Abbie_0d162f5f.png/expressions.zip',
    })
    server.use(
      detailHandler(withExpressions),
      listHandler([withExpressions]),
      http.get('*/api/v1/expressions/:folder', () =>
        HttpResponse.json({
          folder: 'Abbie_kzbYR2QbpncC',
          total: 3,
          bytes: 300,
          items: [
            {
              name: 'sadness-_00001_.webp',
              kind: 'image',
              size: 100,
              modified: '2026-08-01T00:00:00Z',
              url: '/x',
              thumb_url: '/x/thumb',
            },
            {
              name: 'neutral-_00001_.webp',
              kind: 'image',
              size: 100,
              modified: '2026-08-01T00:00:00Z',
              url: '/y',
              thumb_url: '/y/thumb',
            },
            {
              name: 'joy-_00001_.webp',
              kind: 'image',
              size: 100,
              modified: '2026-08-01T00:00:00Z',
              url: '/z',
              thumb_url: '/z/thumb',
            },
          ],
        }),
      ),
    )
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    await userEvent.click(screen.getByRole('button', { name: /^Expressions/ }))
    await screen.findByText('neutral')
    const headings = screen
      .getAllByRole('heading', { level: 4 })
      .map((h) => h.textContent)
    expect(headings[0]).toMatch(/^neutral/)
    expect(screen.getByRole('link', { name: /Download all/ })).toHaveAttribute(
      'href',
      '/api/v1/characters/Abbie_0d162f5f.png/expressions.zip',
    )
  })

  it('the expressions tab shows the empty state for a character with no expressions folder', async () => {
    server.use(detailHandler(), listHandler())
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    await userEvent.click(screen.getByRole('button', { name: /^Expressions/ }))
    expect(screen.getByText(/no expression sprites/i)).toBeInTheDocument()
  })

  it('a card with no lorebook shows the empty state', async () => {
    const bare = detailCard({ card: { character_book: null } })
    server.use(detailHandler(bare), listHandler([bare]), ...loreHandlers())
    renderDetail('/characters/x.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    await userEvent.click(screen.getByRole('button', { name: /Lorebook/ }))
    expect(screen.getByText(/No lorebook on this card/)).toBeInTheDocument()
  })

  it('links the creator at the browse route, not at /characters', async () => {
    // `/characters/:id` is this page and browse is the index route, so
    // `/characters?creator=…` looks right and silently lands on the catch-all.
    // Pinned as an href rather than a click because that failure renders a
    // page with no error on it at all.
    server.use(detailHandler(), listHandler())
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    expect(screen.getByRole('link', { name: 'KornyPony' })).toHaveAttribute(
      'href',
      '/?creator=KornyPony',
    )
  })

  it('deep-links a tab via ?tab=', async () => {
    server.use(detailHandler(), listHandler())
    renderDetail('/characters/Abbie_0d162f5f.png?tab=info')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })
    expect(screen.getByText(/A long description of Abbie/)).toBeInTheDocument()
  })

  it('edits the description: whole-card PUT with the read’s If-Match', async () => {
    let put: {
      body: { card: Record<string, unknown> }
      ifMatch: string | null
    } | null = null
    server.use(
      detailHandler(),
      listHandler(),
      http.put('*/api/v1/characters/:id', async ({ request }) => {
        const body = (await request.json()) as { card: Record<string, unknown> }
        put = { body, ifMatch: request.headers.get('If-Match') }
        return HttpResponse.json(detailCard({ card: { ...body.card } }), {
          headers: { ETag: '"abc-2"' },
        })
      }),
    )
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    await userEvent.click(descriptionEditButton())
    const box = screen.getByRole('textbox')
    await userEvent.clear(box)
    await userEvent.type(box, 'A rewritten description.')
    await userEvent.click(screen.getByRole('button', { name: /^Save$/ }))

    await waitFor(() => expect(put).not.toBeNull())
    expect(put!.ifMatch).toBe('"abc-1"')
    // The whole card goes back, not a patch — name and greeting ride along.
    expect(put!.body.card.name).toBe('Abbie')
    expect(put!.body.card.first_mes).toBe('You again.')
    expect(put!.body.card.description).toBe('A rewritten description.')
    // The editor closes and the new prose is shown.
    await waitFor(() =>
      expect(screen.getByText('A rewritten description.')).toBeInTheDocument(),
    )
  })

  it('a stale write (412) is reported, not clobbered', async () => {
    server.use(
      detailHandler(),
      listHandler(),
      http.put('*/api/v1/characters/:id', () =>
        HttpResponse.json({ detail: 'stale' }, { status: 412 }),
      ),
    )
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    await userEvent.click(descriptionEditButton())
    await userEvent.click(screen.getByRole('button', { name: /^Save$/ }))

    // The editor stays open (a textbox is still present) and a toast explains why.
    expect(
      await screen.findByText(/changed since you opened it/i),
    ).toBeInTheDocument()
    expect(screen.getByRole('textbox')).toBeInTheDocument()
  })

  it('favourites the card: POST with the new value, button reflects it', async () => {
    let posted: boolean | null = null
    server.use(
      detailHandler(),
      listHandler(),
      http.post('*/api/v1/characters/:id/favorite', async ({ request }) => {
        const body = (await request.json()) as { value: boolean }
        posted = body.value
        return HttpResponse.json({
          id: 'Abbie_0d162f5f.png',
          favorite: body.value,
        })
      }),
    )
    renderDetail('/characters/Abbie_0d162f5f.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })

    await userEvent.click(screen.getByRole('button', { name: /^Favourite$/ }))
    await waitFor(() => expect(posted).toBe(true))
    expect(
      await screen.findByRole('button', { name: /^Favourited$/ }),
    ).toBeInTheDocument()
  })

  it('J pages to the next card in the neighbour set', async () => {
    const a = detailCard({ id: 'a.png', name: 'Abbie' })
    const b = detailCard({ id: 'b.png', name: 'Bella' })
    server.use(
      http.get('*/api/v1/characters/:id', ({ params }) =>
        HttpResponse.json(params.id === 'b.png' ? b : a),
      ),
      listHandler([a, b]),
    )
    renderDetail('/characters/a.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })
    await waitFor(() => expect(screen.getByText('1 of 2')).toBeInTheDocument())

    await userEvent.keyboard('j')

    expect(
      await screen.findByRole('heading', { name: 'Bella', level: 1 }),
    ).toBeInTheDocument()
  })

  it('J and K do nothing while a field is being edited', async () => {
    // The regression: J/K are bare letters bound at the window, so typing a
    // word containing one into the description paged to the next card and threw
    // the unsaved draft away.
    const a = detailCard({ id: 'a.png', name: 'Abbie' })
    const b = detailCard({ id: 'b.png', name: 'Bella' })
    server.use(
      http.get('*/api/v1/characters/:id', ({ params }) =>
        HttpResponse.json(params.id === 'b.png' ? b : a),
      ),
      listHandler([a, b]),
    )
    renderDetail('/characters/a.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })
    await waitFor(() => expect(screen.getByText('1 of 2')).toBeInTheDocument())

    await userEvent.click(descriptionEditButton())
    const box = screen.getByRole('textbox')
    await userEvent.clear(box)
    await userEvent.type(box, 'jack and kate')

    expect(box).toHaveValue('jack and kate')
    expect(
      screen.getByRole('heading', { name: 'Abbie', level: 1 }),
    ).toBeInTheDocument()
  })

  it('the pager reports the position within the neighbour set', async () => {
    const a = detailCard({ id: 'a.png', name: 'Abbie' })
    const b = detailCard({ id: 'b.png', name: 'Bella' })
    server.use(
      http.get('*/api/v1/characters/:id', ({ params }) =>
        HttpResponse.json(params.id === 'b.png' ? b : a),
      ),
      listHandler([a, b]),
    )
    renderDetail('/characters/a.png')
    await screen.findByRole('heading', { name: 'Abbie', level: 1 })
    await waitFor(() => expect(screen.getByText('1 of 2')).toBeInTheDocument())
  })
})
