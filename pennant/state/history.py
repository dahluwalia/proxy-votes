"""One director's record across every run: how each vote moved, and why.

Design choice: history is a read, never a log kept on the side. Run outputs and decisions
are append-only (the database refuses edits), so every past answer is still stored and can
be replayed exactly. A director is followed by their notice mention ids, which stay the same
across runs even when merges and splits change their person id. Each change of vote,
points or rule is attributed the same way `pennant diff` attributes it: the decisions that
touch it, else the inputs that changed between the two runs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from pennant.core import db
from pennant.core.models import Decision, Recommendation
from pennant.state.decisions import touched
from pennant.state.diff import diff
from pennant.state.inputs import load_outputs, recs_by_mention

COMPLETE = ("staged", "held", "published")  # runs whose outputs are whole


@dataclass
class VoteEntry:
    """One run's answer for one notice mention."""
    run_id: str
    status: str
    finished_at: str | None
    was_published: bool
    mention_id: str
    rec: Recommendation
    changed: bool  # vote, points or rule differ from the previous run that scored it
    causes: list[str] = field(default_factory=list)


@dataclass
class DecisionEntry:
    decision: Decision
    revoked_by: list[str]


def _state(rec: Recommendation) -> tuple:
    return rec.vote, rec.points_lo, rec.points_hi, rec.points_rule, rec.exec_rule


def vote_history(conn: sqlite3.Connection, mentions: list[str]) -> list[VoteEntry]:
    """Every run's recommendation for these mentions, oldest first, with the cause of
    each change."""
    entries: list[VoteEntry] = []
    last: dict[str, tuple[str, tuple]] = {}  # mention -> (run it was last scored in, state)
    for run in db.list_runs(conn):
        if run["status"] not in COMPLETE:
            continue
        run_id = run["run_id"]
        recs = recs_by_mention(load_outputs(conn, run_id))
        changes = None
        for m in mentions:
            rec = recs.get(m)
            if rec is None:
                continue
            before = last.get(m)
            changed = before is None or before[1] != _state(rec)
            causes = ["first run"] if before is None else []
            if before and changed:
                changes = changes if changes is not None else diff(conn, before[0], run_id)
                causes = next((c.causes for c in _flatten(changes)
                               if c.kind == "recommendation" and c.subject == m), [])
            entries.append(VoteEntry(run_id, run["status"], run["finished_at"],
                                     run["published_seq"] is not None, m, rec, changed, causes))
            last[m] = (run_id, _state(rec))
    return entries


def _flatten(changes):
    for c in changes:
        yield c
        yield from _flatten(c.children)


def decision_history(conn: sqlite3.Connection, ids: set[str]) -> list[DecisionEntry]:
    """Every decision about these ids (mentions, CH officers, seats, orgs), revokes of them
    included, oldest first, with what revoked each one."""
    everything = db.all_decisions(conn)
    by_id = {d.decision_id: d for d in everything}
    mine = [d for d in everything if touched(d, by_id) & ids]
    revokers: dict[str, list[str]] = {}
    for d in everything:
        if d.kind == "revoke":
            revokers.setdefault(d.payload["decision_id"], []).append(d.decision_id)
    return [DecisionEntry(d, revokers.get(d.decision_id, [])) for d in mine]


def record_changes(conn: sqlite3.Connection, ids: set[str]) -> list[tuple[str, str, object]]:
    """(run_a, run_b, change) for every change between consecutive runs to the director's
    own records: their person (merge, split), their seats and those seats' organisations."""
    out = []  # a failed run's outputs are partial, so it is skipped, not compared
    runs = [r["run_id"] for r in db.list_runs(conn) if r["status"] in COMPLETE]
    for a, b in zip(runs, runs[1:], strict=False):
        for c in _flatten(diff(conn, a, b)):
            if c.kind != "recommendation" and c.subject in ids:
                out.append((a, b, c))
    return out
