"""`pennant runs` and `pennant publish` / `pennant rollback`: pick which run's votes stand.

Design choice: a run is never edited or deleted, so rolling back is only moving the
published pointer to an earlier run that passed its gates. A rollback then stays in
effect: later runs, nightly or after an analyst decision, are staged but not published
(state/pipeline.py), because they read the same cache that may hold what went wrong. It
ends when an analyst publishes a newer run, after checking it with `pennant diff`.
"""

from __future__ import annotations

import sqlite3

import typer
from rich.text import Text

from pennant.cli_style import RUN_STYLE, console, counts, dim, styled, table
from pennant.cli_view import fail
from pennant.core import db

PUBLISHABLE = ("staged", "published")  # a held or failed run never passed its gates


def _votes(conn: sqlite3.Connection, run_id: str) -> list[str]:
    return [r.vote for r in db.get_outputs(conn, run_id, "recommendation")]


def print_runs(conn: sqlite3.Connection, limit: int) -> None:
    """The latest runs, newest first: status, who ran it, vote counts, which is published."""
    runs = db.list_runs(conn)
    if not runs:
        fail("No runs yet. Start with: pennant run")
    pub, back = db.published_run_id(conn), db.rolled_back_from(conn)
    t = table("Runs", "Run", "Status", "By", "Started", "Votes", "")
    for r in reversed(runs[-limit:]):
        note = (Text("◀ published", style="bold green") if r["run_id"] == pub
                else Text("rolled back from", style="yellow") if r["run_id"] == back
                else Text("can publish", style="dim") if r["status"] == "staged"
                and (pub is None or r["id"] > int(pub[4:])) else Text(""))
        t.add_row(r["run_id"], styled(r["status"], RUN_STYLE), r["author"],
                  str(r["started_at"])[:16].replace("T", " "),
                  counts(_votes(conn, r["run_id"])), note)
    c = console()
    c.print(t)
    if back:
        c.print(Text(f"Rolled back from {back} to {pub}: new runs are staged, not published, "
                     "until you publish one (pennant publish run:N).", style="yellow"),
                soft_wrap=True)


def publish_run(conn: sqlite3.Connection, run_id: str, yes: bool, rollback: bool) -> str:
    """Point the published votes at `run_id`; returns the run that was published before."""
    run = db.get_run(conn, run_id)
    if run is None:
        fail(f"No such run: {run_id}")
    if run["status"] not in PUBLISHABLE:
        fail(f"Refused: {run_id} is {run['status']}; only a run that passed its gates "
             "(staged, or published before) can be published.")
    before = db.published_run_id(conn)
    if run_id == before:
        fail(f"{run_id} is already published.")
    older = before is not None and int(run_id[4:]) < int(before[4:])
    if rollback and not older:
        fail(f"Refused: {run_id} is not earlier than the published {before}; "
             f"use `pennant publish {run_id}`.")
    c = console()
    c.print(Text.assemble(("Publish ", "bold"), (run_id, "bold"), f" ({run['status']}, by ",
                          run["author"], f", started {str(run['started_at'])[:16]}) in place "
                          f"of {before or 'nothing'}"))
    c.print(Text.assemble("  votes now:   ", counts(_votes(conn, before)) if before else "-"))
    c.print(Text.assemble("  votes after: ", counts(_votes(conn, run_id))))
    newest = db.rolled_back_from(conn) or before
    if older:
        c.print(dim(f"  This is a rollback: until you publish a run newer than {newest}, new "
                    "runs (nightly or after a decision) are staged, not published."),
                soft_wrap=True)
    elif db.rolled_back_from(conn) and int(run_id[4:]) > int(newest[4:]):
        c.print(dim("  This ends the rollback: later runs publish again when their gates "
                    "pass."), soft_wrap=True)
    if not yes and not typer.confirm("Publish it?", default=False):
        fail("Nothing published.")
    db.publish(conn, run_id)
    typer.echo(f"Published {run_id}.")
    return before or run_id
