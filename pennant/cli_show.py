"""`pennant show`: one director, vote first, then the facts behind it.

Design choice: the vote and what would change it come first, then compact tables
(mentions, Companies House records, seats with ✓ / ✗ / ? facts), then the citations. A
seat's own citations always print; an organisation's listing sources are summarised as a
count unless one is stale or `--evidence` asks for all of them. Citation lines never wrap,
so a URL stays copyable.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

from pennant.cli_style import STATUS_STYLE, console, dim, styled, table, tri, vote
from pennant.cli_view import ROLE_LABELS, View, print_reviews
from pennant.core.models import Person
from pennant.evidence.cite import cite
from pennant.evidence.freshness import flagged

SEAT_FACTS = ("board_seat", "chair", "executive", "current", "listed", "identity")


def print_person(view: View, p: Person, evidence: bool = False) -> None:
    c = console()
    c.print(Text.assemble((view.name(p), "bold"), f"  ({p.person_id})  ", (view.tag(), "dim")))
    stale = flagged(view.reviews)

    def line(ev: Any, *subjects: str, indent: str = "    ") -> None:
        mark = "  [STALE]" if any((s, ev.ref) in stale for s in subjects) else ""
        c.print(Text(indent + cite(ev) + mark, style="yellow" if mark else ""), soft_wrap=True)

    # 1. The vote at each meeting, and what would change it.
    for (pid, mtg), rec in sorted(view.recs.items()):
        if pid != p.person_id:
            continue
        pts = (f"{rec.points_lo}" if rec.points_lo == rec.points_hi
               else f"{rec.points_lo}–{rec.points_hi}")
        c.print()
        c.print(Text.assemble(
            "Vote at ", view.meeting_label(mtg), ": ", vote(rec.vote),
            ("" if view.published else f"  (NOT PUBLISHED: {view.run_id} is {view.status})",
             "red"),
            f"   {pts} points   points rule {rec.points_rule} · executive rule {rec.exec_rule}"),
            soft_wrap=True)
        for r in rec.reasons:
            c.print(Text("  • " + r), soft_wrap=True)
        if rec.unknown_facts:
            c.print(Text("  Would flip the vote: " + ", ".join(rec.unknown_facts),
                         style="yellow"), soft_wrap=True)

    # 2. Where they are named, and their Companies House records.
    c.print()
    t = table("Mentions", "Notice", "Name as written", "Role at the company", "Coverage",
              "Mention id")
    for mid in sorted(p.mention_ids):
        e = view.roster.get(mid)
        if e:
            t.add_row(view.mention_label(mid), e.name, e.role_text_at_issuer or "(not stated)",
                      e.coverage, dim(mid))
    c.print(t)
    c.print()
    t = table("Companies House records", "Officer id", "Name", "Born")
    for oid in p.officer_ids:
        o = view.officers.get(oid)
        born = f"{o.dob_month}/{o.dob_year}" if o and o.dob_year else "-"
        t.add_row(oid, o.name if o else "-", born)
    if not p.officer_ids:
        t.add_row("(none linked)", "", "")
    c.print(t)
    if p.evidence:
        c.print(dim("  Why these records are one person:"))
        for ev in p.evidence:
            line(ev, p.person_id)

    # 3. Seats: the facts the vote used.
    seats = sorted((s for s in view.seats if s.mention_id in p.mention_ids
                    or (s.officer_id and s.officer_id in p.officer_ids)),
                   key=lambda s: (s.org_name.casefold(), s.seat_id))
    c.print()
    t = table("Seats", "Organisation", "Role", "Status", "Board", "Chair", "Exec", "Current",
              "Listed", "ID", caption="✓ yes   ✗ no   ? unknown")
    for s in seats:
        t.add_row(s.org_name, s.role_text or ROLE_LABELS.get(s.role_type, str(s.role_type)),
                  styled(s.status or "unreconciled", STATUS_STYLE),
                  *(tri(getattr(s, f)) for f in SEAT_FACTS))
    c.print(t)

    # 4. Citations, per seat.
    c.print(Text("\nSources", style="bold"))
    for s in seats:
        c.print(Text(f"  {s.org_name}", style="bold"), dim(f"  {s.seat_id}"), soft_wrap=True)
        for ev in s.evidence:
            line(ev, p.person_id, s.seat_id)
        org = view.orgs.get(s.org_key)
        if org is None:
            continue
        market = f" on {org.market}" if org.market else ""
        parent = f"; parent {org.parent_name}" if org.parent_name else ""
        c.print(dim(f"    {org.canonical_name or org.org_key}: listed {org.listed}{market}"
                    f"{parent}"), soft_wrap=True)
        shown = [ev for ev in org.evidence if evidence or (org.org_key, ev.ref) in stale]
        for ev in shown:
            line(ev, org.org_key, indent="      ")
        hidden = len(org.evidence) - len(shown)
        if hidden:
            c.print(dim(f"      + {hidden} source{'s' * (hidden != 1)} for this listing "
                        "(pennant show --evidence lists them)"))

    # 5. What the analyst could settle.
    subjects = {p.person_id, *p.mention_ids, *p.officer_ids, *(s.seat_id for s in seats),
                *(s.org_key for s in seats)}
    items = [i for i in view.reviews if i.subject in subjects]
    if items:
        c.print(Text("\nReview items", style="bold"))
        print_reviews(items)
