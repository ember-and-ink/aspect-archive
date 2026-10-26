import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { CreatorNotes, type MediaMap } from './CreatorNotes'

/** The rendered notes: jsdom implements shadow DOM, so this is the same tree
 *  the browser shows, not a string it was built from. */
function notesRoot(notes: string, media?: MediaMap): ShadowRoot {
  const { getByTestId } = render(<CreatorNotes notes={notes} media={media} />)
  return getByTestId('creator-notes').shadowRoot!
}

const FILE = '/api/v1/galleries/Mara_X/files/localized_media_1_a.webp'

describe('CreatorNotes', () => {
  it('renders markdown image syntax as an <img> from the gallery, not a mangled link', () => {
    // The shape proxy/text/html_md.py produces for every source that flattens
    // creator notes to markdown (JanitorAI, saucepan, datacat, JannyAI).
    const root = notesRoot(
      'Look at this:\n\n![a portrait](https://example.com/a.png)',
      { 'https://example.com/a.png': FILE },
    )
    const img = root.querySelector('img')!
    expect(img.getAttribute('src')).toBe(FILE)
    expect(img.getAttribute('alt')).toBe('a portrait')
    expect(root.querySelector('a[href="https://example.com/a.png"]')).toBeNull()
  })

  it('never hotlinks an archived card: an image the gallery lacks becomes a placeholder', () => {
    const root = notesRoot('![a portrait](https://saucepan.ai/cdn/x/card)', {})
    expect(root.querySelector('img')).toBeNull()
    expect(root.querySelector('.notes-missing')!.textContent).toBe(
      'a portrait · not in gallery',
    )
  })

  it('says a known-dead image is dead', () => {
    const root = notesRoot('<img src="https://example.com/gone.png">', {
      'https://example.com/gone.png': null,
    })
    expect(root.querySelector('.notes-missing')!.textContent).toBe('dead link')
  })

  it('looks an HTML <img> up by its decoded src, which is how the server keys it too', () => {
    const root = notesRoot(
      '<p>hi</p><img src="https://example.com/b.png?x=1&amp;y=2" alt="b">',
      { 'https://example.com/b.png?x=1&y=2': FILE },
    )
    expect(root.querySelector('img')!.getAttribute('src')).toBe(FILE)
  })

  it('routes images through /proxy only for a card with no gallery map (a Discover preview)', () => {
    const root = notesRoot('![a](https://saucepan.ai/cdn/x/card)')
    expect(root.querySelector('img')!.getAttribute('src')).toBe(
      '/proxy/https%3A%2F%2Fsaucepan.ai%2Fcdn%2Fx%2Fcard',
    )
  })

  it('opens every link in a new tab', () => {
    const root = notesRoot('See [the source](https://example.com/page).', {})
    const a = root.querySelector('a')!
    expect(a.getAttribute('href')).toBe('https://example.com/page')
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('is page content, not a frame, and still strips anything active', () => {
    const { container, getByTestId } = render(
      <CreatorNotes
        notes={
          '<p onclick="x()">hi</p><script>alert(1)</script><iframe src="https://e.com"></iframe>'
        }
        media={{}}
      />,
    )
    const root = getByTestId('creator-notes').shadowRoot!
    expect(container.querySelector('iframe')).toBeNull()
    expect(root.querySelector('script, iframe')).toBeNull()
    expect(root.querySelector('p')!.hasAttribute('onclick')).toBe(false)
  })

  it("keeps a card's <style> inside the shadow root, minus any rule aimed at the host", () => {
    const root = notesRoot(
      '<style>:host { position: fixed } .x { color: red }</style><p class="x">hi</p>',
      {},
    )
    // [0] is the base sheet; [1] is the card's own.
    const cardStyle = root.querySelectorAll('style')[1].textContent!
    expect(cardStyle).not.toContain(':host')
    expect(cardStyle).toContain('.x { color: red }')
  })

  it('still renders markdown bold/images when the notes contain angle-bracket text that is not a real tag', () => {
    // Real JanitorAI creator notes (dezea's cards) advertise LoreBary plugin
    // tokens like `<SYMBOLS=943F533B>` and a `<blank>` scenario placeholder.
    // Neither is an HTML tag; a naive "has a <...>" sniff misread the whole
    // note as HTML and skipped markdown conversion entirely, leaving the
    // literal `**` and `![]()` on the page.
    const root = notesRoot(
      'Scenario 10: <blank>\n\n**Akane Kujo:**\n\n![a](https://example.com/a.png)\n\n<SYMBOLS=943F533B> more text',
      { 'https://example.com/a.png': FILE },
    )
    expect(root.textContent).toContain('<blank>')
    expect(root.querySelector('strong')!.textContent).toBe('Akane Kujo:')
    expect(root.querySelector('img')!.getAttribute('src')).toBe(FILE)
    expect(root.textContent).toContain('<SYMBOLS=943F533B>')
  })
})
