"""`pennant show`: one director, vote first, then the facts behind it.

Design choice: the output explains itself. The vote comes first, in words: why it is
what it is, each rule with its numbers, and every unknown fact that decides it, as a
question with the command that settles it. Then compact tables (mentions, Companies
House records, seats with ✓ / ✗ / ? facts, and one sources table per seat), each with a
key to the codes it uses. Links are numbered under each sources table, never inside a
cell, so a URL stays on one line and copyable. An organisation's listing sources are
counted unless one is stale or `--evidence` asks for all of them.
"""

from __future__ import annotations

from typing import Any

from rich.padding import Padding
from rich.text import Text

from pennant.cli_keys import (
    COVERAGE,
    FACT_QUESTION,
    POINTS_KEY,
    SEAT_COLUMNS,
    STATUS,
    TRI_KEY,
    VOTE_KEY,
    is_summary_reason,
    rule_lines,
    why_line,
)
from pennant.cli_style import STATUS_STYLE, console, dim, styled, table, tri, vote
from pennant.cli_view import ROLE_LABELS, View, print_reviews
from pennant.core.config import notice_label
from pennant.core.models import Evidence, Person, Seat, parse_notice_ref
from pennant.evidence.freshness import checked, flagged, local_day
from pennant.policy.reconcile import not_counted

SEAT_FACTS = ("board_seat", "chair", "executive", "current", "listed", "identity")


def _key(c: Any, *lines: str) -> None:
    for line in lines:
        c.print(dim(f"  {line}"), soft_wrap=True)


def _unknown(view: View, target: str, seats: dict[str, Seat]) -> tuple[str, str]:
    """(question in words, command that settles it) for one 'Would flip' target."""
    subject, _, field = target.rpartition(".")
    question = FACT_QUESTION.get(field, field)
    if field == "ch_record":
        return f"Companies House: {question}", "see the review items below"
    if field == "role_type_at_issuer":
        where = view.mention_label(subject) if subject in view.roster else subject
        return (f"{where}: {question}", f"pennant set-fact {subject} role_type_at_issuer "
                "<non_executive_director|executive_director|chair> --reason \"...\"")
    seat = seats.get(subject)
    label = f"{seat.org_name} ({seat.role_text or seat.role_type.value})" if seat else subject
    return f"{label}: {question}", f'pennant set-fact {subject} {field} <yes|no> --reason "..."'


def _print_votes(c: Any, view: View, p: Person, seats: dict[str, Seat]) -> None:
    for (pid, mtg), rec in sorted(view.recs.items()):
        if pid != p.person_id:
            continue
        pts = (f"{rec.points_lo}" if rec.points_lo == rec.points_hi
               else f"{rec.points_lo}–{rec.points_hi}")
        c.print()
        c.print(Text.assemble(
            ("Vote at ", "bold"), (view.meeting_label(mtg), "bold"), ": ", vote(rec.vote),
            ("" if view.published else f"  (NOT PUBLISHED: {view.run_id} is {view.status})",
             "red"), f"   {pts} points"), soft_wrap=True)
        c.print(Text("  " + why_line(rec)), soft_wrap=True)
        for line in rule_lines(rec):
            c.print(Text("  " + line), soft_wrap=True)
        seat_lines = [r for r in rec.reasons if " pt" in r and not is_summary_reason(r)]
        notes = [r for r in rec.reasons if r not in seat_lines and not is_summary_reason(r)]
        if seat_lines:
            c.print(Text("  Seats that score points or are executive:"))
            for r in seat_lines:
                c.print(Text("    • " + r), soft_wrap=True)
        for r in notes:
            c.print(Text("  Note: " + r), soft_wrap=True)
        if rec.unknown_facts:
            c.print(Text("  Unknown facts that decide this vote (settle one to move it to FOR "
                         "or AGAINST):", style="yellow"), soft_wrap=True)
            for target in rec.unknown_facts:
                question, command = _unknown(view, target, seats)
                c.print(Text(f"    • {question}", style="yellow"), soft_wrap=True)
                c.print(Text(f"        -> {command}", style="green"), soft_wrap=True)
    _key(c, f"Key: {VOTE_KEY}", POINTS_KEY, "[status] after a seat: see the Seats key below")


def _from(ev: Evidence, link: int | None) -> str:
    n = f" [{link}]" if link else ""
    match ev.source:
        case "notice":
            doc, page = parse_notice_ref(ev.ref)
            return f"{notice_label(doc)} notice" + (f" p.{page}" if page else "")
        case "ch":
            return f"Companies House{n}"
        case "web":
            return f"Web{n}"
        case "analyst":
            return f"Analyst ({ev.ref})"
    return ev.source


def _last_checked(ev: Evidence, run_day: str) -> str:
    """The last day this source confirmed the fact. A notice quote is found again in the
    PDF on every run; an analyst's evidence is dated when the decision was recorded."""
    if ev.source == "notice":
        return run_day
    when = checked(ev)
    return str(local_day(when)) if when else "-"


def _sources_table(c: Any, rows: list[tuple[Evidence, bool]], title: str | None,
                   run_day: str) -> None:
    """One table of evidence: where from, what it says, last checked; links below it."""
    t = table(title, "From", "Says", "Last checked")
    links: list[str] = []
    for ev, is_stale in rows:
        link = None
        if ev.source in ("ch", "web"):
            links.append(" ".join(x for x in (ev.title, ev.ref) if x))
            link = len(links)
        when = _last_checked(ev, run_day)
        style = "yellow" if is_stale else ""
        t.add_row(Text(_from(ev, link), style=style),
                  Text(f'"{" ".join(ev.quote.split())}"' if ev.quote else "-", style=style),
                  Text(when + ("  STALE" if is_stale else ""), style=style))
    c.print(Padding(t, (0, 0, 0, 4), expand=False))
    for i, url in enumerate(links, 1):
        c.print(dim(f"    [{i}] {url}"), soft_wrap=True)


def _print_not_counted(c: Any, view: View, p: Person, seats: list[Seat]) -> None:
    """The person's other CH appointments, which never became seats, and why."""
    ch = [view.officers[o] for o in p.officer_ids if o in view.officers]
    agms = [view.meetings[e.meeting_id].agm_date for m in p.mention_ids
            if (e := view.roster.get(m)) and e.meeting_id in view.meetings]
    rows = not_counted(seats, ch, agms, list(view.orgs.values()))
    if not rows:
        return
    c.print()
    t = table("Other Companies House appointments (not counted)", "Company", "Role",
              "Appointed", "Ended", "Why it doesn't count")
    for _, a, why in rows:
        t.add_row(a.company_name or a.company_number, a.role, str(a.appointed_on or "-"),
                  str(a.resigned_on or "-"), why)
    c.print(t)
    _key(c, "Directorships Companies House holds for this person that can't change the vote: "
            "a seat counts only if it is held on the AGM date at a listed organisation, and "
            "UK private companies, LLPs and guarantee companies (often charities) can't list")


def print_person(view: View, p: Person, evidence: bool = False) -> None:
    c = console()
    c.print(Text.assemble((view.name(p), "bold"), f"  ({p.person_id})  ", (view.tag(), "dim")))
    stale = flagged(view.reviews)
    seats = sorted((s for s in view.seats if s.mention_id in p.mention_ids
                    or (s.officer_id and s.officer_id in p.officer_ids)),
                   key=lambda s: (s.org_name.casefold(), s.seat_id))

    # 1. The vote at each meeting, in words, and what would change it.
    _print_votes(c, view, p, {s.seat_id: s for s in seats})

    # 2. Where they are named, and their Companies House records.
    c.print()
    t = table("Mentions (where the notices name this director)", "Notice", "Name as written",
              "Role at the company", "Coverage", "Mention id")
    used = set()
    for mid in sorted(p.mention_ids):
        e = view.roster.get(mid)
        if e:
            t.add_row(view.mention_label(mid), e.name, e.role_text_at_issuer or "(not stated)",
                      e.coverage, dim(mid))
            used.add(e.coverage)
    c.print(t)
    _key(c, *(f"Coverage {k}: {COVERAGE[k]}" for k in COVERAGE if k in used))
    c.print()
    t = table("Companies House records", "Officer id", "Name", "Born")
    for oid in p.officer_ids:
        o = view.officers.get(oid)
        born = f"{o.dob_month}/{o.dob_year}" if o and o.dob_year else "-"
        t.add_row(oid, o.name if o else "-", born)
    if not p.officer_ids:
        t.add_row("(none linked)", "", "")
    c.print(t)
    _key(c, "Each officer id is one Companies House record; records are linked to the notice "
            "by the company's own officer list, then by name and month of birth")
    if p.evidence:
        _sources_table(c, [(ev, (p.person_id, ev.ref) in stale) for ev in p.evidence],
                       "Why these records are one person", view.run_day)

    # 3. Seats: the facts the vote used.
    c.print()
    t = table("Seats (every role found, and the facts the vote used)", "Organisation", "Role",
              "Status", "Board", "Chair", "Exec", "Current", "Listed", "ID")
    for s in seats:
        t.add_row(s.org_name, s.role_text or ROLE_LABELS.get(s.role_type, str(s.role_type)),
                  styled(s.status or "unreconciled", STATUS_STYLE),
                  *(tri(getattr(s, f)) for f in SEAT_FACTS))
    c.print(t)
    statuses = {str(s.status or "unreconciled") for s in seats}
    _key(c, TRI_KEY, SEAT_COLUMNS,
         *(f"Status {k}: {v}" for k, v in STATUS.items() if k in statuses))
    _print_not_counted(c, view, p, seats)

    # 4. Sources, one table per seat.
    c.print(Text("\nSources", style="bold"))
    _key(c, "Where each fact comes from: the notice (page and exact words), the Companies "
            "House register, and web pages behind each organisation's listed status. Last "
            "checked: the last day the source confirmed it (a notice quote is found again in "
            "the PDF on every run). STALE: last checked before the AGM, so it may be out of "
            "date")
    for s in seats:
        c.print()
        c.print(Text(f"  {s.org_name}", style="bold"), dim(f"  {s.seat_id}"), soft_wrap=True)
        rows = [(ev, any((x, ev.ref) in stale for x in (p.person_id, s.seat_id)))
                for ev in s.evidence]
        org = view.orgs.get(s.org_key)
        if org is not None:
            shown = [ev for ev in org.evidence if evidence or (org.org_key, ev.ref) in stale]
            rows += [(ev, (org.org_key, ev.ref) in stale) for ev in shown]
        _sources_table(c, rows, None, view.run_day)
        if org is not None:
            market = f" on {org.market}" if org.market else ""
            parent = f"; parent {org.parent_name}" if org.parent_name else ""
            hidden = len(org.evidence) - len(shown)
            more = (f" ({hidden} web source{'s' * (hidden != 1)}; `pennant show --evidence` "
                    "lists them)") if hidden else ""
            c.print(dim(f"    Listed: {org.listed}{market}{parent}{more}"), soft_wrap=True)

    # 5. What the analyst could settle.
    subjects = {p.person_id, *p.mention_ids, *p.officer_ids, *(s.seat_id for s in seats),
                *(s.org_key for s in seats)}
    items = [i for i in view.reviews if i.subject in subjects]
    if items:
        c.print(Text("\nReview items", style="bold"))
        _key(c, "What the pipeline couldn't settle on its own; each ends with the command "
                "that settles it")
        print_reviews(items)
