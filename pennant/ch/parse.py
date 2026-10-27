"""Pure parsers for Companies House API responses.

Design choice: parsing is separate from fetching, so every recorded fixture can be
checked without a network or a database. Parsers never raise on odd data: a missing
officer id drops the row, and a bad date becomes None. Company numbers are padded
to 8 characters. Officer ids come from `links.officer.appointments` (officer lists)
or `links.self` (search). Names come from `name_elements` when present, otherwise
from "SURNAME, Forenames" or "Forenames SURNAME", with titles and honours dropped
("Sharmila NEBHRAJANI  OBE" has the surname NEBHRAJANI) using dedup.names' word lists,
so the two never disagree about what is part of a name.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pennant.core.models import CHAppointment, CHOfficer, pad_company_number
from pennant.dedup.names import fold, is_title_or_honour

_OFFICER_LINK = re.compile(r"/officers/([^/]+)/appointments")


def pad(n: Any) -> str:
    """Company number padded to 8 characters; anything odd is returned as-is."""
    try:
        return pad_company_number(n)
    except ValueError:
        return str(n or "").strip().upper()


def officer_id_from(links: dict[str, Any] | None) -> str | None:
    links = links or {}
    href = (links.get("officer") or {}).get("appointments") or links.get("self") or ""
    m = _OFFICER_LINK.search(href)
    return m.group(1) if m else None


def _drop_honours(tokens: list[str], keep: int) -> list[str]:
    """Leading titles and trailing honours removed, keeping at least `keep` tokens."""
    tokens = list(tokens)
    while len(tokens) > keep and is_title_or_honour(tokens[0]):
        tokens.pop(0)
    while len(tokens) > keep and is_title_or_honour(tokens[-1]):
        tokens.pop()
    return tokens


def split_name(name: str) -> tuple[str | None, str | None]:
    """(surname, forenames) from 'SURNAME, Forenames[, Title]' or 'Forenames SURNAME
    [Honours]'. Titles and honours are dropped unless they are all there is ('MA, Jack')."""
    if "," in name:
        parts = [p.strip() for p in name.split(",")]
        surname = " ".join(_drop_honours(parts[0].split(), 1))
        given = " ".join(_drop_honours(parts[1].split(), 0)) if len(parts) > 1 else ""
        return surname or None, given or None
    tokens = _drop_honours(name.split(), 2)
    cut = len(tokens)
    while cut > 0 and tokens[cut - 1].isupper() and len(tokens[cut - 1].strip(".")) > 1:
        cut -= 1
    if cut in (0, len(tokens)):  # case says nothing (no upper-case word, or all upper case)
        cut = max(len(tokens) - 1, 0)  # so read it as if lower case: the last word
    return " ".join(tokens[cut:]) or None, " ".join(tokens[:cut]) or None


def _name_parts(item: dict[str, Any], fallback: str) -> tuple[str | None, str | None]:
    ne = item.get("name_elements") or {}
    if ne.get("surname"):
        given = " ".join(x for x in (ne.get("forename"), ne.get("other_forenames")) if x)
        return ne["surname"], given or None
    return split_name(fallback)


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _dob(item: dict[str, Any]) -> dict[str, int | None]:
    dob = item.get("date_of_birth") or {}
    return {"dob_month": dob.get("month"), "dob_year": dob.get("year")}


def natural_name(officer: CHOfficer) -> str:
    """'Forenames SURNAME', the order search and names.compare expect."""
    if officer.surname and officer.forenames:
        return f"{officer.forenames} {officer.surname}"
    return officer.name


def parse_company(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_number": pad(body.get("company_number")),
        "company_name": body.get("company_name") or "",
        "company_type": body.get("type"),
        "company_status": body.get("company_status"),
        "jurisdiction": body.get("jurisdiction"),
    }


def parse_officer_item(item: dict[str, Any], number: str, company_name: str,
                       company_type: str | None = None) -> CHOfficer | None:
    """One row of a company's officer list -> a CHOfficer with that one appointment."""
    officer_id = officer_id_from(item.get("links"))
    if not officer_id:
        return None
    name = item.get("name") or ""
    surname, forenames = _name_parts(item, name)
    appt = CHAppointment(
        company_number=number,
        company_name=company_name,
        company_type=company_type,
        role=item.get("officer_role") or "unknown",
        appointed_on=_date(item.get("appointed_on") or item.get("appointed_before")),
        resigned_on=_date(item.get("resigned_on")),
    )
    return CHOfficer(officer_id=officer_id, name=name, surname=surname, forenames=forenames,
                     appointments=[appt], **_dob(item))


def parse_appointments(officer_id: str, body: dict[str, Any]) -> CHOfficer:
    """An officer's appointment list -> one CHOfficer (company types not yet filled)."""
    items = body.get("items") or []
    name = body.get("name") or (items[0].get("name") if items else "") or ""
    surname, forenames = _name_parts(items[0] if items else {}, name)
    appts = [
        CHAppointment(
            company_number=pad((i.get("appointed_to") or {}).get("company_number")),
            company_name=(i.get("appointed_to") or {}).get("company_name") or "",
            role=i.get("officer_role") or "unknown",
            appointed_on=_date(i.get("appointed_on") or i.get("appointed_before")),
            resigned_on=_date(i.get("resigned_on")),
        )
        for i in items
    ]
    return CHOfficer(officer_id=officer_id, name=name, surname=surname, forenames=forenames,
                     appointments=appts, **_dob(body))


def parse_search_item(item: dict[str, Any]) -> CHOfficer | None:
    officer_id = officer_id_from(item.get("links"))
    if not officer_id:
        return None
    name = item.get("title") or ""
    surname, forenames = split_name(name)
    return CHOfficer(officer_id=officer_id, name=name, surname=surname, forenames=forenames,
                     **_dob(item))


def _fold(text: str | None) -> str:
    """names.fold, so namesakes and names.compare fold accents the same way."""
    return " ".join(fold(text or "").split())


def _namesake_key(name: str) -> tuple[str, str]:
    """(surname, first forename), folded: what a hit must share to be the same person."""
    surname, forenames = split_name(name.strip())
    return _fold(surname), _fold((forenames or "").split(" ")[0])


def namesakes(body: dict[str, Any] | None, name: str) -> list[tuple]:
    """The hits sharing `name`'s surname and first forename, as sorted (id, name, DOB)."""
    want, out = _namesake_key(name), set()
    for item in (body or {}).get("items") or []:
        hit = parse_search_item(item)
        if hit and (_fold(hit.surname), _fold((hit.forenames or "").split(" ")[0])) == want:
            out.add((hit.officer_id, hit.name, hit.dob_month, hit.dob_year))
    return sorted(out, key=str)


def active_on(appt: CHAppointment, as_of: date) -> bool:
    """In office on `as_of` (a resignation dated that day still counts)."""
    started = appt.appointed_on is None or appt.appointed_on <= as_of
    return started and (appt.resigned_on is None or appt.resigned_on >= as_of)
