"""M6.1 Reconcile a director's notice seats with Companies House as of the AGM date.

Design choice: CH is the authority for UK directorships, the notice for everything else.
Each notice seat gets one status from the M6.1 table (confirmed, stale, contradiction,
ch_invisible, ch_unavailable, past), and every active CH directorship the notice left out
becomes an `undisclosed` seat. Disagreements make facts unknown plus a review item,
never a guess. Org names are compared after folding case, punctuation and legal suffixes,
and corporate groups follow the parent names that web research found.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from pennant.ch.client import WEB_URL
from pennant.classify.research import org_key, org_tokens
from pennant.classify.roles import listed_by_rule
from pennant.core.models import (
    CHAppointment,
    CHOfficer,
    Evidence,
    Meeting,
    OrgFacts,
    Person,
    ReviewItem,
    RoleType,
    Seat,
    SeatStatus,
    pad_company_number,
)
from pennant.core.tri import NO, UNKNOWN, YES

# --- names, orgs and groups ----------------------------------------------------

def norm_org(name: str | None) -> str:
    """Comparable org name, folded as org keys are (classify.research.org_tokens)."""
    return " ".join(org_tokens(name or ""))


def _num(n: str | None) -> str | None:
    try:
        return pad_company_number(n) if n else None
    except ValueError:
        return None


def org_index(orgs: Sequence[OrgFacts]) -> dict[str, OrgFacts]:
    idx: dict[str, OrgFacts] = {}
    for o in orgs:
        idx.setdefault(o.org_key, o)
        idx.setdefault(norm_org(o.canonical_name), o)
    return idx


def find_org(idx: dict[str, OrgFacts], name: str, key: str | None = None) -> OrgFacts | None:
    return idx.get(key or "") or idx.get(norm_org(name))


def group_key(seat: Seat, idx: dict[str, OrgFacts], dedupe: bool = True) -> str:
    """The seat's corporate group: its org, or with dedupe the top parent research found."""
    org = find_org(idx, seat.org_name, seat.org_key)
    key = norm_org(org.canonical_name if org else seat.org_name)
    seen = {key}
    while dedupe and org and org.parent_name and norm_org(org.parent_name) not in seen:
        key = norm_org(org.parent_name)
        seen.add(key)
        org = idx.get(key)
    return key


def is_issuer_seat(seat: Seat, meeting: Meeting) -> bool:
    return (_num(seat.ch_company_number) == meeting.company_number
            or norm_org(seat.org_name) == norm_org(meeting.company_name))


# --- reconcile -----------------------------------------------------------------

def _active(a: CHAppointment, as_of: date) -> bool:
    return ((a.appointed_on is None or a.appointed_on <= as_of)
            and (a.resigned_on is None or a.resigned_on > as_of))


def _same_org(seat: Seat, a: CHAppointment) -> bool:
    n = _num(seat.ch_company_number)
    return (n is not None and n == _num(a.company_number)) or (
        norm_org(seat.org_name) == norm_org(a.company_name))


def _ch_evidence(o: CHOfficer, a: CHAppointment) -> Evidence:
    end = f", resigned {a.resigned_on}" if a.resigned_on else ""
    return Evidence(source="ch", ref=f"{WEB_URL}/officers/{o.officer_id}/appointments",
                    quote=f"{a.company_name}: {a.role}, appointed {a.appointed_on}{end}")


def _review(kind: str, subject: str, detail: str, fact: str | None = None) -> ReviewItem:
    cmd = f'pennant set-fact {subject} {fact} <yes|no> --reason "..."' if fact else None
    return ReviewItem(kind=kind, subject=subject, detail=detail, suggested_command=cmd)


def _fill_from_same_org(seats: list[Seat]) -> None:
    """Two notices can describe one seat: the M&S notice gives Tamara Ingram no role, her
    Intertek biography says she is an M&S non-executive. A chair or executive fact left
    unknown is filled from the person's other current board seats at the same org, but
    only when those all agree."""
    live = [s for s in seats if s.current != NO and s.board_seat != NO]
    for s in live:
        same = [x for x in live if x is not s and norm_org(x.org_name) == norm_org(s.org_name)]
        for fact in ("chair", "executive"):
            known = {getattr(x, fact) for x in same} - {UNKNOWN}
            if getattr(s, fact) == UNKNOWN and len(known) == 1:
                setattr(s, fact, known.pop())
                s.evidence += [e for x in same for e in x.evidence if e not in s.evidence]


def reconcile(
    person: Person, meeting: Meeting, seats: Sequence[Seat], ch: Sequence[CHOfficer] | None,
    orgs: Sequence[OrgFacts] = (),
) -> tuple[list[Seat], list[ReviewItem]]:
    """The person's seats as of the AGM date, each with a status (the M6.1 table), plus
    review items. `ch` is the person's linked CH records, or None if CH couldn't be read.
    Only notice seats are read from `seats`; CH-only seats are rebuilt from `ch`."""
    as_of, idx = meeting.agm_date, org_index(orgs)
    appts = [(o, a) for o in ch or [] for a in o.appointments
             if "director" in a.role.lower() or "member" in a.role.lower()]
    out: list[Seat] = []
    items: list[ReviewItem] = []
    matched: set[str] = set()
    for s in sorted((x.model_copy(deep=True) for x in seats if x.mention_id),
                    key=lambda x: x.seat_id):
        s.identity = YES if s.identity == UNKNOWN else s.identity  # from their own biography
        if is_issuer_seat(s, meeting):
            s.ch_company_number = s.ch_company_number or meeting.company_number
            s.listed = YES if s.listed == UNKNOWN else s.listed  # the company holding the vote
            # Standing at the AGM means on the board, if CH can confirm the person at all:
            # with no CH record (unanchored, or CH down) they may have left it (Garnier).
            # Unknown + chair is a chair-designate (see score._rows).
            if s.current == UNKNOWN and s.chair != YES and ch is not None:
                s.current = YES
        hits = [(o, a) for o, a in appts if _same_org(s, a)]
        matched.update(_num(a.company_number) or a.company_number for _, a in hits)
        live = [(o, a) for o, a in hits if _active(a, as_of)]
        for o, a in (live or hits)[:1]:
            if (ev := _ch_evidence(o, a)) not in s.evidence:
                s.evidence.append(ev)
        if s.current == NO:
            s.status = SeatStatus.past
        elif ch is None:
            s.status = SeatStatus.ch_unavailable
        elif live:
            s.status = SeatStatus.confirmed
            s.board_seat = YES if s.board_seat == UNKNOWN else s.board_seat
        elif hits or _num(s.ch_company_number):
            # CH has no live appointment: resigned before the AGM (stale), not yet appointed
            # on it, or none at all (both contradictions). The notice's "current" is in doubt.
            later = sorted(a.appointed_on for _, a in hits
                           if a.appointed_on and a.appointed_on > as_of)
            s.status = SeatStatus.stale if hits and not later else SeatStatus.contradiction
            s.current = UNKNOWN
            why = (f"shows it appointed after the AGM ({later[0]}), so not yet on" if later
                   else "resigned before" if hits else "has no appointment there on")
            items.append(_review(s.status.value, s.seat_id, f"{person.person_id}: notice says "
                                 f"{s.role_text!r} at {s.org_name}; CH {why} {as_of}", "current"))
        else:
            s.status = SeatStatus.ch_invisible
        out.append(s)
    _fill_from_same_org(out)
    for o, a in appts:
        n = _num(a.company_number) or a.company_number
        if n in matched or not _active(a, as_of):
            continue
        matched.add(n)
        org = find_org(idx, a.company_name)
        listed = org.listed if org and org.listed != UNKNOWN else listed_by_rule(a.company_type)
        if listed == NO:
            continue
        s = Seat(seat_id=f"seat:{o.officer_id}:{n}", officer_id=o.officer_id,
                 org_name=a.company_name, ch_company_number=n, role_text=a.role,
                 org_key=org.org_key if org else org_key(a.company_name),
                 role_type=RoleType.director_unspecified, board_seat=YES, current=YES,
                 listed=listed, identity=YES, status=SeatStatus.undisclosed,
                 evidence=[_ch_evidence(o, a)])
        out.append(s)
        items.append(_review("undisclosed", s.seat_id, f"{person.person_id}: CH shows {a.role} "
                             f"of {a.company_name} on {as_of}; not in the notice", "board_seat"))
    return out, items


