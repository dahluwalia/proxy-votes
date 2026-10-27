"""M4.2: anchor each notice name to its issuer's CH officer list, then follow date of birth.

Design choice: identity is anchored on companies, not names. A notice name is only
compared with the directors of the company that issued the notice who were in office
on its AGM date. Among those with the same surname, the notice's
first name is ranked by character 3-gram TF-IDF against each director's forenames
(dedup/tfidf.py, Sparkly's scoring, in-process); a listed nickname ("Tom" for Thomas,
dedup/nicknames.py) scores like the exact name, since 3-grams can't see it. The top director is
the anchor only with a score of at least MIN_SCORE and a lead of at least MARGIN over the
next; otherwise it is a review item. The anchor's date of birth then links the person's
other CH records, and only when every given name is the same: a missing or initialled
middle name is a review item, since a common name with a shared DOB is not proof. There
is no LLM, and every link carries its CH evidence.

dedup/cluster.py is the only caller: anchor(), anchor_evidence(), expand(), and
fetch_officer() for an officer that only an analyst's merge joined.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pennant.ch import client
from pennant.ch.parse import active_on, natural_name
from pennant.core import db
from pennant.core.models import (
    CHAppointment,
    CHOfficer,
    Evidence,
    ReviewItem,
    RosterEntry,
    RunContext,
)
from pennant.dedup import tfidf
from pennant.dedup.names import aligned, at_least, compare, parse
from pennant.dedup.nicknames import is_nickname

MERGE_HINT = 'pennant merge {a} {b} --reason "..."'
# Checked on the example notices' 50 names: every true match scores and leads by 0.38
# or more (Gerry beats Ken Murphy 0.46 to 0). A listed nickname scores 1.0 (Tom/Thomas
# would get 0.19 from 3-grams alone); an unlisted one that shares only its initial falls
# below MIN_SCORE and goes to the analyst.
MIN_SCORE = 0.3
MARGIN = 0.25


def _meeting_parts(meeting_id: str) -> tuple[str, date]:
    """'mtg:<company_number>:<agm_date>' -> (company_number, agm_date)."""
    _, number, agm = meeting_id.split(":")
    return number, date.fromisoformat(agm)


def ch_name(officer: CHOfficer) -> str:
    """'SURNAME, Forenames': the unambiguous form for names.compare."""
    if officer.surname and officer.forenames:
        return f"{officer.surname}, {officer.forenames}"
    return officer.name


def _dob(officer: CHOfficer) -> str:
    if officer.dob_month is None or officer.dob_year is None:
        return "unknown"
    return f"{officer.dob_month:02d}/{officer.dob_year}"


def issuer_seat(officer: CHOfficer, number: str, as_of: date) -> CHAppointment | None:
    """The officer's directorship at the issuer, if they were in office on `as_of`."""
    for a in officer.appointments:
        if a.company_number == number and "director" in a.role and active_on(a, as_of):
            return a
    return None


def anchor(
    entry: RosterEntry, issuer_officers: list[CHOfficer]
) -> tuple[CHOfficer | None, list[ReviewItem]]:
    """The issuer director in office on the AGM date who clearly wins the name ranking
    (see `rank`). A close race gives `possible_same_person` and no anchor. With no
    candidate in office, a former issuer director who clearly wins is the anchor (Thierry
    Garnier left Tesco's board before its AGM), with a `not_in_office` review item: the
    record's dates then make the notice's seat stale or a contradiction (reconcile), so
    the vote waits for an analyst instead of trusting the notice. Otherwise `unanchored`."""
    number, agm = _meeting_parts(entry.meeting_id)
    in_office = [o for o in issuer_officers if issuer_seat(o, number, agm)]
    ranked = rank(entry.name, in_office)
    top, runner_up = (ranked + [(0.0, None), (0.0, None)])[:2]
    if _clear(top, runner_up):
        return top[1], []
    if top[0] < MIN_SCORE and (former := _former(entry, issuer_officers, number, agm)):
        return former

    def listing(rows: list[tuple[float, CHOfficer]]) -> str:
        return "; ".join(f"{o.name} ({o.officer_id}, score {s:.2f})" for s, o in rows)

    hint = MERGE_HINT.format(a=entry.mention_id, b="<officer_id>")
    if top[0] >= MIN_SCORE:  # a close race between same-surname directors
        close = [r for r in ranked if top[0] - r[0] < MARGIN]
        return None, [ReviewItem(
            kind="possible_same_person", subject=entry.mention_id,
            detail=f"{entry.name} matches {len(close)} directors of {number} in office on "
            f"{agm} too closely to choose (margin under {MARGIN}): {listing(close)}",
            suggested_command=hint)]
    closest = f"; closest: {listing(ranked[:3])}" if ranked else ""
    return None, [ReviewItem(
        kind="unanchored", subject=entry.mention_id,
        detail=f"{entry.name}: no director of {number} in office on {agm} shares the "
        f"surname with a name score of {MIN_SCORE} or more{closest}"
        f"{_not_in_office(entry, issuer_officers, number, agm)}",
        suggested_command=hint)]


def _clear(top: tuple[float, Any], runner_up: tuple[float, Any]) -> bool:
    return top[1] is not None and top[0] >= MIN_SCORE and top[0] - runner_up[0] >= MARGIN


def _former(entry: RosterEntry, officers: list[CHOfficer], number: str, agm: date,
            ) -> tuple[CHOfficer, list[ReviewItem]] | None:
    """A former (or future) issuer director who clearly wins the name ranking, or None."""
    others = [o for o in officers if not issuer_seat(o, number, agm)
              and any(a.company_number == number and "director" in a.role
                      for a in o.appointments)]
    ranked = rank(entry.name, others)
    top, runner_up = (ranked + [(0.0, None), (0.0, None)])[:2]
    if not _clear(top, runner_up):
        return None
    o = top[1]
    return o, [ReviewItem(
        kind="not_in_office", subject=entry.mention_id,
        detail=f"{entry.name} matches {o.name} ({o.officer_id}), a director of {number} "
        f"{_spans(o, number)} but not on the AGM date {agm}; the notice's seat is checked "
        "against those dates",
        suggested_command=f'pennant show "{entry.name}"')]


def _spans(o: CHOfficer, number: str) -> str:
    """'2021-04-30 to 2026-05-06': the officer's directorships at the issuer."""
    return ", ".join(f"{a.appointed_on} to {a.resigned_on or 'now'}" for a in o.appointments
                     if a.company_number == number and "director" in a.role)


def rank(name: str, officers: list[CHOfficer]) -> list[tuple[float, CHOfficer]]:
    """Officers whose surname matches `name` (and whose initials and middle names don't
    veto it), best first by the TF-IDF score of the notice's first given name against
    their forenames. IDF comes from these officers' forenames. An exact first name
    scores 1.0; a bare surname match scores 0."""
    parsed = parse(name)
    pairs = []
    for o in officers:
        if at_least(compare(parsed, ch_name(o)), "initial"):
            given, ch_given = aligned(parsed, ch_name(o))
            pairs.append((given[:1], ch_given, o))
    weights = tfidf.idf(t for _, ch_given, _ in pairs for t in ch_given)
    scored = [(_score(g[0], ch_given, weights) if g else 0.0, o) for g, ch_given, o in pairs]
    return sorted(scored, key=lambda x: (-x[0], x[1].officer_id))


def _score(first: str, ch_given: tuple[str, ...], weights: dict[str, float]) -> float:
    """1.0 for a listed nickname of any CH forename, else the TF-IDF score."""
    if any(is_nickname(first, n) for n in ch_given):
        return 1.0
    return tfidf.best_match(first, ch_given, weights)


def _nickname_note(name: str, officer: CHOfficer) -> str:
    """'; Tom is a listed nickname of Thomas' when that is how the names matched."""
    given, ch_given = aligned(name, ch_name(officer))
    hit = next((n for n in ch_given if given and is_nickname(given[0], n)), None)
    return f"; {given[0].title()} is a listed nickname of {hit.title()}" if hit else ""


def _not_in_office(entry: RosterEntry, officers: list[CHOfficer], number: str, agm: date) -> str:
    """Same-name issuer directors who were not in office on the AGM date (e.g. resigned
    after the notice went out), for the analyst. Empty if there are none."""
    notes = []
    for o in officers:
        seats = [a for a in o.appointments if a.company_number == number and "director" in a.role]
        if seats and not issuer_seat(o, number, agm) \
                and at_least(compare(entry.name, ch_name(o)), "variant"):
            spans = ", ".join(f"{a.appointed_on} to {a.resigned_on or 'now'}" for a in seats)
            notes.append(f"{o.name} ({o.officer_id}) was a director {spans}")
    return f"; not in office on the AGM date: {'; '.join(notes)}" if notes else ""


def anchor_evidence(entry: RosterEntry, officer: CHOfficer) -> list[Evidence]:
    """CH evidence for the notice-mention -> issuer-officer link."""
    number, agm = _meeting_parts(entry.meeting_id)
    seat = issuer_seat(officer, number, agm)
    role = f"{seat.role} of {seat.company_name or number}" if seat else f"officer of {number}"
    since = f" since {seat.appointed_on}" if seat and seat.appointed_on else ""
    if seat is None:  # a former director: say when
        spans = [f"{a.appointed_on} to {a.resigned_on or 'now'}" for a in officer.appointments
                 if a.company_number == number and "director" in a.role]
        role, since = f"director of {number}", f", {', '.join(spans)}" if spans else ""
    return [Evidence(
        source="ch", ref=f"{client.WEB_URL}/company/{number}/officers",
        quote=f"{officer.name}, born {_dob(officer)}: {role}{since}"
        f"{_nickname_note(entry.name, officer)}")]


def _earliest_agm(ctx: RunContext) -> date | None:
    meetings = db.get_outputs(ctx.conn, ctx.run_id, "meeting")
    return min((m.agm_date for m in meetings), default=None)


def expand(
    officer: CHOfficer, ctx: RunContext
) -> tuple[list[tuple[CHOfficer, list[Evidence]]], list[ReviewItem]]:
    """The anchor's other CH officer records: an officer search on its CH name, keeping
    hits with the same DOB month and year, the same surname and the same given names.
    Each comes back as a full record (all appointments, with company types from the
    earliest AGM on).

    A same-DOB hit whose name is `exact` but lacks or initials a middle name, or matches
    only at `variant` or `initial`, becomes a `possible_same_person` review item. A
    different DOB is ignored. A failed search leaves the other records unknown and raises
    a `ch_unavailable` review item. The search reads on until it is past the officers
    sharing the surname and first forename; if it stops at its page cap among them, the
    rest are unread and a `search_incomplete` review item says so."""
    if officer.dob_month is None or officer.dob_year is None:
        return [], [ReviewItem(
            kind="no_dob", subject=officer.officer_id,
            detail=f"{officer.name} has no date of birth on CH, so no other CH records "
            "can be linked")]
    res = client.search_officers(natural_name(officer), ctx)
    if not res.ok:
        return [], [ReviewItem(
            kind="ch_unavailable", subject=officer.officer_id,
            detail=f"CH officer search for {officer.name} is {res.status}: {res.error}; "
            "their other CH records are unknown",
            suggested_command="pennant run")]
    since = _earliest_agm(ctx)
    found: list[tuple[CHOfficer, list[Evidence]]] = []
    reviews: list[ReviewItem] = []
    if not res.complete:
        reviews.append(ReviewItem(
            kind="search_incomplete", subject=officer.officer_id,
            detail=f"CH lists more officers named like {officer.name} than the search "
            f"reads ({res.unread} unread); other CH records of theirs may be missing",
            suggested_command="pennant merge <mention> <officer_id> --reason \"...\""))
    seen = {officer.officer_id}
    for hit in res.data:
        same_dob = (hit.dob_month, hit.dob_year) == (officer.dob_month, officer.dob_year)
        if hit.officer_id in seen or not same_dob:
            continue
        seen.add(hit.officer_id)
        # `exact`: the first names agree and stated middle names don't contradict, so
        # "Christopher" and "Christopher John" with one DOB link (CH often files the same
        # person both ways); "Christopher John" vs "Christopher Paul" is a conflict.
        level = compare(ch_name(officer), ch_name(hit))
        if level == "exact":
            full = client.appointments(hit.officer_id, ctx, since)
            if not full.ok:
                reviews.append(ReviewItem(
                    kind="ch_unavailable", subject=hit.officer_id,
                    detail=f"CH appointments for {hit.name} are {full.status}: {full.error}; "
                    "their seats are unknown",
                    suggested_command="pennant run"))
            evidence = Evidence(
                source="ch", ref=f"{client.WEB_URL}/officers/{hit.officer_id}/appointments",
                quote=f"{hit.name}, born {_dob(hit)}: same date of birth and name as "
                f"{officer.name} ({officer.officer_id})",
                fetched_at=res.fetched_at)
            found.append((full.data if full.ok else hit, [evidence]))
        elif level in ("variant", "initial"):
            reviews.append(ReviewItem(
                kind="possible_same_person", subject=officer.officer_id,
                detail=f"{hit.name} ({hit.officer_id}) shares {officer.name}'s date of birth "
                f"{_dob(officer)}, but the names only match at {level}",
                suggested_command=MERGE_HINT.format(a=officer.officer_id, b=hit.officer_id)))
    return found, reviews


def fetch_officer(officer_id: str, ctx: RunContext) -> tuple[CHOfficer | None, list[ReviewItem]]:
    """The full CH record of an officer an analyst merged in, which no anchor or DOB link
    loaded. A failed fetch is a `ch_unavailable` item (gate G2), never an empty record."""
    full = client.appointments(officer_id, ctx, _earliest_agm(ctx))
    if full.ok:
        return full.data, []
    return None, [ReviewItem(
        kind="ch_unavailable", subject=officer_id,
        detail=f"CH appointments for merged officer {officer_id} are {full.status}: "
        f"{full.error}; their seats are unknown", suggested_command="pennant run")]
