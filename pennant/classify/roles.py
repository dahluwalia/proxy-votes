"""Role titles to board facts, and listed status from the Companies House company type.

Design choice: an ordered regex table, not a model. Phrases whose "chair", "board"
or "director" do not mean a board seat (committee chairs, advisory boards,
trustees) are cut out first. What is left is matched against the board-role rows,
and combinations that mean more are resolved explicitly ("Chair and Chief
Executive" is an executive chair). Unmatched text is `other` with every fact
unknown, so web research or the analyst settles it. Listed status is only ever
ruled `no`, from a UK private company type, never from a name suffix: Capri
Holdings Limited is NYSE-listed.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date
from typing import NamedTuple

from pennant.core.models import RoleType
from pennant.core.tri import NO, UNKNOWN, YES, Tri


class RoleFacts(NamedTuple):
    role_type: RoleType
    board_seat: Tri
    chair: Tri
    executive: Tri
    current: Tri


# role_type -> (board_seat, chair, executive): the M5.1 table.
FACTS: dict[RoleType, tuple[Tri, Tri, Tri]] = {
    RoleType.executive_chair: (YES, YES, YES),
    RoleType.chair: (YES, YES, NO),
    RoleType.deputy_chair: (YES, NO, NO),
    RoleType.non_executive_director: (YES, NO, NO),
    RoleType.executive_director: (YES, NO, YES),
    RoleType.director_unspecified: (YES, NO, UNKNOWN),
    RoleType.officer_title: (UNKNOWN, NO, YES),
    RoleType.committee_chair: (NO, NO, NO),
    RoleType.adviser: (NO, NO, NO),
    RoleType.trustee: (NO, NO, NO),
    RoleType.other: (UNKNOWN, UNKNOWN, UNKNOWN),
}

# Tense -> current. "future", "unclear" and anything else -> unknown.
TENSE: dict[str, Tri] = {"current": YES, "past": NO}

# CH company types that cannot have shares admitted to trading.
PRIVATE_TYPES = frozenset({
    "ltd",
    "private-unlimited",
    "private-unlimited-nsc",
    "private-limited-guarant-nsc",
    "private-limited-guarant-nsc-limited-exemption",
    "private-limited-shares-section-30-exemption",
    "llp",
    "limited-partnership",
    "scottish-partnership",
    "charitable-incorporated-organisation",
    "scottish-charitable-incorporated-organisation",
})

CHAIR = r"chair(?:man|woman|person)?"
_HOLDER = rf"(?:{CHAIR}|(?:deputy|vice) {CHAIR}|member|head|president)"
# "Chair of the X", or the comma / colon form "Chair, X" (punctuation is gone by now), where
# X may not start with "and": "Chair and Trustee" keeps its chair.
_OF = (rf"(?:\b{_HOLDER} (?:of (?:the )?(?:[a-z&]+ ){{0,4}}?"
       r"|(?!and\b|&)(?:[a-z&]+ ){0,4}?))?")
_BODY = r"(?:board|council|committee|panel|group|forum)s?"
_EXEC_COMMITTEE = r"(?:group )?(?:executive|management|operating) committee"
_WORDS = r"(?:(?!board\b)[a-z&]+ ){0,6}?"
# A committee's name: no "board" (Chair of the Board and its Nomination Committee) and no
# other role ("Chair of X plc and member of its Audit Committee" keeps the chair).
_CWORDS = r"(?:(?!board\b|members?\b|directors?\b|chair|of\b)[a-z&]+ ){0,6}?"
_NOT_EXEC = r"(?<!executive )(?<!management )(?<!operating )"

# Qualified phrases, cut out before the board rows. Each may carry "Chair of the ..." with it.
# A board committee chair or member holds no seat by that alone: the seat is the separate
# NED appointment. Committee members share committee_chair's facts (no, no, no). Executive
# and management committees are executive jobs instead (OFFICER below).
QUALIFIED: list[tuple[RoleType, re.Pattern[str]]] = [
    (RoleType.committee_chair, re.compile(
        rf"\b{CHAIR} (?:of )?(?!and\b|&)(?!(?:the )?{_EXEC_COMMITTEE})(?:the )?{_CWORDS}"
        r"committees?\b"
        rf"|(?:\b(?!executive |management |operating )[a-z&]+ )?{_NOT_EXEC}\bcommittees? "
        rf"{CHAIR}\b"
        rf"|\bmembers? of (?!(?:the )?{_EXEC_COMMITTEE})(?:the )?{_WORDS}committees?\b"
        rf"|{_NOT_EXEC}\bcommittees? members?\b")),
    (RoleType.adviser, re.compile(
        rf"{_OF}\b(?:[a-z]+ )?advisory {_BODY}(?: (?:{CHAIR}|member))?\b"
        rf"|{_OF}\bcouncil(?: (?:{CHAIR}|member))?\b"
        r"|\b(?:senior |special |strategic |independent )?advis[oe]rs?\b|\bmentors?\b")),
    (RoleType.trustee, re.compile(
        rf"{_OF}\b(?:board of )?trustees?(?: board)?(?: (?:{CHAIR}|member))?\b"
        r"|\b(?:honorary |senior )?fellow\b")),
]
# Chairing an executive or management committee is an executive job, not a board chair:
# keep the committee (an OFFICER cue) and drop the "chair".
EXEC_COMMITTEE_CHAIR = re.compile(
    rf"\b{CHAIR} (?:of )?(?:the )?({_EXEC_COMMITTEE})\b|\b({_EXEC_COMMITTEE}) {CHAIR}\b")

EXEC_CHAIR = re.compile(rf"\bexecutive {CHAIR}\b")
DEPUTY = re.compile(rf"\b(?:deputy|vice) {CHAIR}\b")
CHAIR_RE = re.compile(rf"\b{CHAIR}\b")
DESIGNATE = re.compile(rf"\b{CHAIR} (?:designate|elect)\b|\b(?:designate|incoming) {CHAIR}\b")
NED = re.compile(
    r"\bnonexecutive(?: (?:board member|director|member))?\b|\b(?:senior )?independent director\b"
    r"|\blead (?:independent )?director\b|\bsupervisory board\b")
EXEC_DIRECTOR = re.compile(r"\bexecutive director\b")
OFFICER = re.compile(
    r"\b(?:ceo|cfo|coo|cto|cio|cmo|cro|cpo|chro|evp|svp)\b"
    r"|\bchief (?:[a-z]+ ){0,3}?(?:officer|executive)\b"
    r"|\b(?:vice )?president\b|\bmanaging director\b|\bdirector general\b|\bpartner\b"
    r"|\b(?:executive|management|operating) committee\b"
    r"|\b(?:finance|financial|commercial|marketing|operations|operating|sales|technology"
    r"|technical|strategy|people|hr|legal|digital|retail) director\b")
# A board seat is only a bare "director" (alone or joined by "and") or an explicit board
# phrase. Any other "director" ("Director of Strategy", "Director, Corporate Affairs",
# "Regional Director") is a job title: an executive role, board seat unknown.
BOARD_PHRASE = re.compile(r"\bboard (?:member|director)s?\b|\bmembers? of (?:the )?board\b"
                          r"|\bdirectors? (?:of|on) (?:the )?board\b")
BARE_DIRECTOR = re.compile(r"(?:^|\b(?:and|&) )directors?(?= (?:and\b|&|since\b|from\b|\d)|$)")
DIRECTOR_WORD = re.compile(r"\bdirectors?\b")
NON_BOARD = re.compile(r"\bnon ?board\b")  # "Director (non-board)": board seat no
# A chair seat that starts later: future wording, or "with effect from <date>" after the AGM.
FUTURE_CHAIR = re.compile(r"\bdesignate\b|\belect\b|\bincoming\b|\bfrom the conclusion\b"
                          r"|\bwill (?:become|be|take|assume|succeed)\b")
EFFECT_FROM = re.compile(r"\b(?:with effect from|effective from|effective) (?:the )?(.*)")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_DATE = re.compile(rf"(?:(\d{{1,2}})(?:st|nd|rd|th)? (?:of )?)?(?:({'|'.join(_MONTHS)}) )?"
                   r"(?:(\d{1,2}) )?(\d{4})\b")
PAST_CUE = re.compile(r"\b(?:former|formerly|previously|retired)\b")


def normalize(text: str) -> str:
    """Lower-case ASCII words: accents folded, 'non-executive' as one word, punctuation
    (except &) to spaces."""
    t = re.sub(r"[‐-―−]", "-", unicodedata.normalize("NFKC", text))
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\bnon\s*-?\s*executive", "nonexecutive", t)
    t = re.sub(r"[^a-z0-9&]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_qualified(text: str) -> tuple[str, list[RoleType]]:
    found: list[RoleType] = []
    for role_type, pattern in QUALIFIED:
        text, n = pattern.subn(" ", text)
        if n:
            found.append(role_type)
    return text, found


def role_type_of(role_text: str | None) -> tuple[RoleType, bool]:
    """(role_type, is_chair_designate) for a role title."""
    text = NON_BOARD.sub(" ", normalize(role_text or ""))  # board no: set in classify_role
    text = EXEC_COMMITTEE_CHAIR.sub(lambda m: m[1] or m[2], text)
    text, qualified = _strip_qualified(text)
    officer = bool(OFFICER.search(text))
    deputy = bool(DEPUTY.search(text))
    chair = bool(CHAIR_RE.search(DEPUTY.sub(" ", text)))
    leftover = _squash(OFFICER.sub(" ", NED.sub(" ", EXEC_DIRECTOR.sub(" ", text))))
    board_phrase = bool(BOARD_PHRASE.search(leftover))
    leftover = _squash(BOARD_PHRASE.sub(" ", leftover))
    bare_director = board_phrase or bool(BARE_DIRECTOR.search(leftover))
    titled = bool(DIRECTOR_WORD.search(BARE_DIRECTOR.sub(" ", leftover)))
    designate = bool(DESIGNATE.search(text))
    if EXEC_CHAIR.search(text) or (chair and officer):
        return RoleType.executive_chair, False
    if deputy:
        return RoleType.deputy_chair, False
    if chair:
        return RoleType.chair, designate
    if EXEC_DIRECTOR.search(text) or (bare_director and officer):
        return RoleType.executive_director, False
    if NED.search(text):
        return RoleType.non_executive_director, False
    if officer or titled:
        return RoleType.officer_title, False
    if bare_director:
        return RoleType.director_unspecified, False
    return (qualified[0] if qualified else RoleType.other), False


def _date_span(text: str) -> tuple[date, date] | None:
    """Earliest and latest day a '1 May 2026' / 'May 2026' / '2026' phrase can mean."""
    m = _DATE.match(text)
    try:
        if not m:
            return None
        year, month, day = int(m[4]), _MONTHS.get(m[2] or ""), m[1] or m[3]
        if month and day:
            return date(year, month, int(day)), date(year, month, int(day))
        if month:
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        return date(year, 1, 1), date(year, 12, 31)
    except ValueError:
        return None


def starts_after(role_text: str | None, as_of: date) -> bool:
    """Does a chair role start after `as_of`? Future wording, or "with effect from" a date
    that may fall after it. "Chair with effect from 2023" is a sitting chair; an effective
    date that can't be read is treated as possibly later (the chair-designate flag)."""
    t = normalize(role_text or "")
    if FUTURE_CHAIR.search(t):
        return True
    m = EFFECT_FROM.search(t)
    span = _date_span(m[1]) if m else None
    return bool(m) and (span is None or span[1] > as_of)


def classify_role(
    role_text: str | None, tense: str | None = "current", *, chair_designate: Tri = UNKNOWN
) -> RoleFacts:
    """Board facts for one appointment. `chair_designate` is the policy flag
    chair_designate_counts_as_chair, used as the chair fact for "Chair designate"."""
    role_type, designate = role_type_of(role_text)
    board_seat, chair, executive = FACTS[role_type]
    if NON_BOARD.search(normalize(role_text or "")):
        board_seat, chair = NO, NO
    if designate:
        chair = chair_designate
    current = TENSE.get((tense or "").strip().lower(), UNKNOWN)
    if current == YES and PAST_CUE.search(normalize(role_text or "")):
        current = UNKNOWN  # "Former CEO" tagged current: the two disagree
    return RoleFacts(role_type, board_seat, chair, executive, current)


def listed_by_rule(ch_company_type: str | None) -> Tri:
    """`no` for a UK private company type (it cannot list shares); otherwise unknown."""
    return NO if (ch_company_type or "").strip().lower() in PRIVATE_TYPES else UNKNOWN
