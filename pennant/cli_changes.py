"""What a write command will change, and what changed over time, for the CLI.

Design choice: a decision is append-only and replayed on every later run, so a write
command first shows exactly what it will record (the fact, its current and new value, and
each affected director with their vote now) and asks before recording it (default no;
`--yes` skips the question). `pennant history` reads the other way: one director's votes
across every stored run, each change with its cause, and every decision about them.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import typer
from rich.text import Text

from pennant.cli_style import changes, console, dim, vote
from pennant.cli_view import View, fail, print_table, rule_text
from pennant.core import db
from pennant.core.models import Decision, Person
from pennant.state.decisions import decision_no, superseded
from pennant.state.history import decision_history, record_changes, vote_history
from pennant.state.supersede import replaced_by

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


def same_meetings(view: View, pa: str, pb: str) -> str:
    """Meeting keys where both people stand ('' if none, or if either is a bare CH officer)."""
    def meetings(pid: str) -> set[str]:
        p = next((x for x in view.persons if x.person_id == pid), None) if pid else None
        return {view.roster[m].meeting_id for m in (p.mention_ids if p else ())
                if m in view.roster}
    return ", ".join(sorted(view.meeting_key(m) for m in meetings(pa) & meetings(pb)))


def _node_name(view: View, node: str) -> str:
    e = view.roster.get(node)
    return f"{e.name} ({view.mention_label(node)})" if e else node


def replacing(conn: sqlite3.Connection, view: View, kind: str,
              payload: dict) -> tuple[list[str], list[Decision]]:
    """(preview lines, decisions) for the earlier merges or splits a new one replaces."""
    draft = Decision(decision_id="(new)", kind=kind, payload=payload, author="analyst",
                     reason="(new)", created_at=datetime.now(UTC))
    old = replaced_by(db.active_decisions(conn), draft)
    lines = []
    for d in old:
        a, b = (", ".join(_node_name(view, n) for n in _as_list(d.payload[k])) for k in "ab")
        what = (f"your merge of {a} and {b}" if d.kind == "must_link"
                else f"your split of {a} from {b}")
        lines.append(f"This replaces {d.decision_id}, {what} ({d.reason!r}, "
                     f"{str(d.created_at)[:10]}): it is undone first.")
    return lines, old


def _as_list(v: str | list[str]) -> list[str]:
    return [v] if isinstance(v, str) else list(v)


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


# --- every decision ---------------------------------------------------------------

def _named(view: View, d: Decision) -> str:
    """A decision in words, with names where the run knows them."""
    p = d.payload
    if d.kind == "must_link":
        return f"merge {_node_name(view, p['a'])} + {_node_name(view, p['b'])}"
    if d.kind == "cannot_link":
        a, b = (", ".join(_node_name(view, n) for n in _as_list(p[k])) for k in "ab")
        return f"split {a} from {b}"
    if d.kind == "set_fact":
        seat = next((s for s in view.seats if s.seat_id == p["target"]), None)
        where = (f"{seat.org_name} seat ({p['target']})" if seat
                 else _node_name(view, p["target"]))
        return f"set {where} {p['field']} = {p['value']}"
    return describe(d)


def print_decisions(conn: sqlite3.Connection, view: View, active_only: bool) -> None:
    """Every analyst decision, oldest first: what, who, why, and whether it still applies."""
    everything = db.all_decisions(conn)
    revokers: dict[str, list[Decision]] = {}
    for d in everything:
        if d.kind == "revoke":
            revokers.setdefault(d.payload["decision_id"], []).append(d)
    memo: dict[str, bool] = {}

    def effective(i: str) -> bool:  # an undo of an undo brings the decision back
        if i not in memo:
            memo[i] = not any(effective(r.decision_id) for r in revokers.get(i, []))
        return memo[i]

    active = [d for d in everything if d.kind != "revoke" and effective(d.decision_id)]
    newer = {}  # replaced set-fact -> the newer set-fact on the same field
    for old in superseded(active):
        o = next(d for d in active if d.decision_id == old)
        key = (o.payload["kind"], o.payload["target"], o.payload["field"])
        newer[old] = max((d for d in active if d.kind == "set_fact" and (
            d.payload["kind"], d.payload["target"], d.payload["field"]) == key),
            key=decision_no).decision_id
    rows, counts = [], {"active": 0, "undone": 0, "replaced": 0}
    for d in everything:
        undo = next((r for r in revokers.get(d.decision_id, []) if effective(r.decision_id)),
                    None)
        if undo:
            state, kind = f"undone by {undo.decision_id}", "undone"
            if undo.reason.startswith("replaced by"):  # undone by a newer merge or split
                by = next((x for x in everything if x.kind in ("must_link", "cannot_link")
                           and decision_no(x) > decision_no(undo)), None)
                what = {"must_link": "merge", "cannot_link": "split"}.get(getattr(by, "kind", ""))
                state = (f"replaced by {by.decision_id} ({what}; undone by {undo.decision_id})"
                         if by else f"replaced (undone by {undo.decision_id})")
                kind = "replaced"
        elif d.decision_id in newer:
            state, kind = f"replaced by {newer[d.decision_id]}", "replaced"
        else:
            state, kind = "active", "active"
        if d.kind == "revoke":
            kind = "undo"
        else:
            counts[kind] += 1
        if active_only and kind != "active":
            continue
        rows.append([d.decision_id, str(d.created_at)[:16].replace("T", " "), d.author,
                     _named(view, d), d.reason, Text(state, style="green" if kind == "active"
                                                     else "dim")])
    if not everything:
        console().print(dim("No decisions yet: merge, split, set-fact and undo record them."))
        return
    print_table(["Id", "Made", "By", "Decision", "Reason", "Status"], rows,
                title="Analyst decisions" + (" (active only)" if active_only else ""))
    console().print(dim(
        f"  {counts['active']} active · {counts['undone']} undone · {counts['replaced']} "
        "replaced. Active decisions are replayed on every run; `pennant undo <id>` reverses "
        "one, and undo rows are themselves decisions"), soft_wrap=True)
