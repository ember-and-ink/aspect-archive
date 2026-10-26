import { useLayoutEffect, useMemo, useRef } from 'react'
import DOMPurify from 'dompurify'

/**
 * Creator notes, rendered into a shadow root.
 *
 * Creator notes are untrusted third-party HTML. Two things keep them in their
 * place:
 *
 *  1. DOMPurify strips scripts, event handlers and dangerous tags/protocols,
 *     and the app shell's CSP (`script-src 'self'`, proxy/server.py) stops
 *     anything that slips past it from running. This is the content boundary.
 *  2. The shadow root is the style boundary: a card's `<style>` cannot restyle
 *     the app, and the app's styles do not leak into the notes. `contain:
 *     paint` on the host clips anything a card positions outside its box.
 *
 * It used to be a sandboxed iframe. That frame's opaque origin could not send
 * the login gate's session cookie, so every gated image 401'd, and on iOS a
 * pan that started inside it scrolled the frame instead of the page. The
 * notes are now ordinary page content with a natural height.
 *
 * Images come from the card's own gallery and nowhere else (`media`, from the
 * detail response): an archived card's notes never hotlink the source site,
 * whose uptime is not ours. A URL with no gallery file shows as a placeholder
 * saying so; Localize fetches it. A Discover preview is the one exception --
 * it is not archived, has no gallery, and passes no `media` -- so its images
 * go through `/proxy`.
 */

/** DOMPurify config: permissive for rich styling, closed to anything active. */
const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'p',
    'br',
    'hr',
    'div',
    'span',
    'strong',
    'b',
    'em',
    'i',
    'u',
    's',
    'strike',
    'del',
    'ins',
    'mark',
    'a',
    'img',
    'ul',
    'ol',
    'li',
    'blockquote',
    'pre',
    'code',
    'table',
    'tr',
    'td',
    'th',
    'thead',
    'tbody',
    'tfoot',
    'caption',
    'colgroup',
    'col',
    'center',
    'font',
    'sub',
    'sup',
    'small',
    'big',
    'details',
    'summary',
    'abbr',
    'cite',
    'q',
    'dl',
    'dt',
    'dd',
    'figure',
    'figcaption',
    'article',
    'section',
    'aside',
    'header',
    'footer',
    'nav',
    'main',
    'address',
    'time',
    'ruby',
    'rt',
    'rp',
    'bdi',
    'bdo',
    'wbr',
    'style',
  ],
  ALLOWED_ATTR: [
    'href',
    'src',
    'alt',
    'title',
    'class',
    'id',
    'style',
    'target',
    'width',
    'height',
    'align',
    'valign',
    'border',
    'colspan',
    'rowspan',
    'color',
    'face',
    'size',
    'start',
    'type',
    'value',
    'reversed',
    'dir',
    'lang',
    'rel',
  ],
  FORBID_TAGS: [
    'script',
    'iframe',
    'object',
    'embed',
    'form',
    'input',
    'button',
    'select',
    'textarea',
    'meta',
    'link',
    'base',
    'noscript',
  ],
  ALLOW_DATA_ATTR: true,
  ALLOW_UNKNOWN_PROTOCOLS: false,
}

/** Percent-encodes a URL for `/proxy/{url}`, matching `proxyEncode` in the
 *  legacy web/ frontend (web/library-sections/30-media-localization-feature.js)
 *  -- `encodeURIComponent` alone leaves `!'()*` unescaped, and cors_proxy.py
 *  reads the whole thing back as one path segment. */
function proxyEncode(url: string): string {
  return encodeURIComponent(url).replace(
    /[!'()*]/g,
    (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase(),
  )
}

/** Source URL -> gallery file URL, or null for a URL known dead. */
export type MediaMap = Record<string, string | null>

/** What stands in for an image the gallery does not hold. Keeps the alt text,
 *  so a captioned portrait still says what it was. */
function placeholder(img: Element, dead: boolean): HTMLElement {
  const box = document.createElement('span')
  box.className = 'notes-missing'
  const alt = img.getAttribute('alt')?.trim()
  box.textContent = `${alt ? `${alt} · ` : ''}${dead ? 'dead link' : 'not in gallery'}`
  box.title = img.getAttribute('src') ?? ''
  return box
}

/** Matches a CSS `url(...)` with an http(s) target; `data:` URIs make no
 *  request and are left as they are. */
const CSS_URL_RE = /url\(\s*(["']?)(https?:\/\/[^"')\s]+)\1\s*\)/gi

/** The CSS counterpart of the `<img>` pass: an image the gallery lacks becomes
 *  `none`, since there is no element to hang a placeholder on. */
function localizeCss(css: string, media: MediaMap | undefined): string {
  return css.replace(CSS_URL_RE, (_, _q, src: string) => {
    if (!media) return `url("/proxy/${proxyEncode(src)}")`
    const local = media[src]
    return local ? `url("${local}")` : 'none'
  })
}

/**
 * Point every image at its gallery file (or, with no `media`, at `/proxy`),
 * open every link in a new tab, and neutralise `:host` in any card `<style>`
 * -- a rule on the host itself is the one thing the shadow root does not
 * scope, and `contain: paint` only holds while the host stays where it is.
 */
function localize(fragment: DocumentFragment, media: MediaMap | undefined) {
  fragment.querySelectorAll('img[src]').forEach((img) => {
    const src = img.getAttribute('src')!
    if (!/^https?:\/\//i.test(src)) return
    if (!media) {
      img.setAttribute('src', `/proxy/${proxyEncode(src)}`)
      return
    }
    const local = media[src]
    if (local) img.setAttribute('src', local)
    else img.replaceWith(placeholder(img, local === null))
  })
  fragment.querySelectorAll('[style*="url("]').forEach((el) => {
    el.setAttribute('style', localizeCss(el.getAttribute('style')!, media))
  })
  fragment.querySelectorAll('a[href]').forEach((a) => {
    a.setAttribute('target', '_blank')
    a.setAttribute('rel', 'noopener noreferrer')
  })
  fragment.querySelectorAll('style').forEach((style) => {
    // An invalid selector drops its whole rule, which is the point.
    style.textContent = localizeCss(
      (style.textContent ?? '').replace(/:host(-context)?/g, ':not(*)'),
      media,
    )
  })
}

const BASE_STYLES = `
  :host {
    display: block; padding: 4px 2px;
    font: 14.5px/1.68 'Figtree', 'Segoe UI', system-ui, sans-serif;
    color: #d2d8da; overflow-wrap: break-word;
  }
  a { color: #57c2a2; text-decoration: none; }
  a:hover { text-decoration: underline; }
  h1, h2, h3, h4, h5, h6 { color: #e9ebec; line-height: 1.3; margin: 18px 0 7px; }
  h1:first-child, h2:first-child, h3:first-child { margin-top: 0; }
  p { margin: 0 0 11px; }
  ul, ol { margin: 0 0 11px 20px; padding: 0; }
  li { margin: 3px 0; }
  img, video { max-width: 100%; height: auto; border-radius: 10px; margin: 8px 0; }
  blockquote { border-left: 2px solid #57c2a2; margin: 14px 0; padding: 2px 0 2px 14px; color: #c3cacd; }
  pre { background: rgba(0,0,0,.3); padding: 10px; border-radius: 8px; overflow-x: auto; white-space: pre-wrap; }
  code { background: rgba(0,0,0,.3); padding: 2px 6px; border-radius: 5px; font-family: 'JetBrains Mono', monospace; font-size: .9em; }
  table { width: 100%; border-collapse: collapse; margin: 10px 0; }
  td, th { padding: 7px 11px; border: 1px solid rgba(255,255,255,.1); }
  .notes-missing {
    display: inline-block; margin: 8px 0; padding: 6px 10px; border-radius: 8px;
    border: 1px dashed rgba(255,255,255,.18); color: #8b9599; font-size: 12.5px;
  }
  [style*="position:fixed"], [style*="position: fixed"],
  [style*="position:sticky"], [style*="position: sticky"] { position: static !important; }
`

/** Real HTML tag names only -- matches PURIFY_CONFIG.ALLOWED_TAGS so a hit
 *  here is guaranteed to survive sanitization as a structural element. */
const HTML_TAG_PATTERN = new RegExp(
  `<\\/?(?:${PURIFY_CONFIG.ALLOWED_TAGS.join('|')})(?:[\\s/>]|$)`,
  'i',
)

/**
 * Whether the notes look like HTML or markdown/plain text.
 *
 * Cards from Chub carry real HTML documents; the JanitorAI/saucepan/datacat
 * mappers flatten to markdown. A markdown body rendered as HTML as-is
 * would show its literal `**` and `#`, so it is escaped and given minimal
 * block/inline formatting first.
 *
 * A naive "contains `<...>`" test false-positives on markdown notes that use
 * angle brackets as plain text -- e.g. JanitorAI creator notes advertising
 * plugin tokens like `<SYMBOLS=943F533B>` or a placeholder `<blank>` --
 * which sent real markdown down the raw-HTML path unconverted. Requiring the
 * bracketed name to be a real tag (from the same whitelist DOMPurify uses)
 * avoids that: those tokens aren't HTML tags, so they fall through to
 * markdownToHtml and render as literal, escaped text instead.
 */
function looksLikeHtml(notes: string): boolean {
  return HTML_TAG_PATTERN.test(notes)
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** Just enough markdown to read a note: paragraphs, headings, bold, italics,
 *  links, images. Images must be matched before links -- `![alt](url)`
 *  contains a valid `[alt](url)` link, and the link pattern would otherwise
 *  eat it first and leave a stray `!` beside an anchor instead of a picture.
 *  `html_to_md` (proxy/text/html_md.py) is what produces this syntax for every
 *  source that flattens creator notes to markdown, so an image-bearing note
 *  from any of them depends on this ordering to render at all. */
function markdownToHtml(md: string): string {
  const inline = (line: string) =>
    escapeHtml(line)
      .replace(
        /!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g,
        '<img src="$2" alt="$1" loading="lazy">',
      )
      .replace(
        /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>',
      )
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
  return md
    .split(/\n{2,}/)
    .map((block) => {
      const heading = block.match(/^(#{1,6})\s+(.*)$/)
      if (heading)
        return `<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`
      return `<p>${inline(block).replace(/\n/g, '<br>')}</p>`
    })
    .join('')
}

export function CreatorNotes({
  notes,
  media,
}: {
  notes: string
  /** The card's gallery map; omitted for a card that is not archived. */
  media?: MediaMap
}) {
  const hostRef = useRef<HTMLDivElement>(null)

  const fragment = useMemo(() => {
    const html = looksLikeHtml(notes) ? notes : markdownToHtml(notes)
    // Straight to a fragment: serializing the sanitized tree and parsing it
    // again is where mutation-XSS lives.
    const clean = DOMPurify.sanitize(html, {
      ...PURIFY_CONFIG,
      RETURN_DOM_FRAGMENT: true,
      // Otherwise a leading <style> is hoisted into <head> and dropped.
      FORCE_BODY: true,
    })
    localize(clean, media)
    return clean
  }, [notes, media])

  useLayoutEffect(() => {
    const host = hostRef.current!
    const root = host.shadowRoot ?? host.attachShadow({ mode: 'open' })
    const style = document.createElement('style')
    style.textContent = BASE_STYLES
    root.replaceChildren(style, fragment.cloneNode(true))
  }, [fragment])

  return (
    <div
      ref={hostRef}
      data-testid="creator-notes"
      className="rounded-xl border border-line-soft bg-surface"
      // Inline, so no rule from inside the shadow root can outrank them.
      style={{ display: 'block', position: 'relative', contain: 'paint' }}
    />
  )
}
