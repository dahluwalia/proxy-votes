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

from pennant import calllog, logs
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


def _ai_table(metrics: dict[str, Any]) -> list[str]:
    kinds = sorted({k.split(".")[1] for k in metrics if k.startswith("ai.")})
    if not kinds:
        return ["  AI: none"]
    rows = ["  AI (research model)   calls  input tokens  output tokens  searches"]
    for where in ("live", "cached"):
        for kind in kinds:
            v = [metrics.get(f"ai.{kind}.{f}_{where}", 0)
                 for f in ("calls", "input_tokens", "output_tokens", "web_searches")]
            if v[0]:
                rows.append(f"    {kind + ' (' + where + ')':<20}{v[0]:>6}{v[1]:>14,}{v[2]:>15,}"
                            f"{v[3]:>10}")
        t = logs.ai_totals(metrics).get(where) or {}
        if t.get("calls"):
            rows.append(f"    {'total (' + where + ')':<20}{t['calls']:>6}"
                        f"{t.get('input_tokens', 0):>14,}{t.get('output_tokens', 0):>15,}"
                        f"{t.get('web_searches', 0):>10}")
    rows.append("    live = spent by this run; cached = what the reused answers cost when fetched")
    return rows


def print_log(conn: sqlite3.Connection, run_id: str, full: bool = False) -> None:
    run = db.get_run(conn, run_id)
    if run is None:
        raise typer.BadParameter(f"no run {run_id!r}")
    report = run.get("gate_report") or {}
    typer.echo(f"{run_id}: {run['status']}  by {run['author']}  started {run['started_at']}  "
               f"finished {run.get('finished_at') or '-'}")
    if report.get("error"):
        typer.echo(f"  error: {report['error']}")
    timings = report.get("timings") or {}
    for st in report.get("stages") or []:
        counts = ", ".join(f"{k}={v}" for k, v in (st.get("counts") or {}).items())
        secs = f"{timings[st['stage']]:.1f}s" if st["stage"] in timings else "-"
        typer.echo(f"  stage {st['stage']:<9} {'ok' if st.get('ok') else 'NOT OK':<7}{secs:>7}  "
                   f"{counts}")
        for err in (st.get("errors") or [])[:10]:
            typer.echo(f"      ! {err}")
    metrics = report.get("metrics")
    if metrics is None:
        typer.echo("  (no counters: this run predates logging)")
    else:
        groups: dict[str, list[str]] = {}
        for name, value in metrics.items():
            head = name.split(".")[0]
            if head != "ai":
                groups.setdefault(SOURCES.get(head, head), []).append(
                    f"{name.split('.', 1)[1]}={value:g}")
        for head, items in groups.items():
            typer.echo(f"  {head}: {', '.join(items)}")
        for line in _ai_table(metrics):
            typer.echo(line)
    path = Path(report["log"]) if report.get("log") else None
    if path is None:
        return
    typer.echo(f"  log file: {path}" + ("" if path.exists() else " (missing)"))
    if full and path.exists():
        typer.echo("")
        typer.echo(path.read_text(encoding="utf-8").rstrip())


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
    typer.echo(f"{'kind':<9}{'prompt':<20}{'model':<22}{'calls':>6}{'ok':>5}{'rej':>5}"
               f"{'unav':>6}{'err':>5}{'secs':>7}{'in tok':>9}{'out tok':>9}{'srch':>6}"
               f"{'kept':>6}{'dropped':>8}{'credits':>9}")
    for g in calllog.summary(calls):
        secs = "-" if g["mean_latency_s"] is None else g["mean_latency_s"]
        typer.echo(f"{g['kind']:<9}{g['prompt_version'][:19]:<20}{g['model'][:21]:<22}"
                   f"{g['calls']:>6}{g['ok']:>5}{g['rejected']:>5}{g['unavailable']:>6}"
                   f"{g['error']:>5}{secs:>7}{g['mean_input_tokens']:>9,}"
                   f"{g['mean_output_tokens']:>9,}{g['mean_searches']:>6}{g['facts_kept']:>6}"
                   f"{g['facts_dropped']:>8}{g['credits']:>9g}")
    typer.echo("secs, tokens and searches are per call; kept / dropped are facts validation "
               "kept or dropped; credits (LlamaExtract) are the total. "
               "`pennant calls --show <id>` prints one call; ids: "
               f"{calls[0]['id']}–{calls[-1]['id']}.")
