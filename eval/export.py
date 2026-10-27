"""Export one stored run as the three CSVs the gold scorer grades (eval/gold/README.md).

Design choice: the gold set ships its own scorer, which reads CSVs rather than our
database, so gold and pipeline never depend on each other's internals; this module is the
only bridge. Seats carry the facts scoring used (org listing and policy flags applied), so
a seat is "counted" here exactly when it could earn points in the vote. Nothing here knows
which notices were processed: it exports whatever meetings the run holds.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from pennant.core.config import Policy
from pennant.core.models import Decision, Recommendation
from pennant.policy.score import effective_seats
from pennant.policy.stage import issuer_orgs

ROSTER_COLUMNS = ("mention", "person_id")
VOTE_COLUMNS = ("mention", "vote", "points_lo", "points_hi")
SEAT_COLUMNS = ("person_id", "org", "current", "board_seat", "chair", "listed", "counted")


def person_id(r: dict[str, Any], mention: str) -> str:
    """The run's person for a mention; an unclustered mention stands alone."""
    p = r["person_of"].get(mention)
    return p.person_id if p else f"p:{mention}"


def _write(path: Path, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)
    return path


def write_votes(out: Path, r: dict[str, Any], recs: Sequence[Recommendation]) -> Path:
    """votes.csv for any set of recommendations (the run's, or a re-score of it)."""
    by = {(x.meeting_id, x.person_id): x for x in recs}
    rows = []
    for e in r["roster"]:
        rec = by.get((e.meeting_id, person_id(r, e.mention_id)))
        rows.append((e.mention_id, rec.vote.value, rec.points_lo, rec.points_hi) if rec
                    else (e.mention_id, "MISSING", "", ""))
    return _write(out / "votes.csv", VOTE_COLUMNS, rows)


def seat_rows(r: dict[str, Any], policy: Policy,
              decisions: Sequence[Decision] = ()) -> list[tuple[str, ...]]:
    """One row per seat of every person, with effective facts and `counted` after the
    analyst's set-facts, as the vote saw them."""
    officer_owner = {o: p.person_id for p in r["person"] for o in p.officer_ids}
    first_meeting = {}  # the seat under vote counts the same at any of a person's meetings
    for e in sorted(r["roster"], key=lambda e: e.mention_id):
        first_meeting.setdefault(person_id(r, e.mention_id), e.meeting_id)
    meetings = {m.meeting_id: m for m in r["meeting"]}
    orgs = [*r["org"], *issuer_orgs(meetings.values())]
    mine: dict[str, list[Any]] = {}
    for s in r["seat"]:
        owner = (person_id(r, s.mention_id) if s.mention_id
                 else officer_owner.get(s.officer_id or ""))
        if owner:
            mine.setdefault(owner, []).append(s)
    rows = []
    for owner, seats in sorted(mine.items()):
        meeting = meetings.get(first_meeting.get(owner, ""))
        if meeting is None:
            continue
        for f in effective_seats(seats, meeting, orgs, policy, decisions):
            rows.append((owner, f["seat"].org_name, f["current"], f["board_seat"], f["chair"],
                         f["listed"], f["counted"]))
    return rows


def write_roster(out: Path, r: dict[str, Any]) -> Path:
    return _write(out / "roster.csv", ROSTER_COLUMNS,
                  [(e.mention_id, person_id(r, e.mention_id)) for e in r["roster"]])


def export_run(out: Path, r: dict[str, Any], policy: Policy,
               decisions: Sequence[Decision] = ()) -> Path:
    """Write roster.csv, votes.csv and seats.csv for a loaded run into `out`."""
    write_roster(out, r)
    write_votes(out, r, r["recommendation"])
    _write(out / "seats.csv", SEAT_COLUMNS, seat_rows(r, policy, decisions))
    return out
