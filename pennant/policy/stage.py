"""The score stage: reconcile each director's seats with CH, then score every roster entry.

Design choice: scoring reads only this run's upstream outputs (meeting, roster, person,
org, officer, review) and each notice seat as upstream last wrote it, so scoring a run
twice gives the same answer and a policy change only re-scores (`rescore`). Each
meeting's own company counts as listed. A scoring error makes that entry REVIEW plus a
`score_error` item and fails the stage; it never becomes a default vote.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pennant.classify.positions import merge_position
from pennant.classify.research import org_key
from pennant.core import db
from pennant.core.config import Policy
from pennant.core.models import (
    CHOfficer,
    Decision,
    Meeting,
    OrgFacts,
    Person,
    PositionFacts,
    Recommendation,
    ReviewItem,
    RosterEntry,
    RunContext,
    Seat,
    StageReport,
    Vote,
    person_id,
)
from pennant.core.tri import UNKNOWN, YES
from pennant.policy.reconcile import reconcile
from pennant.policy.score import orgs_with_overrides, score
from pennant.state.decisions import apply_set_facts


@dataclass
class Inputs:
    meetings: dict[str, Meeting]
    roster: list[RosterEntry]
    seats: list[Seat]  # notice seats only
    persons: list[Person]
    orgs: list[OrgFacts]
    officers: dict[str, CHOfficer]
    unavailable: set[str]  # officer ids / company numbers CH couldn't serve
    positions: dict[str, PositionFacts] = field(default_factory=dict)  # firms' sites, by seat


def issuer_orgs(meetings: Iterable[Meeting]) -> list[OrgFacts]:
    """Each meeting's company is listed: it is holding the AGM its notice calls. Research
    rows come first in the org index, so these only fill gaps."""
    return [OrgFacts(org_key=org_key(m.company_name), canonical_name=m.company_name,
                     org_type="company", listed=YES, equity_listing=YES,
                     market="issuer of this AGM") for m in meetings]


def _upstream_seats(conn: sqlite3.Connection, run_id: str) -> list[Seat]:
    """Notice seats as the upstream stages last wrote them (status None until M6 reconciles),
    so scoring a run twice starts from the same inputs, not from its own earlier output."""
    latest: dict[str, Seat] = {}
    for row in conn.execute("SELECT key, payload FROM run_outputs WHERE run_id = ? "
                            "AND kind = 'seat' ORDER BY id", (run_id,)):
        s = Seat.model_validate_json(row[1])
        if s.mention_id and (s.status is None or row[0] not in latest):
            latest[row[0]] = s
    return [latest[k] for k in sorted(latest)]


def load_inputs(conn: sqlite3.Connection, run_id: str) -> Inputs:
    def get(kind: str) -> list[Any]:
        return db.get_outputs(conn, run_id, kind)

    meetings = {m.meeting_id: m for m in get("meeting")}
    return Inputs(
        meetings=meetings, roster=get("roster"), persons=get("person"),
        seats=_upstream_seats(conn, run_id),
        orgs=[*get("org"), *issuer_orgs(meetings.values())],
        officers={o.officer_id: o for o in get("officer")},
        unavailable={r.subject for r in get("review") if "unavailable" in r.kind},
        positions={p.seat_id: p for p in get("position")})


def score_inputs(inp: Inputs, policy: Policy, decisions: Sequence[Decision] = ()):
    """(recommendations, seats, orgs, review items, errors) for every roster entry. Seats
    and orgs carry analyst set-facts, which gate G4 checks. A roster entry no person
    covers is scored alone; an error becomes a REVIEW plus a review item."""
    entries = {e.mention_id: e for e in inp.roster}
    covered = {m for p in inp.persons for m in p.mention_ids}
    persons = [*inp.persons, *(Person(person_id=person_id([m]), mention_ids=[m])
                               for m in entries if m not in covered)]
    sets = [d for d in decisions if d.kind == "set_fact"]
    recs, seats, items, errors = [], {}, [], []
    for p in sorted(persons, key=lambda p: p.person_id):
        ids = p.officer_ids
        ok = ids and all(i in inp.officers and i not in inp.unavailable for i in ids)
        ch = [inp.officers[i] for i in ids] if ok else None
        mine = [x for x in inp.seats if x.mention_id in p.mention_ids]
        todo = [entries[m] for m in p.mention_ids if m in entries]
        for e in sorted(todo, key=lambda e: (str(getattr(inp.meetings.get(e.meeting_id),
                                                         "agm_date", "")), e.mention_id)):
            try:
                meeting = inp.meetings[e.meeting_id]
                down = {meeting.meeting_id, meeting.company_number} & inp.unavailable
                orgs = orgs_with_overrides(inp.orgs, mine, sets)
                rs, ri = reconcile(p, meeting, mine, None if down else ch, orgs)
                rs = [merge_position(s, inp.positions[s.seat_id])
                      if s.seat_id in inp.positions else s for s in rs]
                recs.append(score(p, e, meeting, rs, inp.orgs, policy, sets,
                                  ch_complete=bool(ok) and not down))
                seats.update({s.seat_id: s for s in apply_set_facts(rs, "seat", sets)})
                items += ri
            except Exception as exc:  # fail closed: REVIEW, never a default vote
                errors.append(f"{e.mention_id}: {exc!r}")
                items.append(ReviewItem(kind="score_error", subject=e.mention_id, detail=repr(exc)))
                recs.append(Recommendation(
                    person_id=p.person_id, meeting_id=e.meeting_id, points_lo=0, points_hi=99,
                    role_at_meeting=e.role_type_at_issuer, points_rule=UNKNOWN,
                    exec_rule=UNKNOWN, vote=Vote.REVIEW, coverage=e.coverage,
                    reasons=[f"scoring failed: {exc!r}"]))
    orgs = [o for o in orgs_with_overrides(inp.orgs, list(seats.values()), sets)
            if o.org_key in {d.payload.get("target") for d in sets}]
    return recs, list(seats.values()), orgs, items, errors


def rescore(conn: sqlite3.Connection, run_id: str, policy: Policy,
            decisions: Sequence[Decision] | None = None) -> list[Recommendation]:
    """Re-score a stored run under another policy without writing (eval's AIM check)."""
    decisions = db.active_decisions(conn) if decisions is None else decisions
    return score_inputs(load_inputs(conn, run_id), policy, decisions)[0]


def run(ctx: RunContext) -> StageReport:
    recs, seats, orgs, items, errors = score_inputs(
        load_inputs(ctx.conn, ctx.run_id), ctx.policy, db.active_decisions(ctx.conn))
    db.put_outputs(ctx.conn, ctx.run_id, "seat", seats)
    db.put_outputs(ctx.conn, ctx.run_id, "org", orgs)
    db.put_outputs(ctx.conn, ctx.run_id, "recommendation", recs)
    db.put_outputs(ctx.conn, ctx.run_id, "review", items)
    counts = {"recommendations": len(recs), "seats": len(seats), "review_items": len(items)}
    counts |= {v.value: sum(r.vote == v for r in recs) for v in Vote}
    return StageReport(stage="score", ok=not errors, counts=counts, errors=errors)
