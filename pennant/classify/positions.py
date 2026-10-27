"""A person's role at a firm, looked up only on that firm's own website.

Design choice: Companies House says who sits on a board but not in what capacity, and a
notice can leave roles out (the M&S notice has no biographies). The web may answer, but
only from the firm itself. One cited search finds the firm's website: the cited page
must be on it and name the firm, and a third-party site never counts. The role question
then runs with the search tool limited to that site, and a fact citing a page off it is
dropped. The role is asked as of the AGM date: the vote must be in by then, so a later
change doesn't matter, and a role that began after it or ended before it is not current.
Answers only fill facts still unknown, so the notice and Companies House win, and
analyst set-facts win over everything (rule 8). Only seats that could count are asked
about: current, maybe a board seat, at an organisation not known to be unlisted, with
board seat, chair or executive unknown. Prompts are in prompts.md.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pennant.classify.research import PROMPT_VERSION, _date, _review, _tri, org_tokens, render
from pennant.classify.roles import FACTS
from pennant.classify.websearch import ask, host, on_site, validate
from pennant.core import db
from pennant.core.models import (
    Evidence,
    PositionFacts,
    ReviewItem,
    RoleType,
    RunContext,
    Seat,
)
from pennant.core.tri import NO, UNKNOWN, YES

CAPACITY = {"executive_director": RoleType.executive_director, "chair": RoleType.chair,
            "executive_chair": RoleType.executive_chair,
            "non_executive_director": RoleType.non_executive_director}
NOT_A_BOARD = {RoleType.adviser, RoleType.trustee, RoleType.committee_chair}
ROLE_FACTS = ("board_seat", "chair", "executive")
# Sites that describe firms but are never a firm's own: encyclopedias, social networks,
# news, market data, exchanges and registries.
THIRD_PARTY = {
    "wikipedia.org", "linkedin.com", "bloomberg.com", "reuters.com", "ft.com", "wsj.com",
    "theguardian.com", "bbc.co.uk", "forbes.com", "marketscreener.com", "crunchbase.com",
    "zoominfo.com", "craft.co", "dnb.com", "opencorporates.com", "endole.co.uk",
    "companycheck.co.uk", "service.gov.uk", "companieshouse.gov.uk", "gov.uk", "sec.gov",
    "londonstockexchange.com", "nyse.com", "nasdaq.com", "euronext.com", "yahoo.com",
    "google.com", "morningstar.com", "globaldata.com", "pitchbook.com", "x.com",
    "twitter.com", "facebook.com", "instagram.com", "youtube.com",
}
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "ltd", "plc", "edu"}


def site_domain(hostname: str) -> str:
    """The registrable part of a host: 'corporate.marksandspencer.com' ->
    'marksandspencer.com', 'www.tesco.co.uk' -> 'tesco.co.uk'."""
    labels = hostname.lower().split(".")
    keep = 3 if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def _names_org(text: str, org: str) -> bool:
    """The org's first word, usually its brand ('rws' in 'RWS Holdings plc'), appears in
    the text; a site address counts ('marks' in 'marksandspencer.com')."""
    words = org_tokens(org)
    return bool(words) and words[0] in " ".join(org_tokens(text))


def research_site(org: str, ctx: RunContext, number: str | None = None,
                  key: str | None = None) -> tuple[str | None, Evidence | None]:
    """(domain, evidence) of the organisation's own website, or (None, None) with a
    review item. The cited page must be on the site the answer names and must name the
    organisation; a third-party site is never accepted. `key` (the seat's org key) keys
    the cache, so a research model's other spelling of the name still hits it."""
    key = key or "-".join(org_tokens(org))
    payload, problems = ask(PROMPT_VERSION["site"], {"org_key": key},
                            lambda: render("site", org=org, number=number or "not known"),
                            ctx, kind="site")
    v, ev, rejected = validate(payload)
    claimed, cite = v.get("website"), ev.get("website")
    domain = site_domain(host(claimed)) if isinstance(claimed, str) and host(claimed) else None
    why = None
    if domain is None or cite is None:
        why = "no cited website found"
    elif domain in THIRD_PARTY or not on_site(cite.ref, domain):
        why = f"{claimed} is not the organisation's own site, or the cited page is not on it"
    elif not _names_org(f"{cite.quote} {host(cite.ref)}", org):
        why = f"the cited page on {domain} does not name {org}"
    command = 'pennant set-fact <seat> <board_seat|chair|executive> <yes|no> --reason "..."'
    _review(ctx, key, problems, rejected, command)
    if why and not problems:  # an answer, but no own site: its roles stay unknown
        db.put_outputs(ctx.conn, ctx.run_id, "review", [ReviewItem(
            kind="site_not_found", subject=key, detail=f"{org}: {why}; roles there stay unknown",
            suggested_command=command)])
    return (None, None) if why else (domain, cite)


def research_position(person: str, org: str, role_text: str | None, as_of: date | str,
                      domain: str, ctx: RunContext, seat_id: str,
                      key: str | None = None) -> PositionFacts:
    """Board membership and capacity on `as_of` (the AGM date: the vote is due by then),
    from `domain`'s pages only. A role that began after `as_of` or ended before it is not
    current. A stated capacity means a board seat. A job title is not one (the prompt
    says so)."""
    values = {"person": person, "org": org, "role_text": role_text or "not stated",
              "as_of": str(as_of), "today": date.today().isoformat(), "domain": domain}
    key_inputs = {"person": "-".join(org_tokens(person)),
                  "org_key": key or "-".join(org_tokens(org)),
                  "as_of": str(as_of), "domain": domain}
    payload, problems = ask(PROMPT_VERSION["position"], key_inputs,
                            lambda: render("position", **values), ctx, kind="position",
                            domains=[domain])
    v, ev, rejected = validate(payload, domain)
    role_type = CAPACITY.get(str(v.get("capacity")))
    board = YES if role_type else _tri(v.get("board_member"))
    facts: dict[str, Any] = {"board_seat": board}
    if role_type:
        _, facts["chair"], facts["executive"] = FACTS[role_type]
    if board == YES:
        day, start, end = _date(str(as_of)), _date(v.get("start_date")), _date(v.get("end_date"))
        outside = bool(day and ((end and end < day) or (start and start > day)))
        facts["current"] = UNKNOWN if outside else YES
    elif board == NO:
        facts["chair"] = NO
    _review(ctx, seat_id, problems, rejected,
            f'pennant set-fact {seat_id} board_seat <yes|no> --reason "..."')
    return PositionFacts(seat_id=seat_id, role_type=role_type, evidence=list(ev.values()), **facts)


def needs_position(s: Seat) -> bool:
    return (s.current != NO and s.board_seat != NO and s.listed != NO
            and s.role_type not in NOT_A_BOARD
            and UNKNOWN in (s.board_seat, s.chair, s.executive))


def merge_position(seat: Seat, pf: PositionFacts) -> Seat:
    """Fill the seat's unknown role facts (and `current`) from the firm's site; a fact the
    notice or CH already settled is kept. Evidence is added when anything was used."""
    update: dict[str, Any] = {f: getattr(pf, f) for f in (*ROLE_FACTS, "current")
                              if getattr(seat, f) == UNKNOWN and getattr(pf, f) != UNKNOWN}
    vague = {RoleType.other, RoleType.director_unspecified, RoleType.officer_title}
    if pf.role_type and seat.role_type in vague and seat.board_seat != NO:
        update["role_type"] = pf.role_type
    if not update:
        return seat
    return seat.model_copy(update={**update, "evidence": [*seat.evidence, *pf.evidence]})


def targets(seats: list[Seat], orgs: list[Any], meetings: dict[str, Any],
            roster: dict[str, Any], persons: list[Any], officers: dict[str, Any],
            ) -> list[tuple[Seat, str, date]]:
    """(seat, person's name, AGM date) for every seat worth asking about: notice seats and
    the CH-only seats reconcile builds, under the same seat ids the score stage uses."""
    from pennant.policy.reconcile import reconcile
    from pennant.policy.stage import issuer_orgs

    orgs = [*orgs, *issuer_orgs(meetings.values())]
    found: dict[str, tuple[Seat, str, date]] = {}
    for p in sorted(persons, key=lambda p: p.person_id):
        ch = [officers[i] for i in p.officer_ids if i in officers]
        entries = sorted((roster[m] for m in p.mention_ids if m in roster),
                         key=lambda e: (str(meetings[e.meeting_id].agm_date), e.mention_id))
        for e in entries:
            meeting = meetings[e.meeting_id]
            mine = [s for s in seats if s.mention_id in p.mention_ids]
            for s in reconcile(p, meeting, mine, ch or None, orgs)[0]:
                if needs_position(s) and s.seat_id not in found:
                    found[s.seat_id] = (s, e.name, meeting.agm_date)
    return [found[k] for k in sorted(found)]


def research_positions(ctx: RunContext, todo: list[tuple[Seat, str, date]],
                       names: dict[str, str]) -> list[PositionFacts]:
    """One site lookup per organisation, then one site-limited question per seat.
    `names` maps org keys to the best known name (research's canonical name)."""
    sites: dict[str, str | None] = {}
    out = []
    for seat, person, as_of in todo:
        org = names.get(seat.org_key) or seat.org_name
        if seat.org_key not in sites:
            sites[seat.org_key] = research_site(org, ctx, seat.ch_company_number,
                                                seat.org_key)[0]
        if domain := sites[seat.org_key]:
            out.append(research_position(person, org, seat.role_text, as_of, domain, ctx,
                                         seat.seat_id, seat.org_key))
    return out
