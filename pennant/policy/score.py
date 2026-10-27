"""M6.1 Score each director: a points interval and the executive rule, then the vote.

Design choice: pure functions over yes/no/unknown facts (Kleene logic). A vote is FOR or
AGAINST only when every way of filling in the unknowns agrees; otherwise it is REVIEW,
listing each single fact that would settle it. Policy flags (AIM, debt-only listings,
chair-designate, group dedupe, the seat under vote) and analyst set-fact decisions are
applied here, last, so changing either only re-scores. Seats come from `reconcile`; the
stage that runs both over a whole run is policy/stage.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pennant.classify.research import is_growth_market, seat_listed
from pennant.classify.roles import FACTS as ROLE_FACTS
from pennant.classify.roles import starts_after
from pennant.core.config import Policy
from pennant.core.models import (
    Decision,
    Evidence,
    Meeting,
    OrgFacts,
    Person,
    Recommendation,
    RoleType,
    RosterEntry,
    RunContext,
    Seat,
    SeatStatus,
    StageReport,
    Vote,
)
from pennant.core.tri import (
    NO,
    UNKNOWN,
    YES,
    Interval,
    Tri,
    isum,
    seat_points,
    t_and,
    t_or,
    to_tri,
)
from pennant.policy.reconcile import (
    find_org,
    group_key,
    is_issuer_seat,
    org_index,
)
from pennant.state.decisions import apply_set_facts

SEAT_FACTS = ("board_seat", "chair", "executive", "current", "listed", "identity")
NED_ROLES = {RoleType.non_executive_director, RoleType.chair, RoleType.deputy_chair}
EXEC_ROLES = {RoleType.executive_director, RoleType.executive_chair, RoleType.officer_title}


# --- score ---------------------------------------------------------------------

def orgs_with_overrides(orgs, seats, decisions) -> list[OrgFacts]:
    """Org set-facts applied as classify applies them (E3): an analyst's `listed` means
    listed for the policy, so it sets equity_listing too. An org with seats but no
    research yet gets a bare OrgFacts, so the analyst can still set its facts."""
    by_key = {o.org_key: o for o in orgs}
    for d in decisions:
        p = d.payload
        names = [s.org_name for s in seats if s.org_key == p.get("target")]
        base = by_key.get(p.get("target")) or (
            OrgFacts(org_key=p["target"], canonical_name=names[0]) if names else None)
        if p.get("kind") != "org" or base is None:
            continue
        upd = {p["field"]: p["value"]} | (
            {"equity_listing": p["value"]} if p["field"] == "listed" else {})
        ev = Evidence(source="analyst", ref=d.decision_id, quote=d.reason)
        by_key[base.org_key] = OrgFacts.model_validate(
            base.model_dump() | upd | {"evidence": [*base.evidence, ev]})
    return list(by_key.values())


def _rows(seats, meeting, idx, policy, notes, analyst_listed) -> list[dict[str, Any]]:
    """One dict of effective facts per seat: org listing and policy flags applied. An
    analyst's org `listed` is used as given, never re-run through the policy flags."""
    rows = []
    for s in seats:
        f: dict[str, Any] = {k: getattr(s, k) for k in SEAT_FACTS}
        org = find_org(idx, s.org_name, s.org_key)
        if org and org.org_key in analyst_listed:
            f["listed"] = analyst_listed[org.org_key]
        elif org and {org.listed, org.equity_listing} != {UNKNOWN}:  # research knows something
            f["listed"] = seat_listed(org, policy)  # the policy flags, as classify applies them
            if org.equity_listing == YES and is_growth_market(org.market):
                notes.append(f"{s.org_name} is on {org.market}: aim_counts_as_listed="
                             f"{policy.aim_counts_as_listed}")
            elif org.listed == YES and org.equity_listing != YES:
                notes.append(f"{s.org_name} has listed debt: debt_only_listing_counts="
                             f"{policy.debt_only_listing_counts}")
        issuer = is_issuer_seat(s, meeting)
        if f["chair"] == YES and (starts_after(s.role_text, meeting.agm_date)
                                  or (issuer and f["current"] == UNKNOWN)):
            f["chair"] = policy.chair_designate_counts_as_chair
            f["current"] = YES if issuer else f["current"]
            notes.append(f"{s.org_name}: chair-designate, counts as chair={f['chair']}")
        rows.append({"id": s.seat_id, "seat": s, "issuer": issuer, **f,
                     "org_key": org.org_key if org else s.org_key,
                     "group": group_key(s, idx, True), "org": group_key(s, idx, False)})
    return rows


def _counted(r: dict[str, Any], policy: Policy) -> Tri:
    c = t_and(r["board_seat"], r["current"], r["listed"], r["identity"])
    return t_and(c, policy.count_seat_under_vote) if r["issuer"] else c


def _effective(seats, meeting, orgs, policy, decisions, entry=None):
    """(rows, notes, evidence, analyst roles): each seat's facts as the vote sees them.
    Org listing and policy flags first, then analyst set-facts, last (rule 8)."""
    sets = [d for d in decisions if d.kind == "set_fact"]
    seats = apply_set_facts(list(seats), "seat", sets)
    analyst_listed = {d.payload["target"]: to_tri(d.payload["value"]) for d in sets
                      if d.payload.get("kind") == "org" and d.payload.get("field") == "listed"}
    notes: list[str] = []
    rows = _rows(seats, meeting, org_index(orgs_with_overrides(orgs, seats, sets)), policy,
                 notes, analyst_listed)
    evidence: list[Evidence] = []
    analyst_roles = []
    set_directly: set[tuple[str, str]] = set()  # (seat id, field) an analyst set by hand
    for d in sets:  # analyst overrides apply last, over the policy flags too
        p, hit = d.payload, False
        for r in rows:
            if p.get("kind") == "seat" and p.get("target") == r["id"]:
                if p["field"] in SEAT_FACTS:
                    r[p["field"]] = getattr(r["seat"], p["field"])
                    set_directly.add((r["id"], p["field"]))
                if p["field"] == "role_type" and r["issuer"]:
                    analyst_roles.append(r["seat"].role_type)
                hit = True
            hit = hit or (p.get("kind") == "org" and p.get("target") == r["org_key"])
        if entry and p.get("kind") == "roster" and p.get("target") in (
                entry.mention_id, f"p:{entry.mention_id}"):
            analyst_roles.append(RoleType(p["value"]))
            hit = True
        if hit:
            notes.append(f"analyst {d.decision_id}: {p.get('target')} {p.get('field')}="
                         f"{p.get('value')} ({d.reason})")
            evidence.append(Evidence(source="analyst", ref=d.decision_id, quote=d.reason))
    if analyst_roles:  # the analyst's role here also settles the issuer seat's role facts
        _, chair, executive = ROLE_FACTS[analyst_roles[-1]]
        for r in (r for r in rows if r["issuer"]):
            for field, value in (("chair", chair), ("executive", executive)):
                if value != UNKNOWN and (r["id"], field) not in set_directly:
                    r[field] = value
    return rows, notes, evidence, analyst_roles


def effective_seats(seats: Sequence[Seat], meeting: Meeting, orgs: Sequence[OrgFacts],
                    policy: Policy, decisions: Sequence[Decision] = ()) -> list[dict[str, Any]]:
    """Each seat's facts as scoring sees them (org listing, policy flags and the analyst
    set-facts in `decisions` applied, as `score` applies them), plus `counted`. Eval exports
    these for the gold scorer, so its seats match the votes."""
    rows = _effective(seats, meeting, orgs, policy, decisions)[0]
    return [{**r, "counted": _counted(r, policy)} for r in rows]


def _ch_gap(person: Person, why: str) -> dict[str, Any]:
    """Fail closed when the person's CH record is missing: the directorships the notice
    left out are unknown, so they enter as one unknown seat in a group of its own."""
    key = f"{person.person_id}.ch_record"
    seat = Seat(seat_id=key, org_name="Undisclosed Companies House directorships",
                org_key=key, role_text=why, status=SeatStatus.ch_unavailable)
    return {"id": key, "seat": seat, "issuer": False, "ch_gap": True, "org_key": key,
            "group": key, "org": key, **{k: UNKNOWN for k in SEAT_FACTS}}


def _exec_seat(r: dict[str, Any]) -> Tri:
    return t_and(r["executive"], r["board_seat"], r["current"], r["listed"], r["identity"])


def evaluate(rows: list[dict[str, Any]], ned_here: Tri, policy: Policy):
    """(points, points_rule, exec_rule, vote). group_dedupe unknown spans both groupings."""
    keys = {YES: ["group"], NO: ["org"]}.get(policy.group_dedupe, ["group", "org"])
    pts, exs = [], []
    for k in keys:
        best: dict[str, Interval] = {}
        for r in rows:
            best[r[k]] = best.get(r[k], Interval(0, 0)).imax(
                seat_points(_counted(r, policy), r["chair"]))
        pts.append(isum(best.values()))
        exs.append(t_or(*(t_and(_exec_seat(e), t_or(*(_counted(s, policy) for s in rows
                                                       if s[k] != e[k]))) for e in rows)))
    points = Interval(min(p.lo for p in pts), max(p.hi for p in pts))
    exec_rule = exs[0] if len(set(exs)) == 1 else UNKNOWN
    points_rule = t_and(ned_here, points.gt(policy.ned_points_threshold))
    verdict = t_or(points_rule, exec_rule)
    vote = {YES: Vote.AGAINST, NO: Vote.FOR}.get(verdict, Vote.REVIEW)
    return points, points_rule, exec_rule, vote


def _ned_here(roles: list[Any]) -> tuple[Tri, RoleType | None]:
    """Is the director a non-executive at this meeting? The first role that says."""
    for r in roles:
        if r in NED_ROLES or r in EXEC_ROLES:
            return (YES if r in NED_ROLES else NO), r
    return UNKNOWN, roles[0] if roles else None


def _fmt(iv: Interval) -> str:
    return str(iv.lo) if iv.lo == iv.hi else f"{iv.lo}–{iv.hi}"


def score(
    person: Person, entry: RosterEntry, meeting: Meeting, seats: Sequence[Seat],
    orgs: Sequence[OrgFacts], policy: Policy, decisions: Sequence[Decision] = (),
    ch_complete: bool = True,
) -> Recommendation:
    """Recommendation for one director at one meeting, from reconciled seats. Pass
    `ch_complete=False` when the person's CH records couldn't be read; a person with no
    linked CH record (unanchored) is treated the same: undisclosed seats are unknown."""
    rows, notes, evidence, analyst_roles = _effective(seats, meeting, orgs, policy, decisions,
                                                      entry)
    gap = ("no CH record linked (unanchored)" if not person.officer_ids
           else "CH record unavailable" if not ch_complete
           or any(s.status == SeatStatus.ch_unavailable for s in seats) else None)
    if gap:
        rows.append(_ch_gap(person, gap))
        notes.append(f"{gap}: directorships the notice left out are unknown")
    issuer_roles = [s.role_type for s in seats if is_issuer_seat(s, meeting) and s.current != NO]
    # an analyst's role wins, then the roster's, then what other notices say about the issuer
    ned, role = _ned_here([*analyst_roles[::-1], entry.role_type_at_issuer, *issuer_roles])
    points, points_rule, exec_rule, vote = evaluate(rows, ned, policy)

    unknown: list[str] = []
    if vote == Vote.REVIEW:  # Kleene logic is monotone: a decided vote can't flip
        def flips(k: str, i: int | None = None) -> bool:
            alts = {evaluate([{**r, k: v} if j == i else r for j, r in enumerate(rows)],
                             v if i is None else ned, policy)[3] for v in (YES, NO)}
            return len(alts) > 1
        if ned == UNKNOWN and flips("ned_here"):
            unknown.append(f"{entry.mention_id}.role_type_at_issuer")
        unknown += [f"{r['id']}.{k}" for i, r in enumerate(rows) if not r.get("ch_gap")
                    for k in SEAT_FACTS if r[k] == UNKNOWN and flips(k, i)]
        if gap and len({evaluate([*rows[:-1], {**rows[-1], **dict.fromkeys(SEAT_FACTS, v)}], ned,
                                 policy)[3] for v in (YES, NO)}) > 1:
            unknown.append(rows[-1]["id"])  # <person_id>.ch_record

    reasons = []
    for r in rows:
        iv, s = seat_points(_counted(r, policy), r["chair"]), r["seat"]
        if iv.hi > 0 or _exec_seat(r) != NO:
            exe = ", executive" if _exec_seat(r) != NO else ""
            reasons.append(f"{s.org_name} ({s.role_text or s.role_type.value}): {_fmt(iv)} pt"
                           f"{exe} [{s.status.value if s.status else 'unreconciled'}]")
            evidence += [e for e in s.evidence if e not in evidence]
    reasons.append(f"points {_fmt(points)} > {policy.ned_points_threshold}: "
                   f"{points.gt(policy.ned_points_threshold)}; non-executive here: {ned}")
    reasons += [f"executive rule: {exec_rule}", *notes]
    return Recommendation(
        person_id=person.person_id, meeting_id=meeting.meeting_id, role_at_meeting=role,
        points_lo=points.lo, points_hi=points.hi, points_rule=points_rule,
        exec_rule=exec_rule, vote=vote, coverage=entry.coverage, reasons=reasons,
        unknown_facts=unknown, evidence=evidence)


def run(ctx: RunContext) -> StageReport:
    """The score stage (reconcile and score every roster entry). It lives in policy/stage.py."""
    from pennant.policy.stage import run as run_stage

    return run_stage(ctx)
