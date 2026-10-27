"""The classify stage: role rules, web research on organisations, then roles on firms' sites.

Design choice: Companies House and the notice are the first sources for people; the web
is asked only where rules and CH leave the answer open. Role facts (board seat, chair,
executive) come from the notice's role title through the M5.1 rules table and from CH.
Where those leave a role fact unknown on a seat that could count, the firm's own website
is asked, and only it (positions.py); its answers are written as `position` outputs,
which the score stage uses to fill unknown facts on the matching seats. A UK private
company type settles listed `no` for free. An organisation where every seat is past, or
is not a board seat (a trustee, adviser or committee role), needs no answer. Every other
organisation gets one cited question: is it listed on the AGM date, on which market, and
is it a subsidiary. Each seat is re-written as a newer run_outputs row with its role
facts and policy-adjusted `listed` filled in; facts already known upstream are never
overwritten by rules.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pennant.ch.client import WEB_URL
from pennant.classify.positions import research_positions, targets
from pennant.classify.research import (
    cached_parent,
    org_key,
    research_org,
    seat_listed,
)
from pennant.classify.roles import FACTS, classify_role, listed_by_rule
from pennant.core import db
from pennant.core.config import Policy
from pennant.core.models import (
    Decision,
    Evidence,
    OrgFacts,
    RoleType,
    RunContext,
    Seat,
    StageReport,
)
from pennant.core.tri import NO, UNKNOWN, YES, Tri, to_tri

_TENSE_OF = {YES: "current", NO: "past"}


def apply_rules(seat: Seat, policy: Policy) -> Seat:
    """Fill role_type (if `other`) and unknown board / chair / executive facts from the
    role title. The seat's `current` already carries the notice tense or CH dates."""
    rf = classify_role(seat.role_text, _TENSE_OF.get(seat.current, "unclear"),
                       chair_designate=policy.chair_designate_counts_as_chair)
    role_type = seat.role_type if seat.role_type != RoleType.other else rf.role_type
    board, chair, executive = rf[1:4] if role_type == rf.role_type else FACTS[role_type]
    update: dict[str, Any] = {"role_type": role_type}
    for field, value in (("board_seat", board), ("chair", chair), ("executive", executive)):
        if getattr(seat, field) == UNKNOWN:
            update[field] = value
    return seat.model_copy(update=update)


def ch_rule_org(key: str, name: str, number: str, ch_type: str) -> OrgFacts:
    quote = f"Company type: {ch_type} (a UK private company type cannot list shares)"
    ev = Evidence(source="ch", ref=f"{WEB_URL}/company/{number}", quote=quote)
    return OrgFacts(org_key=key, canonical_name=name, listed=NO, equity_listing=NO,
                    evidence=[ev])


def apply_org_overrides(facts: dict[str, OrgFacts], decisions: list[Decision]) -> dict[str, Tri]:
    """Analyst set_fact decisions on orgs, oldest first, before `listed` reaches the seats.
    An analyst's `listed` means listed for the policy, so it also sets equity_listing.
    Returns the analyst's `listed` per org key: seats take it as is, past the policy flags."""
    listed: dict[str, Tri] = {}
    for d in decisions:
        p = d.payload
        if d.kind != "set_fact" or p.get("kind") != "org" or p.get("target") not in facts:
            continue
        org = facts[p["target"]]
        update = {p["field"]: p["value"]}
        if p["field"] == "listed":
            update["equity_listing"] = p["value"]
            listed[p["target"]] = to_tri(p["value"])
        ev = Evidence(source="analyst", ref=d.decision_id, quote=d.reason, fetched_at=d.created_at)
        data = org.model_dump() | update
        facts[p["target"]] = OrgFacts.model_validate(
            data | {"evidence": [*data["evidence"], ev.model_dump()]})
    return listed


def run(ctx: RunContext) -> StageReport:
    def out(kind: str) -> list[Any]:
        return db.get_outputs(ctx.conn, ctx.run_id, kind)

    meetings = {m.meeting_id: m for m in out("meeting")}
    roster = {r.mention_id: r for r in out("roster")}
    officers = {o.officer_id: o for o in out("officer")}
    by_mention = {k: meetings[r.meeting_id].agm_date for k, r in roster.items()
                  if r.meeting_id in meetings}
    by_officer: dict[str, date] = {}
    for p in out("person"):
        dates = [by_mention[m] for m in p.mention_ids if m in by_mention]
        by_officer.update({o: min(dates) for o in p.officer_ids if dates})
    default_day = min((m.agm_date for m in meetings.values()), default=date.today())

    def as_of(s: Seat) -> date:
        day = by_mention.get(s.mention_id or "") or by_officer.get(s.officer_id or "")
        return day or default_day

    # 1. Rules. A director standing at this AGM sits on the issuer's board, which CH can see.
    seats = []
    for s in out("seat"):
        s = apply_rules(s, ctx.policy)
        m = meetings.get(roster[s.mention_id].meeting_id) if s.mention_id in roster else None
        if m and (s.ch_company_number == m.company_number
                  or org_key(s.org_name) == org_key(m.company_name)):
            board = YES if s.board_seat == UNKNOWN else s.board_seat
            s = s.model_copy(update={"board_seat": board, "ch_company_number": m.company_number})
        seats.append(s)

    # 2. Organisations: from seats, and from linked officers' CH appointments live on the AGM date.
    orgs: dict[str, dict[str, Any]] = {}

    def add(key: str, name: str, number: str | None, ch_type: str | None, day: date,
            live: bool, from_seat: bool) -> None:
        """Research date: the earliest AGM among the org's seats, so a notice org's question
        doesn't move when dedup links its director elsewhere; CH-only orgs use the officer's."""
        o = orgs.setdefault(key, {"name": name, "number": None, "type": None, "live": False,
                                  "day": None, "ch_day": None})
        o["number"], o["type"] = o["number"] or number, o["type"] or ch_type
        o["live"] = o["live"] or live
        slot = "day" if from_seat else "ch_day"
        o[slot] = min(o[slot] or day, day)

    ch_types = {a.company_number: a.company_type for o in officers.values() for a in o.appointments}
    for s in seats:
        add(s.org_key, s.org_name, s.ch_company_number, ch_types.get(s.ch_company_number or ""),
            as_of(s), s.current != NO and s.board_seat != NO, from_seat=True)
    for oid, day in sorted(by_officer.items()):
        for a in officers[oid].appointments if oid in officers else []:
            if a.resigned_on is None or a.resigned_on >= day:
                add(org_key(a.company_name), a.company_name, a.company_number, a.company_type,
                    day, "secretary" not in (a.role or "").lower(), from_seat=False)

    facts: dict[str, OrgFacts] = {}
    counts = {"seats": len(seats), "orgs": len(orgs), "listed_by_rule": 0, "org_research": 0}
    for key, o in sorted(orgs.items()):
        o["day"] = o["day"] or o["ch_day"]
        if o["number"] and listed_by_rule(o["type"]) == NO:
            facts[key] = ch_rule_org(key, o["name"], o["number"], o["type"])
            facts[key].parent_name = cached_parent(o["name"], o["day"], ctx)
            counts["listed_by_rule"] += 1
        elif not o["live"]:
            facts[key] = OrgFacts(org_key=key, canonical_name=o["name"])  # no seat can count
        else:
            facts[key] = research_org(o["name"], o["day"], ctx, key=key)
            counts["org_research"] += 1

    analyst_listed = apply_org_overrides(facts, db.active_decisions(ctx.conn))

    def listed(key: str) -> Tri:
        return analyst_listed.get(key) or seat_listed(facts.get(key), ctx.policy)

    # 3. Listed status onto every seat, under the policy flags; an analyst's org value wins.
    seats = [s.model_copy(update={"listed": analyst_listed[s.org_key]})
             if s.org_key in analyst_listed else s if s.listed != UNKNOWN else
             s.model_copy(update={"listed": listed(s.org_key)}) for s in seats]
    db.put_outputs(ctx.conn, ctx.run_id, "seat", seats)
    db.put_outputs(ctx.conn, ctx.run_id, "org", list(facts.values()))

    # 4. Roles the notice and CH leave open, asked of each firm's own website only.
    todo = targets(seats, list(facts.values()), meetings, roster, out("person"), officers)
    names = {k: f.canonical_name for k, f in facts.items() if f.canonical_name}
    positions = research_positions(ctx, todo, names)
    db.put_outputs(ctx.conn, ctx.run_id, "position", positions)
    counts |= {"position_targets": len(todo), "position_research": len(positions)}
    return StageReport(stage="classify", ok=True, counts=counts)
