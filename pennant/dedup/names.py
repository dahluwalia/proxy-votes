"""Name parsing and comparison for matching notice names to Companies House records.

Design choice: no similarity scores and no nickname list here. A pair of names gets
one of five levels from explicit rules, so every level can be explained in one sentence:

    exact > variant > initial > surname_only > conflict

- Titles (Dr, Dame, Sir ...) and honours or degrees (CBE, FCA, BSc (Hons) ...) are
  stripped; accents and case are folded; hyphens split tokens ("Jean-Michel").
- The surname is known for CH formats ("SURNAME, Forenames" or "Forenames SURNAME");
  otherwise every token suffix is tried, so "Edward Bonham Carter" matches
  "BONHAM CARTER, Edward".
- The level comes from the first given names. Middle names can only veto (two
  different stated middle names are a conflict); a missing one never hurts.
- `variant`: same surname, and the first given names are different full names
  ("Gerry" vs "Gerard", "Nick" vs "Anthony Nicholas", but also "Ken" vs "Gerard"). It
  says the names could be one person, not that they are: anchor.py ranks the variants
  among one issuer's directors (3-gram TF-IDF, or a listed nickname from
  `dedup/nicknames.py`) and accepts only a clear winner.
- Middle names can only veto: two stated middle names that are neither equal nor an
  initial of each other are a conflict.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Level = Literal["exact", "variant", "initial", "surname_only", "conflict"]
LEVELS: tuple[Level, ...] = ("exact", "variant", "initial", "surname_only", "conflict")

TITLES = frozenset(
    "dr dame sir lord lady prof professor mr mrs ms miss mx baroness baron the rt right "
    "hon honourable rev reverend".split()
)
POSTNOMINALS = frozenset(
    "cbe obe mbe dbe kbe gbe cb cvo lvo kcmg cmg kcb gcb frs freng fmedsci frsa fca aca "
    "fcca acca fcma acma cima cpa cfa ceng bsc ba ma msc mba phd dphil llb llm meng beng "
    "hons qc kc jp dl mp frics fcis frse frcp fiet fcmi finstd".split()
)

_SPLIT = re.compile(r"[\s\-‐-―.]+")  # whitespace, hyphens and dashes, full stops
_STRIP = re.compile(r"[^0-9a-z]")


# Letters NFKD can't split into a base letter plus an accent, so they would otherwise
# survive folding ('søren') or be stripped by _key ('sren'). Spelled out instead.
_LETTERS = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
    "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "þ": "th", "Þ": "TH",
    "ı": "i",
})


def fold(text: str) -> str:
    """Accent- and case-folded text: 'André' -> 'andre', 'Søren' -> 'soren'. Also useful
    for name lookup."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_LETTERS))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _key(raw_token: str) -> str:
    return _STRIP.sub("", fold(raw_token))


def _is_upper_word(raw_token: str) -> bool:
    letters = [c for c in raw_token if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def is_title_or_honour(raw_token: str) -> bool:
    """'Dame', 'OBE', 'BSc', '(Hons)': words that are never part of a person's name."""
    return _key(raw_token) in TITLES | POSTNOMINALS


def _tokens(raw_tokens: list[str]) -> list[str]:
    out = []
    for tok in raw_tokens:
        out.extend(k for k in (_key(p) for p in _SPLIT.split(tok)) if k)
    return out


@dataclass(frozen=True)
class ParsedName:
    raw: str
    tokens: tuple[str, ...]  # folded name tokens, titles and honours removed
    surname_len: int | None  # known from the CH format; None means "try every suffix"

    def splits(self) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
        """Candidate (given names, surname) splits, the single-token surname first."""
        t = self.tokens
        if not t:
            return []
        if self.surname_len:
            return [(t[: -self.surname_len], t[-self.surname_len :])]
        return [(t[:i], t[i:]) for i in range(len(t) - 1, 0, -1)] or [((), t)]

    @property
    def given(self) -> tuple[str, ...]:
        s = self.splits()
        return s[0][0] if s else ()

    @property
    def surname(self) -> str:
        s = self.splits()
        return " ".join(s[0][1]) if s else ""


def parse(name: str) -> ParsedName:
    """Parse a notice name ("Dr Gerry Murphy") or a CH name ("MURPHY, Gerard" or
    "Gerard MURPHY")."""
    parts = [p.strip() for p in name.split(",")]
    rest = " ".join(parts[1:]).split()
    if len(parts) > 1 and any(not is_title_or_honour(t) for t in rest):
        # "SURNAME, Forenames[, Title]": drop titles and honours anywhere in the forenames,
        # and in the surname part ("NEBHRAJANI OBE") unless nothing else is left ("MA").
        head = parts[0].split()
        surname = _tokens([t for t in head if not is_title_or_honour(t)] or head)
        given = _tokens([t for t in rest if not is_title_or_honour(t)])
        return ParsedName(name, tuple(given + surname), len(surname) or None)

    raw = name.replace(",", " ").split()
    while len(raw) > 1 and _key(raw[0]) in TITLES:
        raw.pop(0)
    while len(raw) > 2 and _key(raw[-1]) in POSTNOMINALS:
        raw.pop()

    # "Forenames SURNAME" (CH search results): a trailing run of upper-case words.
    run = 0
    while run < len(raw) and _is_upper_word(raw[-1 - run]):
        run += 1
    surname_len = len(_tokens(raw[-run:])) if 0 < run < len(raw) else None
    return ParsedName(name, tuple(_tokens(raw)), surname_len)


def _token_level(x: str, y: str) -> Level:
    if x == y:
        return "exact"
    if len(x) == 1 or len(y) == 1:
        return "initial" if x[0] == y[0] else "conflict"
    return "variant"  # two different full names: for the ranker, never decided here


def _compatible(x: str, y: str) -> bool:
    """Middle names agree when equal or one is the other's initial."""
    return _token_level(x, y) in ("exact", "initial")


def _fits_inside(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    """Every name in `short` agrees, in order, with some name in `long`."""
    it = iter(long)
    return all(any(_compatible(s, t) for t in it) for s in short)


def _given_level(ga: tuple[str, ...], gb: tuple[str, ...]) -> Level:
    if not ga or not gb:
        return "surname_only"
    first = _token_level(ga[0], gb[0])
    if first == "conflict":  # mismatched initials: a full first name may be a later forename
        later = (len(ga[0]) > 1 and any(len(t) > 1 for t in gb[1:])) \
            or (len(gb[0]) > 1 and any(len(t) > 1 for t in ga[1:]))
        return "variant" if later else "conflict"
    if first == "variant":
        return first  # the ranker scores every forename, middle ones included
    ma, mb = ga[1:], gb[1:]
    short, long = (ma, mb) if len(ma) <= len(mb) else (mb, ma)
    return first if _fits_inside(short, long) else "conflict"


def better(a: Level, b: Level) -> Level:
    return a if LEVELS.index(a) <= LEVELS.index(b) else b


def at_least(level: Level, threshold: Level) -> bool:
    """at_least("exact", "variant") is True: exact is as good as variant or better."""
    return LEVELS.index(level) <= LEVELS.index(threshold)


def compare(a: str | ParsedName, b: str | ParsedName) -> Level:
    """How well two names match: the best level over all surname splits whose surnames
    are equal (ignoring spaces and hyphens). Different surnames are a conflict."""
    pa = a if isinstance(a, ParsedName) else parse(a)
    pb = b if isinstance(b, ParsedName) else parse(b)
    best: Level = "conflict"
    for ga, sa in pa.splits():
        for gb, sb in pb.splits():
            if "".join(sa) == "".join(sb):
                best = better(best, _given_level(ga, gb))
    return best


def aligned(a: str | ParsedName, b: str | ParsedName) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The two given-name tuples under the surname split `compare` scored best, for the
    ranker; empty tuples when the surnames never agree."""
    pa = a if isinstance(a, ParsedName) else parse(a)
    pb = b if isinstance(b, ParsedName) else parse(b)
    found = [(LEVELS.index(_given_level(ga, gb)), ga, gb)
             for ga, sa in pa.splits() for gb, sb in pb.splits() if "".join(sa) == "".join(sb)]
    if not found:
        return (), ()
    _, ga, gb = min(found, key=lambda x: x[0])  # first split wins a tie, as in compare
    return ga, gb
