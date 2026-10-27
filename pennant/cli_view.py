"""Reading one stored run for the CLI: the run itself, meeting and name lookup, tables.

Design choice: read commands show the published run's `run_outputs` and nothing else, so
the analyst sees exactly what was published. Any other run (nothing published yet, or
`--run`) is labelled NOT PUBLISHED, with its status, wherever it is shown. Names match
case- and accent-insensitively; an ambiguous name lists its candidates instead of guessing.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Any, NoReturn

import typer

from pennant.core import db
from pennant.core.config import slug
from pennant.core.models import Meeting, Person, Recommendation, RosterEntry, person_id
from pennant.evidence.cite import cite
from pennant.evidence.freshness import flagged

_LEGAL = re.compile(r"(\s+group)?\s+(plc|p\.l\.c\.|limited|ltd|inc\.?|n\.v\.|s\.a\.|ag)$", re.I)


def short_name(company: str) -> str:
    """'Marks and Spencer Group plc' -> 'Marks and Spencer': a label for any company."""
    return _LEGAL.sub("", company.strip()) or company


def fail(msg: str) -> NoReturn:
    typer.echo(msg, err=True)
    raise typer.Exit(1)


# --- reading a run -------------------------------------------------------------


class View:
    """Everything one run wrote, indexed for lookup."""

    def __init__(self, conn: sqlite3.Connection, run_id: str):
        def get(kind: str) -> list[Any]:
            return db.get_outputs(conn, run_id, kind)

        self.run_id = run_id
        self.status = (db.get_run(conn, run_id) or {}).get("status", "unknown")
        self.published = run_id == db.published_run_id(conn)
        self.meetings = {m.meeting_id: m for m in get("meeting")}
        self.roster: dict[str, RosterEntry] = {r.mention_id: r for r in get("roster")}
        self.persons: list[Person] = get("person") or [
            Person(person_id=person_id([m]), mention_ids=[m]) for m in self.roster]
        self.person_of = {m: p for p in self.persons for m in p.mention_ids}
        self.seats = get("seat")
        self.orgs = {o.org_key: o for o in get("org")}
        self.officers = {o.officer_id: o for o in get("officer")}
        self.recs: dict[tuple[str, str], Recommendation] = {
            (r.person_id, r.meeting_id): r for r in get("recommendation")}
        self.reviews = get("review")

    def meeting_key(self, meeting_id: str) -> str:
        """The key from the notice's filename, else one made from the company name."""
        m = self.meetings.get(meeting_id)
        return (m.key or slug(short_name(m.company_name))) if m else meeting_id

    def meeting_label(self, meeting_id: str) -> str:
        m = self.meetings.get(meeting_id)
        return short_name(m.company_name) if m else meeting_id

    def find_meetings(self, query: str) -> list[Meeting]:
        """Meetings matching a key, a company number or part of a company name. An exact
        key wins, so 'severn-trent' never also picks up a 'severn-trent-water'."""
        q, ms = slug(query), list(self.meetings.values())
        exact = [m for m in ms if self.meeting_key(m.meeting_id) == q
                 or m.company_number == query.strip().zfill(8)]
        return exact or [m for m in ms if q and (q in slug(m.company_name)
                                                 or q in self.meeting_key(m.meeting_id))]

    def tag(self) -> str:
        """The run a heading comes from; anything but the published run says so."""
        return (f"[{self.run_id}]" if self.published
                else f"[{self.run_id}, {self.status}, NOT PUBLISHED]")

    def mention_label(self, mention_id: str) -> str:
        e = self.roster[mention_id]
        return f"{self.meeting_label(e.meeting_id)} r{e.resolution_no}"

    def name(self, person: Person) -> str:
        return next((self.roster[m].name for m in sorted(person.mention_ids) if m in self.roster),
                    person.person_id)


def load_view(conn: sqlite3.Connection, run_id: str | None = None) -> View:
    """The given run, else the published run, else the latest run (with a warning)."""
    if run_id is None:
        run_id = db.published_run_id(conn)
        if run_id is None:
            runs = db.list_runs(conn)
            if not runs:
                fail("No runs yet. Start with: pennant run")
            run_id = runs[-1]["run_id"]
            typer.echo(f"NOT PUBLISHED: nothing is published yet; showing the latest run, "
                       f"{run_id} ({runs[-1]['status']}). These are not final votes.")
    elif db.get_run(conn, run_id) is None:
        fail(f"No such run: {run_id}")
    return View(conn, run_id)


def _fold(text: str) -> list[str]:
    s = unicodedata.normalize("NFKD", text).casefold()
    s = "".join(c if c.isalnum() else " " for c in s if not unicodedata.combining(c))
    return s.split()


def find_people(view: View, query: str) -> list[Person]:
    """People whose id, mention id or name matches; names are case- and accent-insensitive
    and match when every query word is one of the name's words."""
    by_id = {p.person_id: p for p in view.persons}
    if query in by_id:
        return [by_id[query]]
    if query in view.person_of:
        return [view.person_of[query]]
    q = _fold(query)
    hits: dict[str, Person] = {}
    for mid, entry in sorted(view.roster.items()):
        words = _fold(entry.name)
        if q and (q == words or all(w in words for w in q)) and mid in view.person_of:
            hits.setdefault(view.person_of[mid].person_id, view.person_of[mid])
    return list(hits.values())


def resolve_person(view: View, query: str) -> Person:
    people = find_people(view, query)
    if len(people) == 1:
        return people[0]
    if not people:
        fail(f"No director matches {query!r}.")
    lines = [f"{query!r} is ambiguous; use one of these ids:"]
    for p in people:
        where = ", ".join(view.mention_label(m) for m in sorted(p.mention_ids) if m in view.roster)
        lines.append(f"  {p.person_id}  {view.name(p)}  ({where})")
    fail("\n".join(lines))


def rule_label(rec: Recommendation) -> str:
    rules = (("exec", rec.exec_rule), ("points", rec.points_rule))
    return "+".join(n for n, v in rules if v == "yes") or " ".join(
        f"{n}?" for n, v in rules if v == "unknown") or "-"


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    for row in [headers, *rows]:
        typer.echo("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())


OFFICER_ID = re.compile(r"[A-Za-z0-9_-]{20,40}")  # CH officer ids, e.g. 3foiCz6Hg_2s4JTbIRFd01JBwG4


def node_id(view: View, query: str) -> tuple[str, str]:
    """(node id, person id or ""): a person's smallest mention id, or a CH officer id as
    given, which is what `unanchored` and `possible_same_person` items suggest merging."""
    q = query.strip()
    owner = next((p for p in view.persons if q in p.officer_ids), None)
    if owner or (OFFICER_ID.fullmatch(q) and not find_people(view, q)):
        return q, owner.person_id if owner else ""
    p = resolve_person(view, q)
    return min(p.mention_ids), p.person_id


# --- printing --------------------------------------------------------------------

SEAT_FIELDS = ("board_seat", "chair", "executive", "current", "listed", "identity")
MAX_PROBLEMS = 10  # per gate in the printed report; the full list stays in runs.gate_report
ROLE_LABELS = {"executive_director": "Executive director", "executive_chair": "Executive chair",
               "chair": "Chair", "deputy_chair": "Deputy chair", "non_executive_director": "NED",
               "director_unspecified": "Director", "officer_title": "Officer (board unknown)",
               "committee_chair": "Committee chair", "adviser": "Adviser",
               "trustee": "Trustee", "other": "Other", None: "(not stated)"}


def print_votes(view: View, meetings: list[Meeting]) -> None:
    """Role, points lo-hi, rule, vote and coverage per director, a table per meeting."""
    for m in meetings:
        typer.echo(f"\n{m.company_name} — AGM {m.agm_date}  {view.tag()}")
        rows = []
        for e in sorted((e for e in view.roster.values() if e.meeting_id == m.meeting_id),
                        key=lambda e: e.resolution_no):
            p = view.person_of.get(e.mention_id)
            rec = view.recs.get((p.person_id, m.meeting_id)) if p else None
            if rec is None:
                rows.append([str(e.resolution_no), e.name, "", "", "", "MISSING", e.coverage])
                continue
            rows.append([str(e.resolution_no), e.name, ROLE_LABELS.get(rec.role_at_meeting,
                         str(rec.role_at_meeting)), f"{rec.points_lo}–{rec.points_hi}",
                         rule_label(rec), rec.vote, rec.coverage])
        print_table(["Res", "Director", "Role", "Points", "Rule", "Vote", "Coverage"], rows)


def _facts(obj: Any, fields: tuple[str, ...]) -> str:
    return " · ".join(f"{f} {getattr(obj, f)}" for f in fields)


def print_person(view: View, p: Person) -> None:
    """Mentions, CH records, seats with citations, votes and the facts that could flip them."""
    echo = typer.echo
    echo(f"{view.name(p)}  ({p.person_id})  {view.tag()}\n\nMentions")
    for mid in sorted(p.mention_ids):
        e = view.roster.get(mid)
        if e:
            role = e.role_text_at_issuer or "(role not stated)"
            echo(f"  {view.mention_label(mid)}: {e.name}, {role}; coverage {e.coverage}  [{mid}]")
            for ev in e.evidence:
                echo(f"      {cite(ev)}")
    stale = flagged(view.reviews)

    def cited(ev: Any, *subjects: str) -> str:
        return cite(ev) + ("  [STALE]" if any((s, ev.ref) in stale for s in subjects) else "")

    echo("Companies House records")
    for oid in p.officer_ids:
        o = view.officers.get(oid)
        born = f", born {o.dob_month}/{o.dob_year}" if o and o.dob_year else ""
        echo(f"  {oid}" + (f"  {o.name}{born}" if o else ""))
    if not p.officer_ids:
        echo("  (none linked)")
    for ev in p.evidence:  # why the mentions were joined
        echo(f"      {cited(ev, p.person_id)}")
    seats = [s for s in view.seats if s.mention_id in p.mention_ids
             or (s.officer_id and s.officer_id in p.officer_ids)]
    echo("Seats")
    for s in sorted(seats, key=lambda s: (s.org_name.casefold(), s.seat_id)):
        echo(f"  {s.org_name} — {s.role_text or s.role_type}  [{s.status or 'unreconciled'}]"
             f"  {s.seat_id}")
        echo(f"      {_facts(s, SEAT_FIELDS)}")
        for ev in s.evidence:
            echo(f"      {cited(ev, p.person_id, s.seat_id)}")
        org = view.orgs.get(s.org_key)
        if org:
            market = f" ({org.market})" if org.market else ""
            parent = f"; parent {org.parent_name}" if org.parent_name else ""
            echo(f"      org {org.org_key}: listed {org.listed}{market}{parent}")
            for ev in org.evidence:
                echo(f"        {cited(ev, org.org_key)}")
    for (pid, mtg), rec in sorted(view.recs.items()):
        if pid != p.person_id:
            continue
        echo(f"Vote at {view.meeting_label(mtg)}: {rec.vote}  "
             + ("" if view.published else f"(NOT PUBLISHED: {view.run_id} is {view.status})  ")
             + "points "
             f"{rec.points_lo}–{rec.points_hi}  points_rule {rec.points_rule}  "
             f"exec_rule {rec.exec_rule}")
        for r in rec.reasons:
            echo(f"  - {r}")
        if rec.unknown_facts:
            echo("  Would flip the vote: " + ", ".join(rec.unknown_facts))
    subjects = {p.person_id, *p.mention_ids, *p.officer_ids, *(s.seat_id for s in seats),
                *(s.org_key for s in seats)}
    items = [i for i in view.reviews if i.subject in subjects]
    if items:
        echo("Review items")
        print_reviews(items)


def print_reviews(items: list[Any]) -> None:
    for i in sorted(items, key=lambda i: (i.kind, i.subject, i.detail)):
        typer.echo(f"  [{i.kind}] {i.subject}: {i.detail}")
        typer.echo(f"      -> {i.suggested_command or '(no suggested command)'}")


def print_gate_report(conn: sqlite3.Connection, run_id: str) -> str:
    """The stored gate report: {"gates": {G1: {ok, skipped, problems}}, "error": ...}."""
    run = db.get_run(conn, run_id) or {}
    status, report = run.get("status", "unknown"), run.get("gate_report") or {}
    typer.echo(f"{run_id}: {status}")
    if report.get("error"):
        typer.echo(f"  error: {report['error']}")
    for name, g in sorted((report.get("gates") or {}).items()):
        state = "skipped" if g.get("skipped") else "pass" if g.get("ok") else "FAIL"
        typer.echo(f"  {name} {state}")
        problems = g.get("problems") or []
        for problem in problems[:MAX_PROBLEMS]:
            typer.echo(f"    - {problem}")
        if len(problems) > MAX_PROBLEMS:
            typer.echo(f"    … and {len(problems) - MAX_PROBLEMS} more")
    if status != "published":
        pub = db.published_run_id(conn)
        typer.echo(f"  Not published; votes still come from {pub}." if pub else
                   "  Not published, and no run is published yet: `pennant votes` shows the "
                   "latest run marked NOT PUBLISHED.")
    return status
