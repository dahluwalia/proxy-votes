"""What a write command will change, and what changed over time, for the CLI.

Design choice: a decision is append-only and replayed on every later run, so a write
command first shows exactly what it will record (the fact, its current and new value, and
each affected director with their vote now) and asks before recording it (default no;
`--yes` skips the question). `pennant history` reads the other way: one director's votes
across every stored run, each change with its cause, and every decision about them.
"""

from __future__ import annotations

import sqlite3

import typer
from rich.text import Text

from pennant.cli_style import changes, console, dim, vote
from pennant.cli_view import View, fail, print_table, rule_text
from pennant.core import db
from pennant.core.models import Decision, Person
from pennant.state.history import decision_history, record_changes, vote_history

WARNING = ("This is appended to the decision log: every later run replays it until you "
           "`pennant undo` it. The pipeline re-runs now and publishes only if every gate passes.")


def confirm(preview: list[str], yes: bool) -> None:
    """Show exactly what a write command will record, then ask (default no)."""
    for line in preview:
        typer.echo(line)
    typer.echo(WARNING)
    if not yes and not typer.confirm("Record it?", default=False):
        fail("Nothing recorded.")


def who(view: View, p: Person) -> str:
    """'Chris Kennedy (Tesco r9: AGAINST, ITV r12: AGAINST)': a person and their votes now."""
    votes = [f"{view.mention_label(m)}: {rec.vote}" for m in sorted(p.mention_ids)
             if m in view.roster and (rec := view.recs.get((p.person_id,
                                                            view.roster[m].meeting_id)))]
    return f"{view.name(p)} ({', '.join(votes) or p.person_id})"


def node_label(view: View, node: str) -> str:
    p = view.person_of.get(node) or next((x for x in view.persons if node in x.officer_ids), None)
    officer = view.officers.get(node)
    return (who(view, p) if p else
            f"CH officer {node}" + (f" ({officer.name})" if officer else ""))


def set_fact_preview(view: View, kind: str, subject: str, field: str, value: str) -> list[str]:
    """'Set seat X board_seat: unknown -> no', plus each affected person and their votes."""
    if kind == "seat":
        target = next(s for s in view.seats if s.seat_id == subject)
        affected = [p for p in view.persons if target.mention_id in p.mention_ids
                    or (target.officer_id and target.officer_id in p.officer_ids)]
        label = f"{target.org_name} — {target.role_text or target.role_type}"
    elif kind == "org":
        target = view.orgs[subject]
        at_org = [s for s in view.seats if s.org_key == subject]
        affected = [p for p in view.persons if any(s.mention_id in p.mention_ids
                    or (s.officer_id and s.officer_id in p.officer_ids) for s in at_org)]
        label = target.canonical_name
    else:
        target = view.roster[subject]
        affected = [view.person_of[subject]] if subject in view.person_of else []
        label = f"{target.name} at {view.mention_label(subject)}"
    current = getattr(target, field, None)
    return [f"Set {kind} {subject} ({label}) {field}: {current} → {value}",
            *(f"  affects {who(view, p)}" for p in affected)]





def describe(d: Decision) -> str:
    """A decision in words: 'set seat:x board_seat = no', 'merge a + b', 'undo dec:1'."""
    p = d.payload
    if d.kind == "set_fact":
        return f"set {p['kind']} {p['target']} {p['field']} = {p['value']}"
    if d.kind == "must_link":
        return f"merge {p['a']} + {p['b']}"
    if d.kind == "cannot_link":
        return f"split {', '.join(p['a'])} from {', '.join(p['b'])}"
    return f"undo {p['decision_id']}"


def print_history(conn: sqlite3.Connection, view: View, p: Person, every_run: bool) -> None:
    """One director's votes across runs (changes only, unless `every_run`) and the decisions
    about them, oldest first."""
    c = console()
    c.print(Text.assemble((view.name(p), "bold"), f"  ({p.person_id})"))
    entries = vote_history(conn, sorted(p.mention_ids))
    now = db.published_run_id(conn)
    for m in sorted(p.mention_ids):
        rows = [[e.run_id + (" *" if e.run_id == now else ""), e.status,
                 (e.finished_at or "")[:16].replace("T", " "), vote(e.rec.vote),
                 f"{e.rec.points_lo}–{e.rec.points_hi}", rule_text(e.rec),
                 changes(", ".join(e.causes)) if e.changed else ""]
                for e in entries if e.mention_id == m and (e.changed or every_run)]
        label = view.mention_label(m) if m in view.roster else m
        c.print()
        print_table(["Run", "Status", "Finished", "Vote", "Points", "Rule", "Why"], rows,
                    title=f"Votes at {label}" + ("" if every_run else
                                                 " (runs where it changed)"))
    if now:
        c.print(dim(f"* the published run ({now})"))
    seats = [s for s in view.seats if s.mention_id in p.mention_ids
             or (s.officer_id and s.officer_id in p.officer_ids)]
    ids = {*p.mention_ids, *p.officer_ids, p.person_id, *(s.seat_id for s in seats),
           *(s.org_key for s in seats)}
    c.print(Text("\nChanges to their records (person, seats, organisations)", style="bold"))
    found = record_changes(conn, ids)
    for a, b, ch in found:
        c.print(changes(f"  {a} → {b}  {ch.detail}  ({', '.join(ch.causes)})"), soft_wrap=True)
    if not found:
        c.print(dim("  (none)"))
    c.print(Text("\nDecisions about this director", style="bold"))
    rows = [[d.decision.decision_id, str(d.decision.created_at)[:16].replace("T", " "),
             d.decision.author, describe(d.decision), d.decision.reason,
             f"revoked by {', '.join(d.revoked_by)}" if d.revoked_by else ""]
            for d in decision_history(conn, ids)]
    if rows:
        print_table(["Id", "Made", "By", "Decision", "Reason", ""], rows)
    else:
        c.print(dim("  (none)"))
