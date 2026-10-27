"""`pennant`: the analyst's one command.

Design choice: read commands (votes, show, review, diff) show the published run, through
`cli_view`; `history` shows one director across every run. Write commands (merge, split,
set-fact, undo) show what they will record and ask first (`cli_changes`), then append a
decision, re-run the pipeline offline from cache and print the diff, so every change is
replayable and undoable. The CLI reaches the pipeline, decisions and diff only through the
four hooks at the bottom of this file, which tests replace with fakes.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import typer

from pennant.cli_changes import confirm, node_label, print_history, set_fact_preview, who
from pennant.cli_log import print_calls, print_log, work_line
from pennant.cli_view import (
    fail,
    load_view,
    node_id,
    print_gate_report,
    print_person,
    print_reviews,
    print_votes,
    resolve_person,
)
from pennant.core import db
from pennant.core.config import ROOT, load_settings
from pennant.core.models import Decision
from pennant.state import decisions as _decisions
from pennant.state import diff as _diff
from pennant.state import pipeline as _pipeline

app = typer.Typer(help="Board-commitment votes for AGM notices.", no_args_is_help=True,
                  add_completion=False)


SEAT_ONLY_FIELDS = {"board_seat", "chair", "executive", "current", "identity", "role_type"}
REASON = typer.Option(..., "--reason", "-r", help="Why: kept with the decision.")
YES = typer.Option(False, "--yes", "-y", help="Record without asking for confirmation.")
AUTHOR = typer.Option("analyst", "--author", help="Who made the decision.")


def _open() -> closing[sqlite3.Connection]:
    return closing(db.connect(load_settings().db_path))


# --- read commands -------------------------------------------------------------


@app.command()
def votes(meeting: str | None = typer.Option(
              None, "--meeting", "-m", help="Meeting key (from the notice's filename), company "
              "number, or part of the company name."),
          run: str | None = typer.Option(None, help="Run id (default: published).")) -> None:
    """Role, points lo-hi, rule, vote and coverage per director."""
    with _open() as conn:
        view = load_view(conn, run)
    found = view.find_meetings(meeting) if meeting else list(view.meetings.values())
    shown = sorted(found, key=lambda m: (m.agm_date, m.meeting_id))
    if not shown:
        keys = ", ".join(sorted(view.meeting_key(m) for m in view.meetings))
        fail(f"No meeting {meeting!r} in {view.run_id}. Meetings: {keys or 'none'}.")
    print_votes(view, shown)


@app.command()
def show(name: str, run: str | None = typer.Option(None, help="Run id (default: published).")
         ) -> None:
    """A person's mentions, CH records, cited seats, votes and the facts that could flip them."""
    with _open() as conn:
        view = load_view(conn, run)
    print_person(view, resolve_person(view, name))


@app.command()
def review(run: str | None = typer.Option(None, help="Run id (default: published).")) -> None:
    """Open review items, each with a suggested command."""
    with _open() as conn:
        view = load_view(conn, run)
    typer.echo(f"{len(view.reviews)} review items  {view.tag()}")
    print_reviews(view.reviews)


@app.command()
def history(name: str, every_run: bool = typer.Option(
                False, "--all", help="Show every run, not only the runs where the vote changed.")
            ) -> None:
    """A director's votes across every run, why each changed, and the decisions about them."""
    with _open() as conn:
        view = load_view(conn)
        print_history(conn, view, resolve_person(view, name), every_run)


@app.command("diff")
def diff_cmd(run_a: str | None = typer.Argument(None), run_b: str | None = typer.Argument(None)
             ) -> None:
    """Compare two runs (default: the published run against the latest run)."""
    with _open() as conn:
        ids = [r["run_id"] for r in db.list_runs(conn)]
        if not ids:
            fail("No runs yet. Start with: pennant run")
        run_b = run_b or ids[-1]
        if run_a is None:
            pub = db.published_run_id(conn)
            run_a = pub if pub and pub != run_b else (ids[ids.index(run_b) - 1]
                                                      if run_b in ids[1:] else run_b)
        for r in (run_a, run_b):
            if r not in ids:
                fail(f"No such run: {r}")
        _print_diff(conn, run_a, run_b)


def _print_diff(conn: sqlite3.Connection, run_a: str, run_b: str) -> None:
    typer.echo(f"\nDiff: {diff_text(conn, run_a, run_b)}")


# --- write commands ------------------------------------------------------------

def _record_and_rerun(conn: sqlite3.Connection, kind: str, payloads: list[dict[str, Any]],
                      author: str, reason: str) -> None:
    try:
        made = [add_decision(conn, kind, pl, author, reason) for pl in payloads]
    except ValueError as err:  # e.g. a must-link that contradicts a cannot-link
        fail(f"Refused: {err}")
    for d in made:
        typer.echo(f"Recorded {d.decision_id}: {d.kind} {d.payload}")
    _rerun(conn, author)


def _rerun(conn: sqlite3.Connection, author: str) -> str:
    before = db.published_run_id(conn)
    run_id = run_pipeline(load_settings(), offline=True, publish=True, author=author)
    print_gate_report(conn, run_id)
    if before and before != run_id:
        _print_diff(conn, before, run_id)
    return run_id


@app.command("run")
def run_cmd(offline: bool = typer.Option(False, help="Use only cached responses."),
            publish: bool = typer.Option(False, help="Publish if every gate passes.")) -> None:
    """Run the pipeline; print the gate report and the diff against the published run."""
    settings = load_settings()
    with _open() as conn:
        before = db.published_run_id(conn)
        run_id = run_pipeline(settings, offline=offline or settings.offline, publish=publish,
                              author="system")
        status = print_gate_report(conn, run_id)
        report = (db.get_run(conn, run_id) or {}).get("gate_report") or {}
        if report.get("metrics") is not None:
            typer.echo(f"  {work_line(report['metrics'])}")
        if report.get("log"):
            typer.echo(f"  Log: {report['log']}  (details: pennant log {run_id})")
        if before and before != run_id:
            _print_diff(conn, before, run_id)
    if status in ("held", "failed"):
        raise typer.Exit(1)


@app.command("log")
def log_cmd(run: str | None = typer.Argument(None, help="Run id (default: the latest run)."),
            full: bool = typer.Option(False, "--full", help="Also print the run's log file.")
            ) -> None:
    """What a run did: stage times and counts, errors, pages read, CH requests, AI tokens."""
    with _open() as conn:
        runs = db.list_runs(conn)
        if not runs:
            fail("No runs yet: `pennant run` first.")
        print_log(conn, run or runs[-1]["run_id"], full)


@app.command("calls")
def calls_cmd(run: str | None = typer.Option(None, help="Only this run's calls."),
              kind: str | None = typer.Option(None, help="org, site, position or extract."),
              export: Path | None = typer.Option(None, help="Write the calls as JSONL here."),
              show: int | None = typer.Option(None, help="Print one call: prompt and answer."),
              backfill: bool = typer.Option(False, help="First copy in answers cached before "
                                            "the call record existed.")) -> None:
    """Recorded live model calls: per prompt and model, or one in full, or as JSONL."""
    with _open() as conn:
        print_calls(conn, run, kind, export, show, backfill)


@app.command()
def merge(a: str, b: str, reason: str = REASON, author: str = AUTHOR, yes: bool = YES) -> None:
    """Record that two people (or a person and a CH officer id) are one; re-run and diff.

    A and B are names, person ids, mention ids or CH officer ids (as review items suggest).
    """
    with _open() as conn:
        view = load_view(conn)
        (na, pa), (nb, pb) = node_id(view, a), node_id(view, b)
        if na == nb or (pa and pa == pb):
            fail(f"{a!r} and {b!r} are already one person ({pa or na}).")
        confirm([f"Merge into one person: {node_label(view, na)} and {node_label(view, nb)}"],
                 yes)
        _record_and_rerun(conn, "must_link", [{"a": na, "b": nb}], author, reason)


@app.command()
def split(person: str, move: list[str] = typer.Option(..., "--move", help="Mention ids or "
          "meeting keys to split off (repeat, or comma-separate)."), reason: str = REASON,
          author: str = AUTHOR, yes: bool = YES) -> None:
    """Record that some of a person's mentions are someone else; re-run and diff."""
    wanted = {q.strip() for item in move for q in item.split(",") if q.strip()}
    with _open() as conn:
        view = load_view(conn)
        p = resolve_person(view, person)

        def picked(m: str) -> bool:
            in_roster = m in view.roster
            return m in wanted or (in_roster and view.meeting_key(view.roster[m].meeting_id)
                                   in wanted)

        moved = sorted(m for m in p.mention_ids if picked(m))
        stay = sorted(m for m in p.mention_ids if not picked(m))
        if not moved or not stay:
            fail(f"--move must pick some but not all of {sorted(p.mention_ids)}.")
        confirm([f"Split {who(view, p)}: {', '.join(view.mention_label(m) for m in moved)} "
                  f"becomes someone else; {', '.join(view.mention_label(m) for m in stay)} stays"],
                 yes)
        # One decision per split (CONTRACT_CHANGES D1), so one undo reverses it.
        _record_and_rerun(conn, "cannot_link", [{"a": moved, "b": stay}], author, reason)


@app.command("set-fact")
def set_fact(subject: str, field: str, value: str, reason: str = REASON,
             author: str = AUTHOR, yes: bool = YES) -> None:
    """Override one fact on a seat, org or roster entry; re-run and diff.

    SUBJECT is a seat id, an org key, or a mention id (for role_type_at_issuer where the
    notice states no role).
    """
    with _open() as conn:
        view = load_view(conn)
        people = {p.person_id: p for p in view.persons}
        if any(s.seat_id == subject for s in view.seats):
            kind = "seat"
        elif subject in view.orgs:
            kind = "org"
        elif subject in view.roster:
            kind = "roster"
        elif subject in people:
            if len(people[subject].mention_ids) != 1:
                fail(f"{subject} stands at several meetings; give one mention id: "
                      f"{', '.join(sorted(people[subject].mention_ids))}")
            kind, subject = "roster", people[subject].mention_ids[0]
        else:
            near = [s.seat_id for s in view.seats if subject.casefold() in s.seat_id.casefold()]
            fail(f"No seat, org or mention {subject!r}."
                  + (f" Did you mean: {', '.join(near[:5])}" if near else ""))
        if kind == "org" and field in SEAT_ONLY_FIELDS:
            at_org = [s.seat_id for s in view.seats if s.org_key == subject]
            fail(f"{field} is a seat fact; seats at {subject}: {', '.join(at_org) or 'none'}")
        # Field and value are validated by add_decision (CONTRACT_CHANGES B1).
        payload = {"kind": kind, "target": subject, "field": field, "value": value}
        confirm(set_fact_preview(view, kind, subject, field, value), yes)
        _record_and_rerun(conn, "set_fact", [payload], author, reason)


@app.command()
def undo(decision_id: str, reason: str = typer.Option("undo", "--reason", "-r"),
         author: str = AUTHOR, yes: bool = YES) -> None:
    """Revoke a decision, re-run and diff."""
    with _open() as conn:
        d = next((x for x in db.all_decisions(conn) if x.decision_id == decision_id), None)
        if d is None:
            fail(f"Refused: no such decision: {decision_id!r}")
        confirm([f"Undo {d.decision_id}: {d.kind} {d.payload} by {d.author} on "
                  f"{str(d.created_at)[:10]} ({d.reason})"], yes)
        try:
            d = revoke(conn, decision_id, author, reason)
        except ValueError as err:  # unknown or already revoked
            fail(f"Refused: {err}")
        typer.echo(f"Recorded {d.decision_id}: revoke {decision_id}")
        _rerun(conn, author)


@app.command("eval")
def eval_cmd() -> None:
    """Write eval/report.md from the gold files and the published run."""
    path = ROOT / "eval" / "metrics.py"
    spec = importlib.util.spec_from_file_location("pennant_eval_metrics", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    module.main()
    typer.echo(f"Wrote {ROOT / 'eval' / 'report.md'}")


# --- hooks into the pipeline, decisions and diff; tests replace these ---------


def run_pipeline(settings: Any, offline: bool, publish: bool, author: str) -> str:
    """Run the pipeline; returns the new run id (its gate report is stored in `runs`)."""
    return _pipeline.run_pipeline(settings, offline=offline, publish=publish,
                                  author=author).run_id


def add_decision(conn: sqlite3.Connection, kind: str, payload: dict[str, Any], author: str,
                 reason: str) -> Decision:
    """Validate and append a decision; raises ValueError (DecisionError) when refused."""
    return _decisions.add_decision(conn, kind, payload, author, reason)


def revoke(conn: sqlite3.Connection, decision_id: str, author: str, reason: str) -> Decision:
    return _decisions.revoke(conn, decision_id, author, reason)


def diff_text(conn: sqlite3.Connection, run_a: str, run_b: str) -> str:
    """The changes between two runs, each naming its causes, with a legend of decisions."""
    return _diff.report(conn, run_a, run_b)
