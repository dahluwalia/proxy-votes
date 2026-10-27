"""Compare two runs: people merged or split, seats and orgs changed, votes changed.

Design choice: diff is a pure read over run_outputs plus the inputs each run
recorded (notice hashes, policy, cache and decision high-water marks), so any
two runs, rollback targets included, can be compared. Votes are matched by
notice mention, not person id, so a merge can't hide a vote change behind a
renamed person. Each change names its likely causes: the decisions that touch
it if any, otherwise every input that changed between the two runs and any CH
outage in the newer run.

Gate G3 asks a narrower question per vote, with the helpers in state/inputs.py.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from pydantic import BaseModel

from pennant.core import db
from pennant.core.models import Recommendation
from pennant.state.decisions import touched
from pennant.state.identity import absorbed
from pennant.state.inputs import (
    input_changes,
    load_outputs,
    outages,
    person_ids,
    recs_by_mention,
    stored_inputs,
)

SEAT_FIELDS = ("role_type", "board_seat", "chair", "executive", "current", "listed",
               "identity", "status")
ORG_FIELDS = ("listed", "market", "equity_listing", "parent_name", "org_type")
REC_FIELDS = ("vote", "points", "points_rule", "exec_rule", "role_at_meeting")


class Change(BaseModel):
    kind: str  # merged, split, relinked, seat, org, recommendation
    subject: str
    detail: str
    causes: list[str] = []
    children: list[Change] = []


# --- diff --------------------------------------------------------------------


def diff(conn: sqlite3.Connection, run_a: str | None, run_b: str) -> list[Change]:
    """Changes from run_a to run_b; an empty list when nothing changed.
    run_a may be None (no earlier run), which reports everything as added."""
    a, b = load_outputs(conn, run_a), load_outputs(conn, run_b)
    inputs = input_changes(conn, stored_inputs(conn, run_a), stored_inputs(conn, run_b))
    by_id = {d.decision_id: d for d in db.all_decisions(conn)}
    between = [by_id[i] for i in inputs["decisions"]]
    down = [f"{r.kind}: {r.subject}" for r in outages(b)]  # an outage changes answers too
    general = [*inputs["decisions"], *_summarise(inputs), *down] or ["no recorded input change"]

    def causes(ids: set[str]) -> list[str]:
        return [d.decision_id for d in between if touched(d, by_id) & ids] or general

    people = _people_changes(a, b, causes)
    recs = _rec_changes(a, b, causes, by_id)
    for change in people:  # a vote change sits under the merge or split behind it
        change.children = [recs.pop(m) for m in _mentions_of(change) if m in recs]
    return [
        *people,
        *_field_changes("seat", "seat_id", SEAT_FIELDS, a, b, causes),
        *_field_changes("org", "org_key", ORG_FIELDS, a, b, causes),
        *recs.values(),
    ]


def _summarise(inputs: dict[str, list[str]]) -> list[str]:
    out = [f"notice changed: {n}" for n in inputs["notices"]]
    out += [f"policy {p}" for p in inputs["policy"]]
    if inputs["cache"]:
        counts = Counter(k.split(":", 1)[0] for k in inputs["cache"])
        out.append("cache: " + ", ".join(f"{n} {ns}" for ns, n in sorted(counts.items()))
                   + " changed")
    return out


def _mentions_of(change: Change) -> list[str]:
    return change.subject.split(",")


def _people_changes(a, b, causes) -> list[Change]:
    pa = {m: p for p in a["person"] for m in p.mention_ids}
    pb = {m: p for p in b["person"] for m in p.mention_ids}
    out = []
    for p in b["person"]:
        sources = sorted({pa[m].person_id for m in p.mention_ids if m in pa})
        if len(sources) > 1:
            ids = {*p.mention_ids, *p.officer_ids}
            out.append(Change(kind="merged", subject=",".join(sorted(p.mention_ids)),
                              detail=" merged with ".join(sources), causes=causes(ids)))
    for p in a["person"]:
        dests = sorted({pb[m].person_id for m in p.mention_ids if m in pb})
        if len(dests) > 1:
            ids = {*p.mention_ids, *p.officer_ids}
            out.append(Change(kind="split", subject=",".join(sorted(p.mention_ids)),
                              detail=f"{p.person_id} split into {', '.join(dests)}",
                              causes=causes(ids)))
    olds = {p.person_id: p for p in a["person"]}
    for p in b["person"]:
        old = olds.get(p.person_id)
        if old and old.mention_ids == p.mention_ids and old.officer_ids != p.officer_ids:
            gained = sorted(set(p.officer_ids) - set(old.officer_ids))
            lost = sorted(set(old.officer_ids) - set(p.officer_ids))
            detail = " ".join([*(f"+{o}" for o in gained), *(f"-{o}" for o in lost)])
            out.append(Change(kind="relinked", subject=",".join(p.mention_ids),
                              detail=f"{p.person_id} CH records {detail}",
                              causes=causes({*p.mention_ids, *gained, *lost})))
    return out


def _field_changes(kind, key, fields, a, b, causes) -> list[Change]:
    old = {getattr(m, key): m for m in a[kind]}
    new = {getattr(m, key): m for m in b[kind]}
    out = []
    for k in sorted(old.keys() | new.keys()):
        x, y = old.get(k), new.get(k)
        name = (y or x).org_name if kind == "seat" else (y or x).canonical_name
        ids = {k, (y or x).org_key}
        if x is None or y is None:
            detail = f"{kind} {k} ({name}) {'added' if x is None else 'removed'}"
        elif diffs := _diffs(fields, x.model_dump(mode="json"), y.model_dump(mode="json")):
            detail = f"{kind} {k} ({name}): {diffs}"
        else:
            continue
        out.append(Change(kind=kind, subject=k, detail=detail, causes=causes(ids)))
    return out


def _diffs(fields, x: dict, y: dict) -> str:
    return "; ".join(f"{f} {x.get(f)} → {y.get(f)}" for f in fields if x.get(f) != y.get(f))


def _rec_view(r: Recommendation | None) -> dict[str, Any]:
    if r is None:
        return {}
    return r.model_dump(mode="json") | {"points": f"{r.points_lo}–{r.points_hi}"}


def _shown_as(out: dict[str, list[Any]], by_id: dict[str, Any]) -> dict[str, str]:
    return absorbed(out["person"], {r.mention_id: r for r in out["roster"]}, by_id)


def _rec_changes(a, b, causes, by_id) -> dict[str, Change]:
    ra, rb = recs_by_mention(a), recs_by_mention(b)
    names = {r.mention_id: r.name for r in [*a["roster"], *b["roster"]]}
    was, now = _shown_as(a, by_id), _shown_as(b, by_id)  # merged B -> A, per run
    keys = {m.meeting_id: m.key or m.company_name for m in [*a["meeting"], *b["meeting"]]}
    out = {}
    for m in sorted(ra.keys() | rb.keys()):
        x, y = _rec_view(ra.get(m)), _rec_view(rb.get(m))
        if not x or not y:
            v = x or y
            diffs = f"{'new' if not x else 'removed'}: {v['vote']} at {v['points']} points"
        elif was.get(m) != now.get(m):  # B took on A's identity, or got its own back
            lead = now.get(m) or was[m]
            diffs = (f"now shown as {names.get(lead, lead)}" if m in now
                     else f"no longer shown as {names.get(lead, lead)}")
            if more := _diffs(REC_FIELDS, x, y):
                diffs += f" ({more})"
        elif not (diffs := _diffs(REC_FIELDS, x, y)):
            continue
        rec = rb.get(m) or ra[m]
        ids = {m, *person_ids(a, m), *person_ids(b, m)}
        ids |= {s for out_ in (a, b) for s in _seat_keys(out_, ids)}
        out[m] = Change(kind="recommendation", subject=m,
                        detail=f"{names.get(m, m)} @ {keys.get(rec.meeting_id)}: {diffs}",
                        causes=causes(ids))
    return out


def _seat_keys(out: dict[str, list[Any]], ids: set[str]) -> set[str]:
    seats = [s for s in out["seat"] if s.mention_id in ids or s.officer_id in ids]
    return {s.seat_id for s in seats} | {s.org_key for s in seats}


def render(changes: list[Change], indent: str = "") -> str:
    """One line per change, its causes in brackets, vote changes indented under
    the merge or split that caused them."""
    lines = []
    for c in changes:
        lines.append(f"{indent}{c.detail} ({', '.join(c.causes)})")
        if c.children:
            lines.append(render(c.children, indent + "  "))
    return "\n".join(lines)


def report(conn: sqlite3.Connection, run_a: str | None, run_b: str) -> str:
    """The diff as text, with a legend for the decisions it names (for the CLI)."""
    changes = diff(conn, run_a, run_b)
    if not changes:
        return f"no changes from {run_a} to {run_b}"
    named = {c for ch in changes for x in [ch, *ch.children] for c in x.causes}
    legend = [f"{d.decision_id}: {d.kind} {d.payload} by {d.author}: {d.reason}"
              for d in db.all_decisions(conn) if d.decision_id in named]
    return "\n".join([f"{run_a} → {run_b}", *legend, render(changes)])
