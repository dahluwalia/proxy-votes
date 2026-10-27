"""The meeting facts an extract claims, confirmed against the notice's own text.

Design choice: the model proposes and the PDF decides. A company number counts only if
the PyMuPDF text prints it near "registered" or "company number"; an AGM date only if
the text says the AGM is held on it (an authority's expiry date, "until the AGM in 2027",
doesn't count). Resolution numbers must run without gaps. Any failure is an error for
gate G1, never a guess.
"""

from __future__ import annotations

import re
from datetime import date

from pennant.core.models import Meeting, meeting_id, pad_company_number
from pennant.extract.schema import NoticeExtract

_MONTHS = ("January February March April May June July August September October "
           "November December").split()
_DATE = r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")\s*,?\s+(20\d\d)\b"
# "...Annual General Meeting ... will be held ... on Thursday, 18 June 2026", but not
# "until the next AGM to be held in 2027 or 1 October 2027" (authority expiry dates).
_HELD = re.compile(r"(?i)(?:annual general meeting|\bAGM\b).{0,300}?\b(?:held|take place)\b"
                   r"(?:(?!\b20\d\d\b|\bor\b|\bafter\b|\bnext\b).){0,250}?" + _DATE)
# "Registered in England No. 445790", "Registered in Scotland No. SC083026", "04 967 001".
_NUMBER = re.compile(
    r"(?i)(?:regist(?:ered|ration)|company\s+(?:number|no\b))[^\d]{0,60}?"
    r"\b((?:SC|NI|OC|SO|SL|NC)\s?)?(\d(?:[ ]?\d){5,7})\b")


def text_company_numbers(pages: list[str]) -> set[str]:
    found = set()
    for m in _NUMBER.finditer("\n".join(pages)):
        try:
            found.add(pad_company_number((m.group(1) or "") + m.group(2)))
        except ValueError:  # more than 8 characters with its prefix
            continue
    return found


def _date(day: str, month: str, year: str) -> date:
    return date(int(year), _MONTHS.index(month.capitalize()) + 1, int(day))


def text_agm_dates(pages: list[str]) -> set[date]:
    text = re.sub(r"\s+", " ", "\n".join(pages))
    return {_date(*m.groups()) for m in _HELD.finditer(text)}


def parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        m = re.search(_DATE, value or "", re.I)
        return _date(*m.groups()) if m else None


def gap_problem(numbers: list[int | None]) -> str | None:
    """Why these resolution numbers aren't one gap-free run, or None if they are."""
    if not numbers:
        return "no directors standing"
    if None in numbers:
        return "a director has no resolution number"
    nums = sorted(n for n in numbers if n is not None)
    if nums != list(range(nums[0], nums[0] + len(nums))):
        return f"resolution numbers are not contiguous: {nums}"
    return None


def check_meeting(ex: NoticeExtract, pages: list[str], key: str | None):
    """(Meeting, errors): the extract's number and date, confirmed by the PDF text."""
    errors = []
    try:
        number = pad_company_number(ex.company_number or "")
    except ValueError:
        number = None
        errors.append(f"{ex.doc}: company number {ex.company_number!r} is not a number")
    found = text_company_numbers(pages)
    if number and number not in found:
        errors.append(f"{ex.doc}: company number {number} is not in the PDF "
                      f"(found {sorted(found)})")
    day, held = parse_date(ex.agm_date), text_agm_dates(pages)
    if day not in held:
        errors.append(f"{ex.doc}: AGM date {ex.agm_date!r} is not a date the PDF says the AGM "
                      f"is held on ({sorted(str(d) for d in held)})")
    if errors or number is None or day is None or not ex.company_name:
        return None, errors or [f"{ex.doc}: no company name"]
    return Meeting(meeting_id=meeting_id(number, day), company_name=ex.company_name,
                   company_number=number, agm_date=day, doc_sha=ex.doc_sha, key=key), []
