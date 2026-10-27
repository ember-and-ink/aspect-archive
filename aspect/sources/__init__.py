"""Per-site adapters. One module per place a card can come from, each holding
everything this project knows about that site's shapes: field names, obfuscation,
avatar CDN conventions, what counts as a real greeting there.

Every module here maps its site's payload onto the neutral types in
`aspect.cards.models` -- so nothing downstream of a mapper has to know which site
a card came from. The dependency runs one way: sources import cards, never the
reverse.
"""
