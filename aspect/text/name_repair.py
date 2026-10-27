"""Diagnose (and propose fixes for) card names that are not actually names.

SillyTavern injects `data.name` into the prompt as the character's identity --
literally "You are <name>". So a card named `Naomi  your broke roommate started
an Onlyfans` tells the model its name is that whole sentence, and a card named
`Narrator` tells it that its name is Narrator even when the definition is
plainly about one specific woman. Both wreck the roleplay in ways that only
surface long after import.

Two distinct defects, handled by two stages:

  1. JUNK -- the name contains a real name *plus* marketing copy:
     `Malinda (Your Adopted Wolfgirl)`, `Mia, your desperate roommate`,
     `At Your Mercy | Cynthia`, `Airi - Between Hope and Regret`. The fix is
     string surgery: split on separators, keep the segments that look like
     names, drop the ones that look like taglines. Note the segment order is
     NOT fixed -- JanitorAI creators write both `Name | Tagline` and
     `Tagline | Name` -- so each segment is judged on its own merits rather
     than by position.

  2. GENERIC / TITLE -- the name carries no identity at all: `Narrator`,
     `Chatbot`, or a pure scenario title like `Best Friend Gets Walked In On`.
     Nothing can be salvaged from the string, so the real name has to be
     recovered from the definition body.

Both stages lean on the same engine: `roster()`, which ranks the capitalized
tokens of a card body by how much they behave like a character name. The
signals, strongest first:

  * lowercase elsewhere -- a real name is essentially never written lowercase
    in the same document, while `Queen` / `Court` / `Gym` / `However` are. This
    single test does most of the work and is what separates a name from a
    capitalized common noun.
  * possessive use -- "Ayame's eyes" is overwhelmingly name-like.
  * frequency -- the dominant character is talked about most.
  * early definition -- creators define the lead character first, so an early
    first-mention breaks ties between co-stars.

Accuracy, measured against real human judgements rather than a proxy: replaying
the 267 decisions in logs/name_repair.jsonl (`make names ARGS=--rejudge`) the
rules now propose exactly what the human chose 80% of the time -- JUNK 85%,
TITLE 74%, GENERIC 63%. An older estimate of 87%, taken by treating the
archive's single-token names as ground truth, is measurably too kind; prefer the
replay.

Cards whose definition never writes the name at all (entirely `{{char}}`-based)
are unrecoverable by any text rule, and are also by construction never the
broken ones: if the name field is wrong, the creator had to have written the
real name somewhere in the definition.

Four cards in five is good enough to propose and far too low to apply blindly,
so nothing here renames a card on its own -- `diagnose()` returns ranked
suggestions for a human to confirm. See scripts/fix_names.py.
"""

from __future__ import annotations

import collections
import re
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

# Common English words. Capitalized at the start of a sentence, they would
# otherwise pollute the roster ("However", "Since", "Whatever").
_STOP = set(
    """
a an the and but or so if then than that this these those there here when while where what
which who whom whose why how all any both each few more most other some such no nor not only
own same too very can will just should now it its he she they them their his her you your
yours i me my mine we us our ours him hers of for from to in on at by into onto with without
is are was were be been being has have had do does did would could may might must shall
however since never whatever although though because before after until once again always
sometimes often rarely still yet already almost nearly perhaps maybe instead besides
meanwhile later soon today tonight tomorrow yesterday finally suddenly slowly quickly
everyone everything someone something anyone anything nothing nobody somebody everybody
one two three four five six seven eight nine ten first second third last next another
yes yeah okay ok oh ah hey hi hello well please thanks thank sorry wait stop
good bad great better best worse worst new old young little big small long short high low
right left true false real fine sure much many less least even ever
come go get got let make made take took give gave look looked see saw know knew think
thought feel felt want wanted need needed say said tell told ask asked turn turned move
moved keep kept put set run ran walk walked stand stood sit sat lie lay find found leave
hold held pull push open close start started end ended begin began try tried use used
speak spoke talk talked call called work worked live lived love loved like liked
each every either neither several during through across against between among around
behind below above under over
""".split()
)

# Structural / jargon words that card definitions capitalize by convention:
# section headers, RP scaffolding, generic role nouns.
_JARGON = set(
    """
age name role gender species height build appearance personality background backstory body
hair eyes skin face voice speech style clothing outfit occupation job likes dislikes goals
fears quirks habits traits core scenario setting premise rules rule guardrails guardrail
system priority perspective details detail engagement characterization summary notes note
overview info information basic character characters narrator narration bot chatbot story
world plot arc scene scenes beat beats example examples dialogue dialog intro greeting
greetings tags tag creator user assistant human prompt output format response responses
message messages chat roleplay instructions instruction definition definitions ooc npc npcs
ai adult female male woman man girl girls boy boys women men people person child children
human humans mother father sister brother daughter son friend wife husband parents family
current status public private true hidden secret main primary key important additional
section part chapter step phase level rank type kind sort form state mode
""".split()
)

# Second-person / role vocabulary that marks a name segment as marketing copy.
_DESCRIPTOR_WORDS = set(
    """
your you yours my me our his her their its a an the this that these those
is are was were be been being has have had who whom whose which when where while
with without and or but of for from to in on at by into onto not no never always
gets becomes needs wants tries makes takes lets doesn dont doesnt cant wont
""".split()
)

_ROLE_WORDS = set(
    """
roommate girlfriend boyfriend sister brother mother father stepsister stepbrother
step-sister step-brother mom dad wife husband friend bestfriend best-friend classmate
neighbor neighbour maid nurse teacher student bully crush ex daughter son aunt uncle
cousin boss coworker landlord tenant princess queen king prince knight witch vampire
werewolf wolfgirl catgirl foxgirl succubus demon angel elf lamia android ghost milf
waitress secretary assistant babysitter therapist doctor lawyer stripper
""".split()
)

# Lowercase words that legitimately sit inside a person's name.
_NAME_PARTICLES = set(
    """
van von de del della di da du dos das la le les bin ibn al ap mac mc ben ter ten af av
""".split()
)

# Honorifics are part of the name when a name follows (`Princess Xandra`), so
# they must not be scored as the role nouns they otherwise are.
_HONORIFICS = set(
    """
mr mrs ms miss dr doctor professor prof sir madam lady lord king queen prince princess
duke duchess baron baroness count countess captain commander colonel major general
sergeant officer detective father mother sister brother saint lieutenant lt admiral
""".split()
)

# Ranks that are also perfectly good common nouns, so they only read as part of
# the name in the one position where they cannot be anything else: leading, with
# a capitalized non-role word straight after. `Knight Ilya` is the measured case
# (it was condemned as a scenario title); the rest are its near neighbours.
#
# The gate is what matters. Without the leading-position requirement `Lewd Witch
# Liz` stops resolving to `Liz`, and without the capitalized-follower one a
# scenario title like `Catgirl Clan` starts passing as a name.
_LEADING_RANKS = set(
    """
knight dame chief elder master mistress agent warden sheriff deputy marshal
inspector constable governor mayor judge matron ranger
""".split()
)

# Names that carry no identity -- the card needs a real one recovered from the body.
GENERIC_NAMES = {
    "narrator",
    "narator",
    "naratorr",
    "chatbot",
    "bot",
    "character",
    "story",
    "npc",
    "gm",
    "system",
    "assistant",
    "ai",
    "unknown",
    "untitled",
    "oc",
    "rp",
    "roleplay",
}

# Explicit separators, plus a run of 2+ spaces: JanitorAI names routinely lose a
# glyph there, leaving a bare gap that is really a separator
# (`Naomi  your broke roommate...`, `Past regrets  Chloe`).
#
# The comma is included, which is what lets `Mia, your desperate roommate` be
# repaired -- a genuine cast list (`Adelle, Leon, Nina`) survives because every
# one of its segments is judged name-like and they are rejoined afterwards.
_SEP = re.compile(r"\s*[|│┃‖/\\~•·]+\s*|\s+[-–—]+\s+|\s*[:;,]\s+|\s+[Vv][Ss]\.?\s+|\s{2,}")

_TOKEN = re.compile(r"\b([A-Za-z][a-zA-ZÀ-ɏ]{1,20})(['’]s)?\b")

_USER_SENTINEL = "\x01"
_CHAR_SENTINEL = "\x02"


# --------------------------------------------------------------------------
# Roster engine
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    """One ranked guess at the card's real character name."""

    token: str  # lowercase key
    display: str  # original capitalization, plus surname when confident
    score: float
    count: int  # capitalized occurrences in the body
    lowercase: int  # lowercase occurrences (a name should have ~none)
    possessive: int
    first_pos: float  # 0.0 = defined at the very top of the description
    surname: str | None = None


def _mask_macros(text: str) -> str:
    text = re.sub(r"\{\{\s*user\s*\}\}", _USER_SENTINEL, text or "", flags=re.I)
    return re.sub(r"\{\{\s*char\s*\}\}", _CHAR_SENTINEL, text, flags=re.I)


def _original_case(key: str, body: str) -> str:
    """Recover how the body actually capitalizes `key`.

    The *modal* capitalization, not the first one: a definition that shouts its
    subject in one header (`**AYAKO**`) and then writes `Ayako` 122 times means
    Ayako, and proposing `AYAKO` sent that card to a hand-typed correction. An
    all-caps form only wins when it is the only form there is, which is what
    leaves a genuine acronym (`MSSS`, `NAMRC`) alone.
    """
    forms = collections.Counter(
        m.group(1)
        for m in re.finditer(r"\b(" + re.escape(key) + r")\b", body, re.I)
        if m.group(1)[0].isupper()
    )
    if not forms:
        return key.capitalize()
    mixed = collections.Counter({f: n for f, n in forms.items() if not f.isupper()})
    return (mixed or forms).most_common(1)[0][0]


def roster(
    description: str = "",
    personality: str = "",
    scenario: str = "",
    first_mes: str = "",
    greetings: tuple[str, ...] = (),
    limit: int = 8,
) -> list[Candidate]:
    """Rank the capitalized tokens of a card body by how name-like they behave.

    The description/personality/scenario carry the definition (and position
    within them is meaningful -- the lead is defined first); greetings are
    scanned too so a character only *named* in the opening message is still
    found, but they do not affect the first-mention position.
    """
    definition = "\n".join(_mask_macros(t) for t in (description, personality, scenario))
    greeting = "\n".join(_mask_macros(t) for t in (first_mes, *greetings))

    count: collections.Counter[str] = collections.Counter()
    lower: collections.Counter[str] = collections.Counter()
    possessive: collections.Counter[str] = collections.Counter()
    first_at: dict[str, int] = {}
    follows: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    preceded: collections.Counter[str] = collections.Counter()

    for text, in_definition in ((definition, True), (greeting, False)):
        prev_token, prev_end = None, -1
        for m in _TOKEN.finditer(text):
            word, poss_suffix = m.group(1), m.group(2)
            key = word.lower()
            if key in _STOP:
                prev_token = None
                continue
            if word[0].isupper():
                count[key] += 1
                if poss_suffix:
                    possessive[key] += 1
                if in_definition:
                    first_at.setdefault(key, m.start())
                # A capitalized token immediately after another is a surname candidate.
                if prev_token and m.start() - prev_end <= 1:
                    follows[prev_token][key] += 1
                    preceded[key] += 1
                prev_token, prev_end = key, m.end()
            else:
                lower[key] += 1
                prev_token = None

    span = max(len(definition), 1)
    ranked: list[Candidate] = []
    for key, n in count.items():
        if n < 2 or len(key) < 3 or key in _JARGON:
            continue
        lc = lower[key]
        # The core test: a name is not also used as a common word in the same card.
        purity = n / (n + 3.0 * lc)
        if purity < 0.55:
            continue
        # A token that is nearly always the *trailing* half of a two-word name is
        # a surname, not a candidate in its own right -- it gets attached to its
        # first name below. Without this, the possessive on "Damian Thorne's"
        # credits Thorne and the card is proposed as "Thorne".
        if preceded[key] >= 0.75 * n:
            continue
        pos = min(first_at.get(key, span) / span, 1.0)
        score = (
            (n**0.5)
            * purity
            * (1 + 1.2 * min(possessive[key], 6) / 6)
            * (1.35 - 0.6 * pos)
        )
        ranked.append(
            Candidate(
                token=key,
                display=key,
                score=round(score, 2),
                count=n,
                lowercase=lc,
                possessive=possessive[key],
                first_pos=round(pos, 3),
            )
        )

    ranked.sort(key=lambda c: c.score, reverse=True)
    body = definition + "\n" + greeting
    out: list[Candidate] = []
    for cand in ranked[:limit]:
        surname = None
        if follows[cand.token]:
            token, hits = follows[cand.token].most_common(1)[0]
            if hits >= 2 and hits >= 0.25 * cand.count and token not in _JARGON and count[token] >= 2:
                surname = _original_case(token, body)
        cand.display = _original_case(cand.token, body)
        cand.surname = surname
        if surname:
            cand.display = f"{cand.display} {surname}"
        out.append(cand)
    return out


# --------------------------------------------------------------------------
# Name-string surgery
# --------------------------------------------------------------------------


def _strip_emoji(text: str) -> str:
    """Drop decorative symbols creators hang off a name (`Liz 🩸`)."""
    out = "".join(c for c in text if not unicodedata.category(c).startswith("So"))
    return re.sub(r"\s{2,}", " ", out).strip()


def _strip_edges(segment: str) -> str:
    return _strip_emoji(segment).strip(" \t'\"“”‘’.,!?*_-–—()[]")


def _tokens(segment: str) -> list[str]:
    return [w for w in re.split(r"\s+", segment.strip()) if w]


def name_score(segment: str) -> float:
    """Structural plausibility that `segment` is a person-name (or short name list).

    Positive means name-like. This is deliberately only half the decision --
    `Feral Yandere Kitsune` scores as well as `Ayame` structurally, so callers
    combine this with roster evidence from the card body.
    """
    segment = _strip_edges(segment)
    if not segment:
        return -10.0
    words = _tokens(segment)
    if not words:
        return -10.0

    bare = [w.lower().strip(".,()\"'’“”") for w in words]
    # A quoted epithet inside a name (`Cayla "Crimson Witch" Wise`) is part of
    # the name, so its words must not be judged as descriptor/role vocabulary.
    quoted = _quoted_positions(words)

    # "and"/"&" joining two names is fine: `Milly & Meryl`, `Aiko and Willow`.
    joiner = [w in ("and", "&", "+") and 0 < i < len(bare) - 1 for i, w in enumerate(bare)]
    # An honorific is part of the name when a name follows it. A bare rank
    # counts too, but only in first position and only ahead of a capitalized
    # word -- see `_LEADING_RANKS`.
    honorific = [
        w.rstrip(".") in _HONORIFICS and i < len(bare) - 1 for i, w in enumerate(bare)
    ]
    if (
        len(words) > 1
        and bare[0].rstrip(".") in _LEADING_RANKS
        and words[1][:1].isupper()
        and bare[1] not in _ROLE_WORDS
    ):
        honorific[0] = True

    # Joiners, honorifics, particles and quoted epithets are all name furniture:
    # they don't count toward length, so `Kara & Valeria & Elizabeth` is judged
    # as three words and `Vanessa "Vanny" di Notturni` as two.
    furniture = [
        joiner[i] or honorific[i] or quoted[i] or bare[i] in _NAME_PARTICLES
        for i in range(len(words))
    ]
    effective = max(len(words) - sum(furniture), 1)
    score = 1.0 if effective <= 3 else (-1.0 if effective <= 5 else -3.0)

    for i, w in enumerate(bare):
        if furniture[i]:
            continue
        if w in _DESCRIPTOR_WORDS:
            score -= 2.5
        # A role noun standing alone IS the name (`Angel`, `Ghost`, `Mom`);
        # it only reads as a descriptor next to other words.
        if w in _ROLE_WORDS and len(words) > 1:
            score -= 2.0

    for i, w in enumerate(words):
        core = w.strip(".,()\"'’“”")
        if furniture[i]:
            continue
        if (
            core
            and core[0].islower()
            and core.lower() not in _NAME_PARTICLES
            and not _has_inner_capital(core)
        ):
            score -= 2.0

    score += 0.5 * sum(
        1
        for w in words
        if w.strip("\"'“”‘’")[:1].isupper() or _has_inner_capital(w.strip(".,()\"'’“”"))
    )
    return score


def _has_inner_capital(word: str) -> bool:
    """A lowercase word carrying a capital further in is a name, not a lapse.

    `vestai-Drexar`, `d'Artagnan`, `al-Rashid`: the compound half that follows
    the hyphen or apostrophe is capitalized, which no ordinary lowercase word
    ever is. Without this the whole of `Lt. K'Lira vestai-Drexar` reads as a
    scenario title.
    """
    return bool(re.search(r"[\-'’][A-ZÀ-Þ]", word))


def _quoted_positions(words: list[str]) -> list[bool]:
    """Mark the words that sit inside a quoted epithet within a name."""
    inside, out = False, []
    for w in words:
        opens = len(re.findall(r'["“‘]', w))
        closes = len(re.findall(r'["”’]', w))
        was = inside
        if opens and not closes:
            inside = True
        elif closes and not opens:
            inside = False
        out.append(was or inside or bool(opens and closes and w.strip("\"'“”‘’")))
    return out


def split_name(raw: str) -> list[str]:
    """Split a card name into candidate segments on separators."""
    return [s.strip() for s in _SEP.split(raw or "") if s.strip()]


def _parenthetical_is_descriptive(inner: str, roster_tokens: frozenset[str]) -> bool:
    """True when a parenthetical is marketing copy rather than a nickname/alias.

    `Malinda (Your Adopted Wolfgirl)` / `Maia (Blackmailed Assailant)` -> drop.
    `Adeline (“Addie”)` / `Hana (Aiko)` / `... Complex (NAMRC)` -> keep.

    A bare adjective+noun role phrase (`Blackmailed Assailant`, `Caring Nurse`)
    is structurally indistinguishable from a two-word name, so the tie is broken
    on roster evidence: a real alias is written in the definition, a tagline is
    not.
    """
    inner = inner.strip()
    if not inner:
        return False
    if inner[0] in "\"“'‘":  # quoted nickname
        return False
    words = _tokens(inner)
    if len(words) == 1:
        return False  # single token: alias or acronym, not a tagline
    if _segment_tokens(inner) & roster_tokens:
        return False  # named in the definition -> a real alias
    return True


def strip_parentheticals(name: str, roster_tokens: frozenset[str] = frozenset()) -> str:
    """Drop descriptive parentheticals, keep nickname/alias ones."""

    def repl(m: re.Match[str]) -> str:
        return "" if _parenthetical_is_descriptive(m.group(1), roster_tokens) else m.group(0)

    return _balance_parens(re.sub(r"\s*\(([^)]*)\)", repl, name).strip())


def _segment_tokens(segment: str) -> frozenset[str]:
    """The comparable lowercase word tokens of a name segment."""
    return frozenset(
        t.lower().strip(".,()\"'’“”")
        for t in _tokens(_strip_edges(segment))
        if len(t.strip(".,()\"'’“”")) >= 3
    )


def _balance_parens(name: str) -> str:
    """Drop a bracket left unmatched by trimming, so we never emit `Maia (Foo`.

    Whitespace is deliberately left alone: a run of 2+ spaces is a separator
    signal that `split_name` still needs (`Naomi  your broke roommate...`).
    """
    for opener, closer in (("(", ")"), ("[", "]")):
        if name.count(opener) != name.count(closer):
            name = name.replace(opener, "").replace(closer, "")
    return name.strip()


# --------------------------------------------------------------------------
# Diagnosis
# --------------------------------------------------------------------------

OK = "ok"
JUNK = "junk"
GENERIC = "generic"
TITLE = "title"

# How many separate people the roster must find, at comparable strength, before
# we call a card a genuine ensemble/narrator piece and leave its name alone.
_ENSEMBLE_MIN = 4
_ENSEMBLE_RATIO = 0.45

# How deep to look when asking "is this word named anywhere in the definition?".
# Wide on purpose -- this is a membership test, not a ranking.
_GROUNDING_WIDTH = 60

# `X & Y` / `X and Y` joining two names, when the card is already being
# repaired. The capitalised lookahead is what makes it safe -- see the comment
# at the JUNK return in `diagnose`.
_JOINER = re.compile(r",?\s+(?:&|and)\s+(?=[A-Z])")

# The same joiner with a *lowercase* follower: `Himari and rikka`, `Melissa and
# lily`, `Thea and sybil` are all real two-hander rosters whose second name the
# creator just never shifted. Left alone the lowercase word reads as a defect,
# the whole segment is condemned and the card lands in TITLE with nothing
# usable -- all three had to be typed out by hand.
#
# The follower is only promoted when the body itself names it (see the roster
# grounding at the call site), which is what separates these from the archive's
# `Girlfriend and the Ex` and `Kira and others`: `the` never enters a roster,
# and `others` cannot, because it is written lowercase everywhere it appears.
_LOWER_JOINED = re.compile(r"\b[A-Z][\w'’-]*(?:\s+(?:&|and)\s+)([a-z][\w'’-]{2,})\b")

# A definition that opens with a labelled `Name:` / `Character:` field is the
# creator answering the question directly. `_SEP`-style structural judgement has
# no way to know that `Two of Five` is a Borg designation, `Kara Swift` a plain
# name written with a lowercase surname, or `“The Seduction Game” TV show` the
# literal identity the card wants -- but all three say so in their first line.
#
# The label sits at the start of a line, behind at most a little markdown or
# bracket noise: `Name:`, `**Name:**`, `>**Name:**`, `- Full Name =`,
# `[Character= ...`.
_DECLARED_NAME = re.compile(
    r"^[^\w\n]{0,4}(?:(?:full |character |char |real |true )?name|character)"
    r"[*_\]\s]{0,3}[:=][ \t]*(.{1,80})",
    re.I | re.M,
)

_WORDS = re.compile(r"[a-zA-Z0-9À-ɏ]+")

_NARRATOR_MARKERS = re.compile(
    r"\b(?:omniscient narrator|ensemble (?:narrator|cast)|GM/narrator|game master|"
    r"narrates? (?:the|all)|plays? all (?:the )?(?:background )?(?:NPCs|characters)|"
    r"voices the (?:crew|world)|all the NPCs|controlling the narrative|"
    # World/scenario cards state their own nature outright: "Narrator is not a
    # single person but the entire world of...", "{{char}} is not a single
    # character, but the name of the roleplay scenario". Missing this renamed a
    # whole confederation to "Chief". Note the near-miss that is NOT safe to
    # match: "he represents the world of duty she cannot escape" is ordinary prose.
    r"not a single (?:person|character|speaker|individual|entity|NPC)|"
    # `Last Man on Earth` proposed "Progress" because nothing marked it as the
    # narrator card it says it is ("You are simply a narration voice that sets
    # the scene"). `narration voice` hits 4 archive cards, 3 of them already
    # suspected ensembles; the obvious wider phrasing `storyteller` was REJECTED
    # on the same measurement -- 13 cards, most of them ordinary named characters.
    r"narration voice|"
    r"multi-?character)\b",
    re.I,
)


@dataclass
class Diagnosis:
    """What is wrong with a card's name, and what to do about it."""

    raw: str
    verdict: str  # OK | JUNK | GENERIC | TITLE
    suggestion: str | None  # best single proposal, None when nothing is confident
    candidates: list[Candidate] = field(default_factory=list)
    kept_segments: list[str] = field(default_factory=list)
    dropped_segments: list[str] = field(default_factory=list)
    ensemble: bool = False  # looks like a genuine multi-character / narrator card
    reason: str = ""

    @property
    def needs_review(self) -> bool:
        return self.verdict != OK


def _recase_shouted(name: str, body: str) -> str:
    """Give a SHOUTED word back the capitalization the definition uses for it.

    `RILEY: Your Best Friend... | Gamer Girl` keeps the segment `RILEY`, and the
    hand-typed correction was `Riley`. The body is the authority: a word is only
    re-cased when the definition itself writes a mixed-case form of it, which
    leaves a real acronym (`MSSS`, `NAMRC`) shouting as it should.

    Like the `&`/`and` joiner rule, this only runs on a name that is being
    repaired anyway -- an all-caps name is not a defect worth a review queue,
    and `ARIA` alone would have been seven cards of pure punctuation review.

    Whole words only. Matching bare runs of capitals instead mangles the
    stylised names this archive is full of: `3M1LY` -> `3M1Ly`, `AIko` ->
    `Aiko`, `Slave SQ3R9` -> `Slave Sq3R9`.
    """

    def repl(m: re.Match[str]) -> str:
        word = m.group(0)
        actual = _original_case(word.lower(), body)
        return word if actual.isupper() else actual

    return re.sub(r"\b[A-ZÀ-Þ]{2,}\b", repl, name)


def _shift_joined_name(name: str, body: str, roster_tokens: set[str]) -> str:
    """Capitalize a grounded name sitting lowercase after a joiner.

    `Himari and rikka` -> `Himari and Rikka`, which the ordinary joiner rule
    then finishes into `Himari, Rikka`. See `_LOWER_JOINED` for why grounding
    is the gate.
    """

    def repl(m: re.Match[str]) -> str:
        token = m.group(1)
        if token.lower() not in roster_tokens:
            return m.group(0)
        return m.group(0)[: m.start(1) - m.start(0)] + _original_case(token.lower(), body)

    return _LOWER_JOINED.sub(repl, name)


def _word_seq(text: str) -> list[str]:
    return [w.lower() for w in _WORDS.findall(text or "")]


def _contiguous_in(needle: list[str], haystack: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[i : i + len(needle)] == needle for i in range(len(haystack) - len(needle) + 1)
    )


def declared_as_name(raw: str, body: str) -> bool:
    """True when the definition's own `Name:` field spells out this card name.

    The card name has to appear as a *contiguous* run of words inside the
    declared value, which is what keeps the test honest. A loose bag-of-words
    overlap would let a `Character:` field holding a sentence ("a shy girl who
    gets walked in on by her best friend") vouch for the scenario title `Best
    Friend Gets Walked In On`; requiring the phrase itself does not.

    A leading honorific or rank is dropped first -- creators routinely put the
    rank in the card name and leave it out of the field (`Lt. K'Lira
    vestai-Drexar` vs `Name: K'Lira vestai-Drexar`).

    The comparison is case-insensitive, because `Kara swift` against `Name: Kara
    Swift` is a name with a lowercase surname and not a defect. But a name whose
    *first* word is lowercase is broken however the field reads -- `kate` and
    `crybby.exe` both sit next to a definition that declares them -- so those
    are refused outright.
    """
    stripped = _strip_edges(raw)
    words = _word_seq(stripped)
    while len(words) > 1 and words[0] in _HONORIFICS:
        words = words[1:]
    if not words or not stripped[:1].isupper():
        return False
    return any(
        _contiguous_in(words, _word_seq(m.group(1)))
        for m in _DECLARED_NAME.finditer(body)
    )


def _looks_like_ensemble(cands: list[Candidate], body: str) -> bool:
    if _NARRATOR_MARKERS.search(body):
        return True
    if len(cands) < _ENSEMBLE_MIN:
        return False
    top = cands[0].score
    peers = sum(1 for c in cands[:6] if c.score >= _ENSEMBLE_RATIO * top)
    return peers >= _ENSEMBLE_MIN


def diagnose(card: dict, *, limit: int = 5) -> Diagnosis:
    """Classify a card's name and propose a repair.

    `card` is the `data` block of a chara_card_v3 (or a v2 card dict).
    """
    raw = (card.get("name") or "").strip()
    description = card.get("description") or ""
    personality = card.get("personality") or ""
    scenario = card.get("scenario") or ""
    first_mes = card.get("first_mes") or ""
    greetings = tuple(card.get("alternate_greetings") or ())

    # Two different uses of the roster, with two different widths:
    #   * `cands`  -- the short ranked list shown to a human as suggestions.
    #   * `roster_tokens` -- a wide net used only to answer "is this word named
    #     anywhere in the definition?". It must be generous: `Aria Calysa` is a
    #     real name whose surname never cracks any top-5.
    wide = roster(description, personality, scenario, first_mes, greetings, limit=_GROUNDING_WIDTH)
    cands = wide[:limit]
    body = "\n".join([description, personality, scenario, first_mes])
    ensemble = _looks_like_ensemble(cands, body)
    roster_tokens = {c.token for c in wide}

    # ---- stage 2 trigger: the name carries no identity at all ----------
    if not raw or raw.lower() in GENERIC_NAMES:
        best = cands[0].display if cands and not ensemble else None
        return Diagnosis(
            raw=raw,
            verdict=GENERIC,
            suggestion=best,
            candidates=cands,
            ensemble=ensemble,
            reason=(
                "generic placeholder name; card is a genuine narrator/ensemble piece"
                if ensemble
                else "generic placeholder name -- real character recovered from the definition"
            ),
        )

    # ---- stage 1: segment the string and keep the name-like parts ------
    cleaned = _shift_joined_name(strip_parentheticals(raw, roster_tokens), body, roster_tokens)
    segments = split_name(cleaned)
    # When the definition never names anyone (an all-`{{char}}` card), there is
    # no roster to ground against and grounding must not be used as a filter --
    # otherwise a perfectly good name gets shredded for lack of evidence.
    have_roster = bool(roster_tokens)

    # A single-segment name has no competing alternative, so the creator gets
    # the benefit of the doubt: it is judged on structure alone. Demanding body
    # evidence here would condemn real names the definition simply never spells
    # out (`Aria Calysa`, `Claire Rosewood`). Grounding only arbitrates when
    # there are rival segments to choose between.
    solo = len(segments) == 1

    kept, dropped = [], []
    for seg in segments:
        structural = name_score(seg)
        # Roster evidence promotes a borderline segment, but it must never
        # rescue an outright sentence: `...started an Onlyfans` is "grounded"
        # only because OnlyFans is named in the definition too.
        grounded = bool(_segment_tokens(seg) & roster_tokens)
        multiword = len(_tokens(_strip_edges(seg))) > 1
        if solo or not have_roster:
            keep = structural >= 1.0
        elif multiword:
            # `Toxic Stardom` / `Numb Feeling` are structurally identical to a
            # two-word name; only the definition can tell them apart.
            keep = grounded and structural >= -1.0
        else:
            keep = grounded or structural >= 1.0
        (kept if keep else dropped).append(seg)

    # Nothing name-like survived -> the whole name is a scenario title, unless
    # the definition declares this exact string as the character's name.
    if not kept:
        if declared_as_name(raw, body):
            return Diagnosis(
                raw=raw,
                verdict=OK,
                suggestion=None,
                candidates=cands,
                kept_segments=segments,
                ensemble=ensemble,
                reason="unusual, but the definition declares it as the name",
            )
        best = cands[0].display if cands and not ensemble else None
        return Diagnosis(
            raw=raw,
            verdict=TITLE,
            suggestion=best,
            candidates=cands,
            dropped_segments=dropped,
            ensemble=ensemble,
            reason="name is a scenario title, not a character name",
        )

    # Only whitespace/bracket tidying here -- `_strip_edges` is for *judging* a
    # segment, and would eat the quotes off `Adeline (“Addie”)`.
    rebuilt = ", ".join(_strip_emoji(k) for k in kept) if len(kept) > 1 else _strip_emoji(kept[0])
    rebuilt = re.sub(r"\s{2,}", " ", _balance_parens(rebuilt)).strip(" ,;:-–—")

    if rebuilt != raw:
        rebuilt = _recase_shouted(rebuilt, body)
        # A spaced `&` or `and` between kept names is a separator like any
        # other, so a roster being repaired anyway comes out reading
        # consistently: `Katya, Felicity, Sam & April` and `Bill, Bob and Jim`
        # both end up all-commas. Every hand-typed override of such a
        # suggestion in the decision log made exactly this edit (see
        # logs/name_repair.jsonl, --stats).
        #
        # Two things keep it safe. The next word must be capitalised, which is
        # what separates the archive's 68 real rosters (`Aiko and Willow`) from
        # `Girlfriend and the Ex` and `Kira and others`. And it is deliberately
        # NOT a defect on its own -- `Zoe & Lily` is a perfectly good name, and
        # tidying punctuation is not worth 139 cards of review queue. The
        # required spaces also leave an unspaced `R&B` alone.
        return Diagnosis(
            raw=raw,
            verdict=JUNK,
            suggestion=_JOINER.sub(", ", rebuilt),
            candidates=cands,
            kept_segments=kept,
            dropped_segments=dropped,
            ensemble=ensemble,
            reason="name carries a tagline/descriptor alongside the real name",
        )

    return Diagnosis(
        raw=raw,
        verdict=OK,
        suggestion=None,
        candidates=cands,
        kept_segments=kept,
        ensemble=ensemble,
        reason="name looks like a name",
    )
