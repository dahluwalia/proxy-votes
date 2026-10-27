"""`pennant log` and `pennant calls`: what a run did, and the model calls behind it.

Design choice: the report (runs.gate_report) keeps each run's stage times and counters, so
this works for any run even if its log file is gone; the log file adds the line-by-line
detail (errors with tracebacks, every live call) and is printed only with --full.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from pennant import calllog, logs
from pennant.cli_style import RUN_STYLE, console, dim, styled, table
from pennant.core import db

SOURCES = {"pdf": "PDF", "ch": "Companies House", "cache": "cache", "llamaextract": "LlamaExtract"}


def work_line(metrics: dict[str, Any]) -> str:
    """One line for `pennant run`: pages read, CH requests, AI spent this run."""
    ai = logs.ai_totals(metrics).get("live") or {}
    parts = [f"{metrics.get('pdf.pages_read', 0)} PDF pages read",
             f"{metrics.get('ch.requests', 0)} CH requests",
             f"{metrics.get('llamaextract.jobs_live', 0)} LlamaExtract jobs "
             f"({metrics.get('llamaextract.credits_live', 0):g} credits)",
             f"{ai.get('calls', 0)} research calls ({ai.get('input_tokens', 0):,} in / "
             f"{ai.get('output_tokens', 0):,} out tokens, {ai.get('web_searches', 0)} searches)"]
    return "Work this run: " + "; ".join(parts)


def _ai_table(metrics: dict[str, Any]) -> Table | Text:
    kinds = sorted({k.split(".")[1] for k in metrics if k.startswith("ai.")})
    if not kinds:
        return dim("AI: none")
    t = table("AI usage (research model)", "Question", "Calls", "Input tokens",
              "Output tokens", "Searches",
              caption="live = spent by this run; cached = what the reused answers cost "
                      "when they were fetched")
    for col in t.columns[1:]:
        col.justify = "right"
    for where in ("live", "cached"):
        for kind in kinds:
            v = [metrics.get(f"ai.{kind}.{f}_{where}", 0)
                 for f in ("calls", "input_tokens", "output_tokens", "web_searches")]
            if v[0]:
                t.add_row(f"{kind} ({where})", f"{v[0]}", f"{v[1]:,}", f"{v[2]:,}", f"{v[3]}")
        tot = logs.ai_totals(metrics).get(where) or {}
        if tot.get("calls"):
            t.add_row(Text(f"total ({where})", style="bold"), f"{tot['calls']}",
                      f"{tot.get('input_tokens', 0):,}", f"{tot.get('output_tokens', 0):,}",
                      f"{tot.get('web_searches', 0)}", end_section=True)
    return t


def print_log(conn: sqlite3.Connection, run_id: str, full: bool = False) -> None:
    c = console()
    run = db.get_run(conn, run_id)
    if run is None:
        raise typer.BadParameter(f"no run {run_id!r}")
    report = run.get("gate_report") or {}
    c.print(Text.assemble((f"{run_id}: ", "bold"), styled(run["status"], RUN_STYLE),
                          (f"  by {run['author']}  started {run['started_at']}  "
                           f"finished {run.get('finished_at') or '-'}", "dim")))
    if report.get("error"):
        c.print(Text(f"  error: {report['error']}", style="red"), soft_wrap=True)
    timings = report.get("timings") or {}
    c.print()
    t = table("Stages", "Stage", "Result", "Time", "Counts")
    t.columns[2].justify = "right"
    for st in report.get("stages") or []:
        counts = ", ".join(f"{k}={v}" for k, v in (st.get("counts") or {}).items())
        secs = f"{timings[st['stage']]:.1f}s" if st["stage"] in timings else "-"
        t.add_row(f"stage {st['stage']}", Text("ok", "green") if st.get("ok")
                  else Text("NOT OK", "bold red"), secs, counts)
        for err in (st.get("errors") or [])[:10]:
            t.add_row("", "", "", Text(f"! {err}", style="red"))
    c.print(t)
    metrics = report.get("metrics")
    if metrics is None:
        c.print(dim("  (no counters: this run predates logging)"))
    else:
        groups: dict[str, list[str]] = {}
        for name, value in metrics.items():
            head = name.split(".")[0]
            if head != "ai":
                groups.setdefault(SOURCES.get(head, head), []).append(
                    f"{name.split('.', 1)[1]}={value:g}")
        c.print()
        w = table("Work", "Source", "Counts")
        for head, items in groups.items():
            w.add_row(head, ", ".join(items))
        c.print(w)
        c.print()
        c.print(_ai_table(metrics))
    path = Path(report["log"]) if report.get("log") else None
    if path is None:
        return
    c.print(Text.assemble("\n  log file: ", (str(path), "cyan"),
                          ("" if path.exists() else " (missing)", "red")), soft_wrap=True)
    if full and path.exists():
        c.print()
        c.print(Text(path.read_text(encoding="utf-8").rstrip()), soft_wrap=True)


def print_calls(conn: sqlite3.Connection, run_id: str | None, kind: str | None,
                export: Path | None, show: int | None, backfill: bool = False) -> None:
    """`pennant calls`: recorded live model calls (calllog.py), summarised per kind,
    prompt version and model; one call in full with --show; all as JSONL with --export."""
    if backfill:
        typer.echo(f"Copied {calllog.backfill_from_cache(conn)} cached answers into the call "
                   "record (no prompt text: they predate it).")
    calls = calllog.rows(conn, run_id, kind)
    if show is not None:
        c = next((c for c in calls if c["id"] == show), None)
        if c is None:
            raise typer.BadParameter(f"no recorded call {show}")
        for k in ("id", "run_id", "called_at", "provider", "kind", "prompt_version", "model",
                  "outcome", "error", "latency_s", "inputs", "usage", "validation"):
            typer.echo(f"{k}: {c[k]}")
        typer.echo("\n--- prompt ---\n" + (c["prompt"] or ""))
        answer = [b.get("text", "") for b in ((c["response"] or {}).get("content") or [])
                  if isinstance(b, dict) and b.get("type") == "text"]
        typer.echo("\n--- answer ---\n" + ("\n".join(answer) if answer
                                          else json.dumps(c["response"])[:4000]))
        return
    if export is not None:
        n = calllog.export_jsonl(calls, export)
        typer.echo(f"Wrote {n} calls to {export}")
        return
    if not calls:
        typer.echo("No live model calls recorded" + (f" for {run_id}" if run_id else "") +
                   " (answers served from the cache make no call).")
        return
    t = table("Model calls", "Kind", "Prompt", "Model", "Calls", "ok", "Rejected",
              "Unavailable", "Error", "Secs", "In tokens", "Out tokens", "Searches", "Kept",
              "Dropped", "Credits",
              caption="secs, tokens and searches are per call; kept / dropped are facts "
                      "validation kept or dropped; credits (LlamaExtract) are the total")
    for col in t.columns[3:]:
        col.justify = "right"
    for g in calllog.summary(calls):
        secs = "-" if g["mean_latency_s"] is None else f"{g['mean_latency_s']}"

        def n(v: int, style: str) -> Text:
            return Text(str(v), style=style if v else "dim")

        t.add_row(g["kind"], g["prompt_version"], g["model"], str(g["calls"]),
                  n(g["ok"], "green"), n(g["rejected"], "yellow"), n(g["unavailable"], "red"),
                  n(g["error"], "bold red"), secs, f"{g['mean_input_tokens']:,}",
                  f"{g['mean_output_tokens']:,}", f"{g['mean_searches']}",
                  str(g["facts_kept"]), n(g["facts_dropped"], "yellow"), f"{g['credits']:g}")
    c = console()
    c.print(t)
    c.print(dim(f"pennant calls --show <id> prints one call; ids {calls[0]['id']}–"
                f"{calls[-1]['id']}"))
