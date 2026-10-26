"""The card domain: what a character card *is* (`models`), how one is assembled
from mapped source fields (`builder`), how one is written to and edited on disk
(`edit`, `pngtools`), and the pieces that hang off it (`gallery`, `lorebook`,
the two avatar modules).

Everything here is source-agnostic. Per-site knowledge -- what JanitorAI calls a
greeting, how saucepan obfuscates a definition -- lives in `aspect.sources` and is
mapped to these types before it arrives.
"""
